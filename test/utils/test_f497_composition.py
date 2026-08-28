"""F497 P2 — profile composition merge engine + drafted persona composition.

P2 (r5/r6) lands the D5 merge engine (``utils/profile_composition.py``), the D3
store layout, and DRAFTED unified personas for the ``empirical_reviewer`` and
``dev`` positions (plus ``grunt``). Per the r5 user directive, persona BODY text
is NO LONGER required to be byte-identical to the legacy profiles — personas may
be rewritten to FIT the position. AC1 therefore narrows to STRUCTURAL/frontmatter
identity + a required-sections lint + a per-(position,provider) smoke compose.

The repo stubs are NOT wired to the new personas until the r5 gate confirms; the
smoke/lint tests here drive composition through EPHEMERAL stubs in a tmp store, so
they validate the engine + drafts without touching the repo's legacy profiles.

Coverage:
  * D5 engine units: AC3 (skills tri-state), AC6 (contextPolicy atomic +
    extraLeaves union), AC10 (replaces-absent hard error), AC13 (catch-all total
    coverage), meta-key stripping.
  * Smoke: every drafted (position, provider) pair composes with no error and a
    non-empty persona.
  * Required-sections lint: each drafted position body carries its invariant
    sections (worker-protocol callback rules, containment, stop-and-ask; plus
    F129 + never-edit + AC14 test-attachments for the reviewer).
  * AC14: the reviewer persona carries the test-attachments/suite-recommendation
    clause and a ``requires:`` clause set.
  * Ruling 3: no overlay carries ``model``/``reasoningEffort``.
"""

from __future__ import annotations

import pathlib

import frontmatter
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.profile_composition import (
    CONTEXT_POLICY_FIELD,
    DICT_SHALLOW_FIELDS,
    LIST_UNION_FIELDS,
    PERSONA_TEXT_FIELD,
    CompositionError,
    Layer,
    compose_profile,
)

# The drafted persona corpus lives in the ROOT repo's profiles/{positions,overlays}.
# Discover it via env override or a sibling walk; skip when absent (bare checkout).
import os

_ENV = "CAO_F497_PROFILES_DIR"


def _profiles_dir() -> "pathlib.Path | None":
    env = os.environ.get(_ENV, "").strip()
    cands = [pathlib.Path(env)] if env else []
    here = pathlib.Path(__file__).resolve()
    for anc in here.parents:
        sib = anc / "profiles"
        if (sib / "positions").is_dir():
            cands.append(sib)
    for c in cands:
        if (c / "positions").is_dir():
            return c
    return None


_PROFILES = _profiles_dir()

# Drafted (position, provider) pairs to smoke-compose (unwired in the repo).
_SMOKE_PAIRS = [
    ("empirical_reviewer", "kiro_cli"),
    ("empirical_reviewer", "codex"),
    ("dev", "kiro_cli"),
    ("dev", "codex"),
    ("dev", "grok_cli"),
    ("grunt", "cline_cli"),
]


def _install_ephemeral_stores(monkeypatch, tmp_path):
    """Copy the drafted positions/overlays into a tmp agent-store and repoint."""
    if _PROFILES is None:
        pytest.skip("drafted persona corpus not found; set CAO_F497_PROFILES_DIR")
    import shutil

    store = tmp_path / "agent-store"
    (store / "positions").mkdir(parents=True)
    (store / "overlays").mkdir(parents=True)
    for f in (_PROFILES / "positions").glob("*.md"):
        shutil.copy(f, store / "positions" / f.name)
    for f in (_PROFILES / "overlays").glob("*.md"):
        shutil.copy(f, store / "overlays" / f.name)

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.utils import agent_profiles

    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "LOCAL_AGENT_STORE_DIR", store)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", store)
    return store


def _write_ephemeral_stub(store, name, position, provider, *, description="d", role="developer"):
    post = frontmatter.Post("")
    post["name"] = name
    post["provider"] = provider
    post["extends"] = position
    post["description"] = description
    post["role"] = role
    (store / f"{name}.md").write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Smoke: every drafted (position, provider) pair composes with a real persona
