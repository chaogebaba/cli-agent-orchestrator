"""F788 #645 — a production writer for certification rows, and a detector for drift.

The incident this file reproduces, verbatim from #645: ``profiles/positions/general.md``
carried PASS rows for ``kiro_cli`` and ``codex`` at ``position_sha 5eb08f4f9de16738``
while the store had moved to ``ca548c655b4ceb3e``. ``routing.cell_certified`` answered
UNCERTIFIED, ``resolve_routing_binding`` refused every kiro/codex row
``E-PROVIDER-UNCERTIFIED``, and the fleet ran for weeks on the ``provider=`` bypass
because NOTHING diffed the recorded pair against the current one.

Two mechanisms close it and both are tested here by execution, not by inspection:

* ``cao-orchestrator certify`` is the only writer, computes the pair with the reader's
  own helpers, demands evidence, and replaces a row rather than appending a second;
* ``cao-orchestrator cert-status`` diffs every row on both axes and exits 1 on drift.

The last three tests are MUTANTS: each disables one property of the pair and asserts
the suite would notice. A writer with no sha pin, an appending writer and a detector
blind to ``overlay_sha`` each reproduce a different half of the original defect.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.orchestrator_commands.cert_status import cert_status
from cli_agent_orchestrator.cli.orchestrator_commands.certify import (
    CertifyError,
    certify,
    current_sha_pair,
    read_evidence,
    write_certification_row,
)
from cli_agent_orchestrator.utils import routing
from cli_agent_orchestrator.utils.clause_lint import (
    E_CERT_BINARY_DRIFT,
    E_CERT_STALE,
    lint_certifications,
)

#: The stale sha #645 measured on the live ``general`` rows.
MEASURED_STALE_SHA = "5eb08f4f9de16738"

FAKE_BINARY_SHA = "a" * 64

_GENERAL_BODY = """\
# GENERAL

Follow the brief and report back.
"""


def _store(tmp_path: Path) -> Path:
    """A positions store with an overlays sibling, shaped like the installed one."""
    positions = tmp_path / "agent-store" / "positions"
    overlays = tmp_path / "agent-store" / "overlays"
    positions.mkdir(parents=True)
    overlays.mkdir(parents=True)
    (positions / "general.md").write_text(
        '---\nrole: developer\nskills: ["cao-worker-protocols"]\n---\n' + _GENERAL_BODY,
        encoding="utf-8",
    )
    (overlays / "kiro_cli.md").write_text("## kiro notes\nv1\n", encoding="utf-8")
    (overlays / "codex.md").write_text("## codex notes\nv1\n", encoding="utf-8")
    return positions


def _evidence(tmp_path: Path, name: str = "smoke.md") -> Path:
    path = tmp_path / name
    path.write_text(
        "# AC15 cell smoke (general, kiro_cli)\n"
        "$ cao-mcp-server assign --position general --provider kiro_cli\n"
        "HTTP 201 terminal 4f21ab90; callback round-trip in 9s (bound 300s)\n",
        encoding="utf-8",
    )
    return path


def _rows(positions: Path, position: str = "general", block: str = "certification") -> list:
    parsed = frontmatter.loads((positions / f"{position}.md").read_text(encoding="utf-8"))
    return list(parsed.metadata.get(block) or [])


def _write_stale_row(positions: Path, provider: str, outcome: str = "PASS") -> None:
    """Put a row at #645's measured stale sha, the state the fleet was actually in."""
    path = positions / "general.md"
    parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
    rows = list(parsed.metadata.get("certification") or [])
    _, ov_sha = current_sha_pair("general", provider, positions)
    rows.append(
        {
            "provider": provider,
            "position_sha": MEASURED_STALE_SHA,
            "overlay_sha": ov_sha,
            "outcome": outcome,
            "date": "2026-08-29",
        }
    )
    parsed.metadata["certification"] = rows
    path.write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# ARM 1 — the #645 measurement, reproduced and then closed
# --------------------------------------------------------------------------


