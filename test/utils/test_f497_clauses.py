"""F497 AC14 — required-clause lint (D11 body-regression protection).

The clause table (``profiles/positions/_clauses.toml``, supervisor-owned) is the
POLICY; the position personas are the artifacts under test. These tests prove the
lint passes on the seeded corpus and FAILS CLOSED on every malformed shape.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from cli_agent_orchestrator.utils.clause_lint import (
    ClauseLintError,
    lint_positions,
    load_clause_table,
)

_HERE = pathlib.Path(__file__).resolve().parent


def _positions_dir() -> "pathlib.Path | None":
    env = os.environ.get("CAO_F497_PROFILES_DIR", "").strip()
    cands = [pathlib.Path(env) / "positions"] if env else []
    for anc in _HERE.parents:
        p = anc / "profiles" / "positions"
        if (p / "_clauses.toml").is_file():
            cands.append(p)
    for c in cands:
        if (c / "_clauses.toml").is_file():
            return c
    return None


_POS = _positions_dir()
_skip = pytest.mark.skipif(_POS is None, reason="drafted positions corpus not found")


@_skip
def test_ac14_seeded_corpus_passes_lint():
    results = lint_positions(_POS)
    # Every position in the corpus is linted and carries its required clauses.
    assert "empirical_reviewer" in results
    assert {
        "callback-contract",
        "containment",
        "f129-pins",
        "never-edit-artifact-branch",
        "test-attachments",
    } <= set(results["empirical_reviewer"])
    assert {"callback-contract", "containment"} <= set(results["dev"])
    assert "grunt-scope" in results["grunt"]


@_skip
def test_ac14_table_loads_and_validates():
    table = load_clause_table(_POS / "_clauses.toml")
    assert "f129-pins" in table.rules
    assert table.rules["f129-pins"].heading is not None
    assert table.rules["callback-contract"].marker is not None


# --- fail-closed cases (synthetic tables/positions in tmp) -----------------


def _write(tmp, name, text):
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def _mk_positions(tmp, clause_toml, files: dict):
    (tmp / "_clauses.toml").write_text(clause_toml, encoding="utf-8")
    for fname, body in files.items():
        (tmp / fname).write_text(body, encoding="utf-8")
    return tmp


_MINIMAL_TABLE = """
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[required]
dev = ["callback-contract", "containment"]
"""


def test_ac14_fail_missing_clause(tmp_path):
    _mk_positions(
        tmp_path,
        _MINIMAL_TABLE,
        {
            "dev.md": "---\nrole: developer\n---\n\n# Dev\n\n<!-- clause:callback-contract -->\nbody\n"
        },
    )
    with pytest.raises(ClauseLintError, match="missing required clause 'containment'"):
        lint_positions(tmp_path)


def test_ac14_fail_position_without_row(tmp_path):
    _mk_positions(
        tmp_path,
        _MINIMAL_TABLE,
        {
            "dev.md": "---\n---\n\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n",
            "rogue.md": "---\n---\n\n# Rogue\n\nno row for me\n",
        },
    )
    with pytest.raises(ClauseLintError, match="no \\[required\\] row"):
        lint_positions(tmp_path)


def test_ac14_fail_row_without_file(tmp_path):
    table = _MINIMAL_TABLE + '\nreviewer = ["callback-contract"]\n'
    _mk_positions(
        tmp_path,
        table,
        {"dev.md": "---\n---\n\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"},
    )
    with pytest.raises(ClauseLintError, match="names a position with no"):
        lint_positions(tmp_path)


def test_ac14_fail_unknown_clause_in_table(tmp_path):
    bad = _MINIMAL_TABLE + "\n[required2]\n"  # noqa - ensure required references known ids
    table = """
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[required]
dev = ["callback-contract", "does-not-exist"]
"""
    _mk_positions(tmp_path, table, {"dev.md": "---\n---\n\n<!-- clause:callback-contract -->\n"})
    with pytest.raises(ClauseLintError, match="unknown clause id 'does-not-exist'"):
        lint_positions(tmp_path)


def test_ac14_requires_may_only_add(tmp_path):
    # A position `requires:` naming a BOGUS id-shaped clause id fails closed
    # (AC14: unknown id is a mistake, never silently dropped).
    table = """
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[required]
dev = ["callback-contract", "containment"]
"""
    _mk_positions(
        tmp_path,
        table,
        {
            "dev.md": "---\nrequires: [no-such-clause]\n---\n\n"
            "<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
        },
    )
    with pytest.raises(ClauseLintError, match="unknown clause id 'no-such-clause'"):
        lint_positions(tmp_path)


def test_ac14_requires_known_id_must_be_present(tmp_path):
    # requires adds a KNOWN id -> must now be present or lint fails.
    table = """
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[clauses.extra-thing]
marker = "<!-- clause:extra-thing -->"
[required]
dev = ["callback-contract", "containment"]
"""
    _mk_positions(
        tmp_path,
        table,
        {
            "dev.md": "---\nrequires: [extra-thing]\n---\n\n"
            "<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
            # note: extra-thing marker intentionally absent
        },
    )
    with pytest.raises(ClauseLintError, match="missing required clause 'extra-thing'"):
        lint_positions(tmp_path)


def test_ac14_requires_prose_sentence_is_accepted(tmp_path):
    # A free-text prose sentence in `requires:` is legal and ignored — it is
    # not id-shaped, so it is neither an ADD directive nor a fail-closed error.
    table = """
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[required]
dev = ["callback-contract", "containment"]
"""
    _mk_positions(
        tmp_path,
        table,
        {
            "dev.md": "---\n"
            'requires:\n  - "Runs the empirical gate; never edits the artifact."\n'
            "---\n\n"
            "<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
        },
    )
    results = lint_positions(tmp_path)
    assert set(results["dev"]) == {"callback-contract", "containment"}


def test_ac14_clause_must_set_exactly_one_of_heading_marker(tmp_path):
    table = """
[clauses.bad]
heading = "## X"
marker = "<!-- clause:bad -->"
[required]
dev = ["bad"]
"""
    _mk_positions(tmp_path, table, {"dev.md": "---\n---\n\n## X\n"})
    with pytest.raises(ClauseLintError, match="EXACTLY one of heading/marker"):
        lint_positions(tmp_path)


def test_ac14_markers_are_never_stripped_by_composition():
    """Inline clause markers ship verbatim through composition (must not be stripped)."""
    if _POS is None:
        pytest.skip("drafted positions corpus not found")
    from cli_agent_orchestrator.utils.profile_composition import Layer, compose_source_body

    body = (_POS / "empirical_reviewer.md").read_text(encoding="utf-8")
    import frontmatter

    parsed = frontmatter.loads(body)
    composed = compose_source_body(
        [Layer(kind="position:empirical_reviewer", metadata={}, body=parsed.content)]
    )
    assert "<!-- clause:callback-contract -->" in composed
    assert "<!-- clause:never-edit-artifact-branch -->" in composed