# --------------------------------------------------------------------------


@pytest.mark.parametrize("position,provider", _SMOKE_PAIRS)
def test_smoke_drafted_pair_composes(monkeypatch, tmp_path, position, provider):
    store = _install_ephemeral_stores(monkeypatch, tmp_path)
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

    name = f"{provider}_{position}_smoke"
    _write_ephemeral_stub(store, name, position, provider)
    prof = load_agent_profile(name)
    assert prof.name == name  # D6: composed .name is the legacy/stub name
    assert prof.position == position
    assert prof.provider == provider
    assert prof.system_prompt and len(prof.system_prompt) > 200, "persona is non-trivial"


# --------------------------------------------------------------------------
# AC1 (narrowed by D11) — extracted names byte-identical EXCEPT system_prompt
# --------------------------------------------------------------------------

_GOLDEN = pathlib.Path(__file__).resolve().parent / "f497_golden"

# Fields legitimately differing on an extracted stub vs its pre-extraction
# golden: the persona body (D11 authored fresh) and the resolver-internal axis.
_AC1_EXCLUDE = {"system_prompt", "position"}


@pytest.mark.parametrize("name", ["kiro_reviewer", "codex_empirical_reviewer"])
def test_ac1_narrowed_extracted_profile_matches_golden_except_body(monkeypatch, tmp_path, name):
    """Every non-body field of an EXTRACTED stub equals the pre-extraction golden.

    D11 dropped byte-identity of the persona BODY for extracted names; AC1 now
    asserts field-for-field equality for every field EXCEPT ``system_prompt``
    (and the resolver-internal ``position`` axis). Unextracted profiles remain
    fully byte-identical (covered by the Phase-1 corpus harness).
    """
    if _PROFILES is None or not (_GOLDEN / f"{name}.md").is_file():
        pytest.skip("golden corpus not available")
    store = _install_ephemeral_stores(monkeypatch, tmp_path)
    # Wire the real stub for this name into the ephemeral store.
    import shutil

    shutil.copy(_PROFILES / f"{name}.md", store / f"{name}.md")

    from cli_agent_orchestrator.utils.agent_profiles import (
        load_agent_profile,
        parse_agent_profile_text,
    )
    from cli_agent_orchestrator.utils.env import resolve_env_vars

    golden = parse_agent_profile_text(
        resolve_env_vars((_GOLDEN / f"{name}.md").read_text(encoding="utf-8")), name
    )
    composed = load_agent_profile(name)

    gd, cd = golden.model_dump(), composed.model_dump()
    drift = {k for k in gd if gd[k] != cd[k]} - _AC1_EXCLUDE
    assert not drift, f"{name}: non-body fields diverged from golden: {drift}"
    # D6: composed .name stays the legacy concrete name.
    assert composed.name == name
    # The persona body IS different (authored) and non-trivial.
    assert composed.system_prompt and composed.system_prompt != golden.system_prompt


_POSITION_REQUIRED_NOTE = (
    "Required-sections regression protection is the AC14 clause-lint "
    "(test_f497_clauses.py), driven by the supervisor-owned _clauses.toml. This "
    "file's smoke + AC1-narrowed tests cover composition; the lint is not "
    "duplicated here."
)


# --------------------------------------------------------------------------
# AC14 — reviewer test-attachments clause + requires: clause set
# --------------------------------------------------------------------------


def test_ac14_reviewer_requires_clause_present():
    if _PROFILES is None:
        pytest.skip("drafted persona corpus not found")
    post = frontmatter.loads(
        (_PROFILES / "positions" / "empirical_reviewer.md").read_text(encoding="utf-8")
    )
    requires = post.metadata.get("requires")
    assert isinstance(requires, list) and requires, "empirical_reviewer needs a requires: set"
    joined = " ".join(requires).lower()
    assert "never edits" in joined and "artifact branch" in joined
    assert "test attachment" in joined or "suite" in joined