def test_f788_the_645_measurement_is_detected_and_fixed_by_the_verbs(tmp_path: Path) -> None:
    """The measured state: two PASS rows at a stale sha, silently refusing every assign."""
    positions = _store(tmp_path)
    _write_stale_row(positions, "kiro_cli")
    _write_stale_row(positions, "codex")

    # (a) The reader's verdict is #645's: certified-looking rows, UNCERTIFIED cells.
    assert routing.cell_certified("general", "kiro_cli", positions) == (False, "UNCERTIFIED")
    assert routing.cell_certified("general", "codex", positions) == (False, "UNCERTIFIED")

    # (b) The detector — which did not exist — names both rows and exits non-zero.
    result = CliRunner().invoke(cert_status, ["--positions-dir", str(positions)])
    assert result.exit_code == 1, result.output
    assert result.output.count(E_CERT_STALE) == 2, result.output
    assert MEASURED_STALE_SHA in result.output
    for provider in ("kiro_cli", "codex"):
        assert f"(general, {provider})" in result.output

    # (c) The writer closes it, and the reader now agrees — without a second row.
    for provider in ("kiro_cli", "codex"):
        written = CliRunner().invoke(
            certify,
            [
                "--position",
                "general",
                "--provider",
                provider,
                "--positions-dir",
                str(positions),
                "--evidence",
                str(_evidence(tmp_path, f"smoke-{provider}.md")),
            ],
        )
        assert written.exit_code == 0, written.output
    assert routing.cell_certified("general", "kiro_cli", positions) == (True, "PASS")
    assert routing.cell_certified("general", "codex", positions) == (True, "PASS")
    assert len(_rows(positions)) == 2, _rows(positions)

    clean = CliRunner().invoke(cert_status, ["--positions-dir", str(positions)])
    assert clean.exit_code == 0, clean.output
    assert "all rows current" in clean.output


# --------------------------------------------------------------------------
# ARM 2 — the writer refuses without evidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,code",
    [
        (None, "E-EVIDENCE-MISSING"),
        ("", "E-EVIDENCE-EMPTY"),
        ("   \n\n", "E-EVIDENCE-EMPTY"),
        ("the cell works fine, I checked\n", "E-EVIDENCE-NO-COMMAND"),
        ("$ cao-mcp-server assign --position general\n", "E-EVIDENCE-NO-OUTPUT"),
        ("command: pytest -k smoke\n", "E-EVIDENCE-NO-OUTPUT"),
    ],
)
def test_f788_writer_refuses_without_command_and_output(tmp_path: Path, content, code: str) -> None:
    positions = _store(tmp_path)
    path = tmp_path / "ev.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(CertifyError) as excinfo:
        read_evidence(path)
    assert excinfo.value.code == code

    result = CliRunner().invoke(
        certify,
        [
            "--position",
            "general",
            "--provider",
            "kiro_cli",
            "--positions-dir",
            str(positions),
            "--evidence",
            str(path),
        ],
    )
    assert result.exit_code != 0
    assert code in result.output
    assert _rows(positions) == [], "a refused certification must write nothing"


def test_f788_evidence_is_required_by_the_cli_signature(tmp_path: Path) -> None:
    """Not merely validated — omitting ``--evidence`` cannot even parse."""
    positions = _store(tmp_path)
    result = CliRunner().invoke(
        certify,
        ["--position", "general", "--provider", "kiro_cli", "--positions-dir", str(positions)],
    )
    assert result.exit_code != 0
    assert "--evidence" in result.output


def test_f788_fenced_block_counts_as_a_command_citation(tmp_path: Path) -> None:
    """The evidence shape the H2 AC15 reports actually use (report.md fenced output)."""
    path = tmp_path / "report.md"
    path.write_text(
        "## Cell (general, kiro_cli)\n\n```\ncell create HTTP 201 terminal=bd5ccfa9\n"
        "delivery_msg 01M2NZ… mb_28ecb07a<-bd5ccfa9\n```\n",
        encoding="utf-8",
    )
    evidence = read_evidence(path)
    assert "cell create HTTP 201" in evidence.command
    assert evidence.sha256 and evidence.path == path.resolve()