# --------------------------------------------------------------------------
# Ruling 3 — no overlay carries model/reasoningEffort
# --------------------------------------------------------------------------


def test_ruling3_no_overlay_carries_model_keys():
    if _PROFILES is None:
        pytest.skip("drafted persona corpus not found")
    banned = {"model", "reasoningEffort"}
    for ov in sorted((_PROFILES / "overlays").glob("*.md")):
        meta = frontmatter.loads(ov.read_text(encoding="utf-8")).metadata
        present = banned & set(meta)
        assert not present, (
            f"overlay {ov.name} carries banned model key(s) {present}; a "
            f"provider-specific model stays on the legacy stub until F277 #90 (ruling 3)"
        )


# --------------------------------------------------------------------------
# D5 engine unit tests (independent of the drafted corpus)
# --------------------------------------------------------------------------


def _pos_layer(meta=None, body="# P\n\nposition body"):
    return Layer(kind="position:test", metadata=dict(meta or {}), body=body)


def _ov_layer(meta=None, body="", provider="codex", replaces=None):
    return Layer(
        kind=f"overlay:{provider}",
        metadata=dict(meta or {}),
        body=body,
        provider=provider,
        replaces=list(replaces or []),
    )


def _compose(pos_meta, ov_meta, *, pos_body="# P\n\nbody", ov_body="", replaces=None):
    return compose_profile(
        "composed",
        [_pos_layer(pos_meta, pos_body), _ov_layer(ov_meta, ov_body, replaces=replaces)],
        position_name="test",
        provider="codex",
    )


def test_ac3_skills_absent_inherits_position():
    assert _compose({"description": "d", "skills": ["a", "b"]}, {"description": "d"}).skills == [
        "a",
        "b",
    ]


def test_ac3_skills_explicit_null_overrides_to_full_catalog():
    prof = _compose({"description": "d", "skills": ["a"]}, {"description": "d", "skills": None})
    assert prof.skills is None


def test_ac3_skills_empty_and_replace():
    prof = _compose({"description": "d", "skills": ["a"]}, {"description": "d", "skills": []})
    prof2 = _compose(
        {"description": "d", "skills": ["a"]},
        {"description": "d", "skills": [], "_replace": ["skills"]},
    )
    assert prof.skills == ["a"]  # union with [] adds nothing
    assert prof2.skills == []  # _replace forces override


def test_ac3_skills_union_dedupes_order_preserving():
    prof = _compose(
        {"description": "d", "skills": ["a", "b"]},
        {"description": "d", "skills": ["b", "c"]},
    )
    assert prof.skills == ["a", "b", "c"]


def test_ac6_context_policy_atomic_replace_unions_extra_leaves():
    pos_cp = {"scope": "persona", "memoryTypes": ["feedback"], "extraLeaves": ["pos-leaf.md"]}
    ov_cp = {"scope": "persona", "memoryTypes": ["project"], "extraLeaves": ["gpt-unrestricted.md"]}
    prof = _compose(
        {"description": "d", "contextPolicy": pos_cp},
        {"description": "d", "contextPolicy": ov_cp},
    )
    assert prof.contextPolicy.memoryTypes == ["project"]  # atomic
    assert set(prof.contextPolicy.extraLeaves) == {"pos-leaf.md", "gpt-unrestricted.md"}  # union


def test_ac6_position_only_context_policy_survives():
    prof = _compose(
        {"description": "d", "contextPolicy": {"scope": "persona", "extraLeaves": ["x.md"]}},
        {"description": "d"},
    )
    assert prof.contextPolicy.extraLeaves == ["x.md"]


def test_ac10_replaces_absent_heading_is_hard_error():
    with pytest.raises(CompositionError, match="absent from the composed"):
        _compose(
            {"description": "d"},
            {"description": "d"},
            pos_body="# P\n\nbody\n\n## Real\n\ntext",
            ov_body="## Replacement\n\nnew",
            replaces=["Nonexistent"],
        )


def test_ac10_replaces_present_heading_swaps_in_place():
    prof = _compose(
        {"description": "d"},
        {"description": "d"},
        pos_body="# P\n\nintro\n\n## Rules\n\nold\n\n## Tail\n\ntail",
        ov_body="## Rules\n\nnew rules",
        replaces=["Rules"],
    )
    assert "new rules" in prof.system_prompt
    assert "old" not in prof.system_prompt
    assert "## Tail" in prof.system_prompt


def test_ac13_every_model_field_has_an_assigned_merge_class():
    named = set(LIST_UNION_FIELDS) | set(DICT_SHALLOW_FIELDS) | {CONTEXT_POLICY_FIELD}
    fields = set(AgentProfile.model_fields.keys())
    assert PERSONA_TEXT_FIELD in fields
    assert named <= fields, f"merge classes name non-fields: {named - fields}"
    assert LIST_UNION_FIELDS.isdisjoint(DICT_SHALLOW_FIELDS)
    assert CONTEXT_POLICY_FIELD not in LIST_UNION_FIELDS | DICT_SHALLOW_FIELDS
    assert fields - named - {PERSONA_TEXT_FIELD}, "expected fields in the scalar catch-all"


def test_r9_position_sha_excludes_certification_block():
    """r9(a): position_sha hashes body + merge-relevant frontmatter EXCLUDING the
    certification: block, so recording a PASS row never invalidates the sha."""
    from cli_agent_orchestrator.utils.profile_composition import position_sha

    body = "# P\n\npersona body\n"
    meta_uncertified = {
        "role": "developer",
        "skills": ["a"],
        "certification": [{"provider": "codex", "outcome": "UNCERTIFIED"}],
    }
    meta_pass = {
        "role": "developer",
        "skills": ["a"],
        "certification": [{"provider": "codex", "outcome": "PASS", "date": "2026-09-01"}],
    }
    # Flipping certification UNCERTIFIED -> PASS must NOT change position_sha.
    assert position_sha(body, meta_uncertified) == position_sha(body, meta_pass)
    # A merge-relevant frontmatter change (skills) DOES change it.
    assert position_sha(body, dict(meta_uncertified, skills=["a", "b"])) != position_sha(
        body, meta_uncertified
    )
    # A body change changes it.
    assert position_sha(body + "x", meta_uncertified) != position_sha(body, meta_uncertified)


def test_r9_recorded_position_sha_matches_helper():
    """The committed certification block's position_sha equals the helper output
    over the live position file (body + merge-relevant frontmatter minus cert)."""
    if _PROFILES is None:
        pytest.skip("drafted persona corpus not found")
    import frontmatter as _fm

    from cli_agent_orchestrator.utils.profile_composition import position_sha

    p = _fm.loads((_PROFILES / "positions" / "empirical_reviewer.md").read_text(encoding="utf-8"))
    recomputed = position_sha(p.content, dict(p.metadata))
    cert = p.metadata.get("certification") or []
    assert cert, "empirical_reviewer must carry a certification block"
    for row in cert:
        assert (
            row["position_sha"] == recomputed
        ), f"recorded position_sha {row['position_sha']} != helper {recomputed} (r9)"
        assert row["outcome"] == "UNCERTIFIED"  # P2 seeds both cells UNCERTIFIED


def test_meta_keys_never_reach_agent_profile():
    prof = compose_profile(
        "composed",
        [
            _pos_layer(
                {
                    "description": "d",
                    "providers": ["codex"],
                    "extends": "x",
                    "requires": ["callback-contract"],
                    "certification": [{"provider": "codex", "outcome": "UNCERTIFIED"}],
                }
            ),
            _ov_layer({"description": "d", "_replace": ["skills"], "replaces": []}),
        ],
        position_name="test",
        provider="codex",
    )
    dumped = prof.model_dump()
    # AC2 (r8): extends, _replace, position (directive), providers, requires,
    # certification, replaces never reach AgentProfile.
    for meta in ("extends", "_replace", "providers", "replaces", "requires", "certification"):
        assert meta not in dumped