def test_f788_row_cites_the_evidence_file_and_its_digest(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    ev_path = _evidence(tmp_path)
    row = write_certification_row(
        positions, "general", "kiro_cli", evidence=read_evidence(ev_path), date="2026-09-16"
    )
    assert str(ev_path) in row["evidence"]
    assert "sha256=" in row["evidence"]
    assert "cao-mcp-server assign" in row["evidence"]


# --------------------------------------------------------------------------
# ARM 3 — idempotent rewrite
# --------------------------------------------------------------------------


def test_f788_second_certification_replaces_the_row_in_place(tmp_path: Path) -> None:
    """One row per cell per axis. An appended twin would be UNREACHABLE…

    …because the reader stops at the first matching row, so an appended
    re-certification of a FAIL cell would never be seen. Replacement is the only
    correctness-preserving update.
    """
    positions = _store(tmp_path)
    ev = read_evidence(_evidence(tmp_path))

    write_certification_row(
        positions, "general", "kiro_cli", outcome="FAIL", evidence=ev, date="2026-09-15"
    )
    assert routing.cell_certified("general", "kiro_cli", positions) == (False, "FAIL")

    write_certification_row(
        positions, "general", "kiro_cli", outcome="PASS", evidence=ev, date="2026-09-16"
    )
    rows = _rows(positions)
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "PASS" and rows[0]["date"] == "2026-09-16"
    assert routing.cell_certified("general", "kiro_cli", positions) == (True, "PASS")


def test_f788_a_row_for_another_provider_is_left_alone(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    ev = read_evidence(_evidence(tmp_path))
    write_certification_row(positions, "general", "codex", evidence=ev, date="2026-09-16")
    before = _rows(positions)[0]
    write_certification_row(positions, "general", "kiro_cli", evidence=ev, date="2026-09-16")
    rows = _rows(positions)
    assert len(rows) == 2
    assert rows[0] == before, "an unrelated cell's row was rewritten"


def test_f788_writer_touches_only_its_own_block(tmp_path: Path) -> None:
    """A whole-file frontmatter round-trip would re-flow bytes this command does not own."""
    positions = _store(tmp_path)
    path = positions / "general.md"
    original = path.read_text(encoding="utf-8")
    write_certification_row(
        positions, "general", "kiro_cli", evidence=read_evidence(_evidence(tmp_path))
    )
    after = path.read_text(encoding="utf-8")
    # Every original line survives verbatim; only the new block is added.
    for line in original.splitlines():
        assert line in after.splitlines(), f"writer perturbed: {line!r}"
    assert 'skills: ["cao-worker-protocols"]' in after


def test_f788_writing_a_row_does_not_shift_position_sha(tmp_path: Path) -> None:
    """The row must not invalidate the pair it records (``_POSITION_SHA_EXCLUDE``)."""
    positions = _store(tmp_path)
    before = current_sha_pair("general", "kiro_cli", positions)
    write_certification_row(
        positions, "general", "kiro_cli", evidence=read_evidence(_evidence(tmp_path))
    )
    assert current_sha_pair("general", "kiro_cli", positions) == before


def test_f788_certifies_a_position_file_with_no_frontmatter(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    (positions / "general.md").write_text(_GENERAL_BODY, encoding="utf-8")
    write_certification_row(
        positions, "general", "kiro_cli", evidence=read_evidence(_evidence(tmp_path))
    )
    assert routing.cell_certified("general", "kiro_cli", positions) == (True, "PASS")
    assert _GENERAL_BODY in (positions / "general.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# ARM 4 — the detector flags a fragment edit and clears after re-certify
# --------------------------------------------------------------------------


@pytest.mark.parametrize("leg", ["position", "overlay"])
def test_f788_detector_flags_a_fragment_edit_then_clears(tmp_path: Path, leg: str) -> None:
    positions = _store(tmp_path)
    ev_path = _evidence(tmp_path)
    write_certification_row(
        positions, "general", "kiro_cli", evidence=read_evidence(ev_path), date="2026-09-16"
    )
    assert CliRunner().invoke(cert_status, ["--positions-dir", str(positions)]).exit_code == 0

    if leg == "position":
        path = positions / "general.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nOne more instruction.\n", "utf-8")
    else:
        (positions.parent / "overlays" / "kiro_cli.md").write_text("## kiro notes\nv2\n", "utf-8")

    drifted = CliRunner().invoke(cert_status, ["--positions-dir", str(positions)])
    assert drifted.exit_code == 1, drifted.output
    assert E_CERT_STALE in drifted.output
    assert routing.cell_certified("general", "kiro_cli", positions)[0] is False

    recert = CliRunner().invoke(
        certify,
        [
            "--position",
            "general",
            "--provider",
            "kiro_cli",
            "--positions-dir",
            str(positions),
            "--evidence",
            str(ev_path),
        ],
    )
    assert recert.exit_code == 0, recert.output
    # The stale row is REPLACED, not joined: a leftover row at the old pair would
    # keep the detector red forever and retrain everyone to ignore it.
    assert len(_rows(positions)) == 1, _rows(positions)
    assert CliRunner().invoke(cert_status, ["--positions-dir", str(positions)]).exit_code == 0
    assert routing.cell_certified("general", "kiro_cli", positions) == (True, "PASS")


def test_f788_warn_only_reports_the_same_drift_and_exits_zero(tmp_path: Path) -> None:
    """The pre-commit mode: the hook warns on a profiles/ edit; block is a later ratchet."""
    positions = _store(tmp_path)
    _write_stale_row(positions, "kiro_cli")
    result = CliRunner().invoke(cert_status, ["--positions-dir", str(positions), "--warn-only"])
    assert result.exit_code == 0, result.output
    assert E_CERT_STALE in result.output


def test_f788_position_filter_scopes_the_sweep(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    (positions / "dev.md").write_text("---\nrole: developer\n---\n# DEV\nbody\n", "utf-8")
    _write_stale_row(positions, "kiro_cli")
    scoped = CliRunner().invoke(
        cert_status, ["--positions-dir", str(positions), "--position", "dev"]
    )
    assert scoped.exit_code == 0, scoped.output
    wide = CliRunner().invoke(cert_status, ["--positions-dir", str(positions)])
    assert wide.exit_code == 1


# --------------------------------------------------------------------------
# ARM 5 — the herdr axis (block, binary pin, drift)
# --------------------------------------------------------------------------


def test_f788_herdr_axis_writes_the_backend_block_with_its_pin(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    row = write_certification_row(
        positions,
        "general",
        "kiro_cli",
        axis="herdr",
        evidence=read_evidence(_evidence(tmp_path)),
        herdr_sha256=FAKE_BINARY_SHA,
        herdr_version="0.9.0",
        protocol=22,
        date="2026-09-16",
    )
    assert row["herdr_sha256"] == FAKE_BINARY_SHA
    assert _rows(positions, block="certification") == [], "the provider axis was written too"
    assert len(_rows(positions, block="herdr_certification")) == 1
    assert routing.herdr_cell_certified(
        "general", "kiro_cli", positions, installed_sha256=FAKE_BINARY_SHA
    ) == (True, "PASS")


def test_f788_herdr_axis_refuses_when_the_binary_cannot_be_seen(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    with pytest.raises(CertifyError) as excinfo:
        write_certification_row(
            positions,
            "general",
            "kiro_cli",
            axis="herdr",
            evidence=read_evidence(_evidence(tmp_path)),
        )
    assert excinfo.value.code == "E-HERDR-BINARY-UNKNOWN"
    assert _rows(positions, block="herdr_certification") == []


def test_f788_detector_flags_a_binary_upgrade_on_a_sha_current_row(tmp_path: Path) -> None:
    """The herdr axis's second staleness: fragments unchanged, runtime replaced."""
    positions = _store(tmp_path)
    write_certification_row(
        positions,
        "general",
        "kiro_cli",
        axis="herdr",
        evidence=read_evidence(_evidence(tmp_path)),
        herdr_sha256=FAKE_BINARY_SHA,
    )
    assert lint_certifications(positions, installed_herdr_sha256=FAKE_BINARY_SHA) == []

    findings = lint_certifications(positions, installed_herdr_sha256="b" * 64)
    assert [f.code for f in findings] == [E_CERT_BINARY_DRIFT], [f.message() for f in findings]
    assert findings[0].axis == "herdr"

    # An UNRESOLVABLE binary is not a drift claim: "cannot see it" != "it changed".
    assert lint_certifications(positions, installed_herdr_sha256=None) == []


def test_f788_sweep_covers_both_axes(tmp_path: Path) -> None:
    positions = _store(tmp_path)
    ev = read_evidence(_evidence(tmp_path))
    write_certification_row(positions, "general", "kiro_cli", evidence=ev)
    write_certification_row(
        positions,
        "general",
        "kiro_cli",
        axis="herdr",
        evidence=ev,
        herdr_sha256=FAKE_BINARY_SHA,
    )
    (positions.parent / "overlays" / "kiro_cli.md").write_text("## kiro notes\nv2\n", "utf-8")

    both = lint_certifications(positions, installed_herdr_sha256=FAKE_BINARY_SHA)
    assert sorted(f.axis for f in both) == ["herdr", "provider"], [f.message() for f in both]
    assert all(f.code == E_CERT_STALE for f in both)
    one = lint_certifications(positions, axis="provider", installed_herdr_sha256=FAKE_BINARY_SHA)
    assert [f.axis for f in one] == ["provider"]


# --------------------------------------------------------------------------
# MUTANTS — each disables one property; all three must turn the arms RED
# --------------------------------------------------------------------------


def test_f788_mutant_writer_that_skips_the_sha_pin(tmp_path: Path) -> None:
    """A writer that records a FIXED sha pair rather than computing the current one.

    This is the original defect in writer form: the row looks certified and the
    reader never matches it.
    """
    positions = _store(tmp_path)
    path = positions / "general.md"
    parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
    parsed.metadata["certification"] = [
        {
            "provider": "kiro_cli",
            "position_sha": MEASURED_STALE_SHA,  # not computed — pinned by hand
            "overlay_sha": "e3b0c44298fc1c14",
            "outcome": "PASS",
            "date": "2026-09-16",
        }
    ]
    path.write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")

    assert routing.cell_certified("general", "kiro_cli", positions) == (False, "UNCERTIFIED")
    assert CliRunner().invoke(cert_status, ["--positions-dir", str(positions)]).exit_code == 1
    with pytest.raises(AssertionError):
        assert routing.cell_certified("general", "kiro_cli", positions) == (True, "PASS")


def test_f788_mutant_writer_that_appends_instead_of_replacing(tmp_path: Path) -> None:
    """Appending leaves a duplicate at the same key; the FIRST row decides the cell."""
    positions = _store(tmp_path)
    ev = read_evidence(_evidence(tmp_path))
    write_certification_row(positions, "general", "kiro_cli", outcome="FAIL", evidence=ev)

    # The mutant: append the re-certification instead of replacing in place.
    path = positions / "general.md"
    parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
    rows = list(parsed.metadata.get("certification") or [])
    twin = dict(rows[0])
    twin["outcome"] = "PASS"
    parsed.metadata["certification"] = rows + [twin]
    path.write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")

    assert len(_rows(positions)) == 2
    # The PASS is unreachable: the reader stops at the first matching row.
    assert routing.cell_certified("general", "kiro_cli", positions) == (False, "FAIL")
    with pytest.raises(AssertionError):
        assert len(_rows(positions)) == 1

    # The real writer, on the same sequence, leaves one row and the cell PASSes.
    write_certification_row(positions, "general", "kiro_cli", outcome="PASS", evidence=ev)
    assert len(_rows(positions)) == 1
    assert routing.cell_certified("general", "kiro_cli", positions) == (True, "PASS")


def test_f788_mutant_detector_that_ignores_overlay_sha(tmp_path: Path) -> None:
    """A detector comparing only ``position_sha`` misses every overlay-only edit."""
    positions = _store(tmp_path)
    write_certification_row(
        positions, "general", "kiro_cli", evidence=read_evidence(_evidence(tmp_path))
    )
    (positions.parent / "overlays" / "kiro_cli.md").write_text("## kiro notes\nv2\n", "utf-8")

    def _mutant_lint(store: Path) -> list:
        findings = []
        for pos_path in sorted(store.glob("*.md")):
            parsed = frontmatter.loads(pos_path.read_text(encoding="utf-8"))
            for row in parsed.metadata.get("certification") or []:
                cur_pos, _cur_ov = current_sha_pair(pos_path.stem, row["provider"], store)
                if str(row.get("position_sha")) != cur_pos:  # overlay leg dropped
                    findings.append(row)
        return findings

    assert _mutant_lint(positions) == [], "fixture did not isolate the overlay leg"
    real = lint_certifications(positions)
    assert [f.code for f in real] == [E_CERT_STALE], [f.message() for f in real]
    # …and the cell the mutant calls healthy is refused at assign time.
    assert routing.cell_certified("general", "kiro_cli", positions)[0] is False
    with pytest.raises(AssertionError):
        assert _mutant_lint(positions), "the mutant detector must stay blind"
