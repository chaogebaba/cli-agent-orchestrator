"""F809 #666 ASK A01 (#672) — ``cao-orchestrator gate-check``.

Every check has an EXECUTED arm over a real workspace on disk, driven through the
real Click command, with the attestation authority invoked as a real subprocess.
The fixture ships a STUB attestation tool rather than importing the root repo's
``scripts/report-attest.sh``: the fork has to be testable from an unpacked sdist
with no sibling checkout. The stub implements the same two-mode contract (attest,
``--check``) and the same exit codes, which is what this command depends on.

The mutation arms are the point of the file. Each replays a defect this build
exists to stop — a finding nothing acts on, a missing prerequisite that reads as
a pass, an unreadable ledger that reads as a pass, a subprocess whose exit code
is ignored, and a duplicate warning per event — and each must come back RED.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.orchestrator_commands import gate_check as module
from cli_agent_orchestrator.cli.orchestrator_commands.gate_check import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_VIOLATION,
    Finding,
    GateCheckError,
    gate_check,
    header_window,
    parse_header,
    run_gate_check,
)

# Mirrors scripts/report-attest.sh: elide every Report-SHA256 line, digest the
# rest, insert the line after the last header key, and exit 0/1 from --check.
STUB_ATTEST = """#!/usr/bin/env python3
import hashlib, re, sys

SELF = re.compile(rb"^(\\*\\*)?Report-SHA256(:\\*\\*|:)[ \\t]")
KEY = re.compile(rb"^(\\*\\*)?(Artifact-[A-Za-z-]+|Git-SHA[a-z-]*|Ruling|Verdict)(:\\*\\*|:)")


def crd(lines):
    return hashlib.sha256(b"".join(l for l in lines if not SELF.match(l))).hexdigest()


args = sys.argv[1:]
check = args[:1] == ["--check"]
if check:
    args = args[1:]
if len(args) != 1 or not args[0].startswith("/"):
    sys.stderr.write("E-USAGE absolute path required\\n")
    sys.exit(2)
path = args[0]
try:
    lines = open(path, "rb").readlines()
except OSError as err:
    sys.stderr.write("E-USAGE %s\\n" % err)
    sys.exit(2)
digest = crd(lines)
if check:
    declared = [l for l in lines if SELF.match(l)]
    value = SELF.sub(b"", declared[0], count=1).strip().decode() if declared else "<absent>"
    print("CRD=%s SELF=%s" % (digest, value))
    sys.exit(0 if digest == value else 1)
kept = [l for l in lines if not SELF.match(l)]
last = 0
for i, line in enumerate(kept[:40]):
    if KEY.match(line):
        last = i + 1
kept.insert(last, b"**Report-SHA256:** %s\\n" % digest.encode())
open(path, "wb").write(b"".join(kept))
print("Report-SHA256: %s" % digest)
"""

LEDGER = """\
# HANDOFF

## POST-RESTART RE-ENTRY

nothing pending.

## Live ledger

| Feature | Lane | Sha | Date | Status |
|---|---|---|---|---|
| F815-gate-check | opus | abc1234 | 2026-09-16 | pending |
| F800-old | opus | def5678 | 2026-09-01 | drained-pass |
| F801-weird | opus | 0123456 | 2026-09-02 | banana |
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace(tmp_path: Path, *, ledger: str | None = LEDGER) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "orchestrator").mkdir(parents=True)
    tool = root / "scripts/report-attest.sh"
    tool.write_text(STUB_ATTEST, encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    (root / "orchestrator/artifact.md").write_text("the pinned artifact\n", encoding="utf-8")
    if ledger is not None:
        (root / "orchestrator/HANDOFF.md").write_text(ledger, encoding="utf-8")
    return root


def _report(
    tmp_path: Path,
    root: Path,
    *,
    extra: str = "",
    attest: bool = True,
    artifact_sha: str | None = None,
    body: str = "\n---\n\nGATE-YES, with reasons.\n",
) -> Path:
    """A canonical F244 report OUTSIDE the repo, attested by the stub tool."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    report = reports / "verdict.md"
    pinned = artifact_sha if artifact_sha is not None else _sha(root / "orchestrator/artifact.md")
    report.write_text(
        "**Ruling:** GATE-YES\n"
        f"**Artifact-Path:** {root / 'orchestrator/artifact.md'}\n"
        f"**Artifact-SHA256:** {pinned}\n"
        "**Artifact-Repo-Path:** orchestrator/artifact.md\n"
        "**Git-SHA-fork:** " + "a" * 40 + "\n" + extra + body,
        encoding="utf-8",
    )
    if attest:
        rc = os.system(f"{root / 'scripts/report-attest.sh'} {report}")
        assert rc == 0, "fixture failed to attest"
    return report


def _declared_hash(report: Path) -> str:
    for line in report.read_text(encoding="utf-8").splitlines():
        if line.startswith("**Report-SHA256:**"):
            return line.split()[-1]
    raise AssertionError("fixture report is not attested")


def _run(root: Path, report: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        gate_check, ["--report", str(report), "--workspace", str(root), *args]
    )
    return result.exit_code, result.output


def _codes(root: Path, report: Path, **kwargs: object) -> list[str]:
    outcome = run_gate_check(report, root, None, None, None, **kwargs)  # type: ignore[arg-type]
    return sorted(finding.code for finding in outcome.findings)


# ---------------------------------------------------------------------------
# header parsing
# ---------------------------------------------------------------------------
def test_header_window_stops_at_the_first_delimiter() -> None:
    text = "**Ruling:** GATE-YES\n---\n**Ledger-Row:** hidden\n"
    assert header_window(text) == ["**Ruling:** GATE-YES"]
    assert "ledger-row" not in parse_header(text)


def test_header_window_stops_at_line_forty() -> None:
    text = "\n".join(f"**Key{n}:** v" for n in range(60))
    assert len(header_window(text)) == 40


def test_bold_and_plain_keys_are_the_same_key_and_repeat() -> None:
    header = parse_header("Depends-On: a\n**Depends-On:** b\nRuling: GATE-YES\n")
    assert header["depends-on"] == ["a", "b"]
    assert header["ruling"] == ["GATE-YES"]


# ---------------------------------------------------------------------------
# G1 report present / absent
# ---------------------------------------------------------------------------
def test_g1_pass_a_complete_transaction_exits_zero(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root, extra="**Ledger-Row:** F815-gate-check\n")
    code, output = _run(root, report)
    assert code == EXIT_OK, output
    assert "transaction complete" in output


def test_g1_absent_report_is_a_violation_not_a_crash(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    code, output = _run(root, tmp_path / "reports/never-written.md")
    assert code == EXIT_VIOLATION
    assert "E-REPORT-MISSING" in output


def test_g1_empty_report_is_a_violation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    code, output = _run(root, empty)
    assert code == EXIT_VIOLATION
    assert "E-REPORT-MISSING" in output


def test_g1_absent_report_does_not_cascade_into_three_more_findings(tmp_path: Path) -> None:
    """Warnings deduplicated per event (#672): one absent file is ONE finding."""
    root = _workspace(tmp_path)
    outcome = run_gate_check(tmp_path / "gone.md", root, None, None, None)
    assert [f.code for f in outcome.findings] == ["E-REPORT-MISSING"]
    assert outcome.notes == ["G2/G3/G4 not run: the report is unavailable"]


# ---------------------------------------------------------------------------
# G2 hash match / mismatch
# ---------------------------------------------------------------------------
def test_g2_edited_after_attestation_is_report_drift(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    report.write_text(report.read_text(encoding="utf-8") + "one more paragraph\n", encoding="utf-8")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-REPORT-DRIFT" in output


def test_g2_unattested_report_is_named_as_such(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root, attest=False)
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-REPORT-UNATTESTED" in output


def test_g2_callback_hash_must_equal_the_reports_own(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    assert _run(root, report, "--callback-hash", _declared_hash(report))[0] == EXIT_OK
    code, output = _run(root, report, "--callback-hash", "b" * 64)
    assert code == EXIT_VIOLATION
    assert "E-REPORT-DRIFT" in output


def test_g2_artifact_pin_matches_and_drifts(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    assert _run(root, report)[0] == EXIT_OK
    (root / "orchestrator/artifact.md").write_text("edited after the pin\n", encoding="utf-8")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-ARTIFACT-DRIFT" in output


def test_g2_artifact_absent_from_the_tree(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    (root / "orchestrator/artifact.md").unlink()
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-ARTIFACT-ABSENT" in output


def test_g2_a_refusing_attestation_tool_exits_two_never_zero(tmp_path: Path) -> None:
    """The stub exits 2 on a relative path; an unusable authority is never a pass."""
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    tool = root / "scripts/report-attest.sh"
    tool.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    code, output = _run(root, report)
    assert code == EXIT_INTERNAL
    assert "E-USAGE" in output or "refused the report" in output


def test_g2_a_missing_attestation_tool_exits_two(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    (root / "scripts/report-attest.sh").unlink()
    code, _ = _run(root, report)
    assert code == EXIT_INTERNAL


# ---------------------------------------------------------------------------
# G3 ledger row present / stale
# ---------------------------------------------------------------------------
def test_g3_open_row_passes_and_absent_row_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    good = _report(tmp_path, root, extra="**Ledger-Row:** F815-gate-check\n")
    assert _run(root, good)[0] == EXIT_OK

    missing = _report(tmp_path, root, extra="**Ledger-Row:** F999-never-appended\n")
    code, output = _run(root, missing)
    assert code == EXIT_VIOLATION
    assert "E-LEDGER-ROW-ABSENT" in output


def test_g3_drained_row_is_stale(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root, extra="**Ledger-Row:** F800-old\n")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-LEDGER-ROW-STALE" in output
    assert "drained-pass" in output


def test_g3_unrecognized_status_is_untrusted_state(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root, extra="**Ledger-Row:** F801-weird\n")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-LEDGER-ROW-STALE" in output


def test_g3_missing_ledger_file_fails_instead_of_passing(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ledger=None)
    report = _report(tmp_path, root, extra="**Ledger-Row:** F815-gate-check\n")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-LEDGER-UNREADABLE" in output


def test_g3_ledger_without_a_live_section_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ledger="# HANDOFF\n\nno ledger here.\n")
    report = _report(tmp_path, root, extra="**Ledger-Row:** F815-gate-check\n")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-LEDGER-UNREADABLE" in output


def test_g3_is_not_run_when_the_report_claims_no_row(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ledger=None)
    assert _run(root, _report(tmp_path, root))[0] == EXIT_OK


# ---------------------------------------------------------------------------
# G4 declared dependency satisfied / missing
# ---------------------------------------------------------------------------
def test_g4_dependency_satisfied_with_and_without_a_pin(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    stage_a = root / "orchestrator/stage-a-ledger.md"
    stage_a.write_text("| mutant | RED |\n", encoding="utf-8")
    plain = _report(tmp_path, root, extra="**Depends-On:** orchestrator/stage-a-ledger.md\n")
    assert _run(root, plain)[0] == EXIT_OK
    pinned = _report(
        tmp_path,
        root,
        extra=f"**Depends-On:** orchestrator/stage-a-ledger.md@{_sha(stage_a)}\n",
    )
    assert _run(root, pinned)[0] == EXIT_OK


def test_g4_dependency_missing(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root, extra="**Depends-On:** orchestrator/stage-a-ledger.md\n")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-DEPENDENCY-MISSING" in output


def test_g4_dependency_drift(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    stage_a = root / "orchestrator/stage-a-ledger.md"
    stage_a.write_text("| mutant | RED |\n", encoding="utf-8")
    report = _report(
        tmp_path,
        root,
        extra=f"**Depends-On:** orchestrator/stage-a-ledger.md@{_sha(stage_a)}\n",
    )
    stage_a.write_text("| mutant | GREEN, actually |\n", encoding="utf-8")
    code, output = _run(root, report)
    assert code == EXIT_VIOLATION
    assert "E-DEPENDENCY-DRIFT" in output


def test_g4_one_declaration_repeated_produces_one_finding(tmp_path: Path) -> None:
    """Deduplicated per event: the same broken prerequisite is one finding."""
    root = _workspace(tmp_path)
    extra = "**Depends-On:** orchestrator/gone.md\nDepends-On: orchestrator/gone.md\n"
    report = _report(tmp_path, root, extra=extra)
    assert _codes(root, report) == ["E-DEPENDENCY-MISSING"]


# ---------------------------------------------------------------------------
# G0 required keys, usage
# ---------------------------------------------------------------------------
def test_g0_a_required_header_key_must_be_declared(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    code, output = _run(root, report, "--require", "Ledger-Row")
    assert code == EXIT_VIOLATION
    assert "E-CONTRACT-INCOMPLETE" in output

    declared = _report(tmp_path, root, extra="**Ledger-Row:** F815-gate-check\n")
    assert _run(root, declared, "--require", "Ledger-Row")[0] == EXIT_OK


def test_a_malformed_callback_hash_is_usage_not_a_violation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    code, output = _run(root, report, "--callback-hash", "not-a-digest")
    assert code == EXIT_INTERNAL
    assert "E-USAGE" in output


def test_a_workspace_that_is_not_a_directory_is_usage(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    result = CliRunner().invoke(
        gate_check, ["--report", str(report), "--workspace", str(tmp_path / "nope")]
    )
    assert result.exit_code == EXIT_INTERNAL


# ---------------------------------------------------------------------------
# mutation arms — each mutates the REAL module or the REAL delegate, then shows
# the arm that caught the behaviour go red.
# ---------------------------------------------------------------------------
def test_mutant_findings_not_wired_to_the_exit_code_turns_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check whose findings never reach the exit code is documentation."""
    root = _workspace(tmp_path)
    assert _run(root, tmp_path / "gone.md")[0] == EXIT_VIOLATION

    monkeypatch.setattr(module, "exit_code_for", lambda result: EXIT_OK)
    with pytest.raises(AssertionError):
        assert _run(root, tmp_path / "gone.md")[0] == EXIT_VIOLATION


def test_mutant_absent_report_reading_as_a_pass_turns_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """report-before-callback is the whole point: no file, no verdict."""
    root = _workspace(tmp_path)
    assert "E-REPORT-MISSING" in _run(root, tmp_path / "gone.md")[1]

    monkeypatch.setattr(module, "check_report", lambda report: [])
    with pytest.raises(AssertionError):
        assert "E-REPORT-MISSING" in _run(root, tmp_path / "gone.md")[1]


def test_mutant_unreadable_ledger_reading_as_a_pass_turns_red(tmp_path: Path) -> None:
    """The masked-FATAL defect class: state that could not be read is not a pass."""
    root = _workspace(tmp_path, ledger=None)
    report = _report(tmp_path, root, extra="**Ledger-Row:** F815-gate-check\n")
    assert _codes(root, report) == ["E-LEDGER-UNREADABLE"]

    def mutant(header: dict[str, list[str]], ledger_path: Path, label: str) -> list[Finding]:
        """The defect: nothing to read, nothing to complain about."""
        if not ledger_path.is_file():
            return []
        return module.check_ledger_rows(header, ledger_path, label)

    header = parse_header(report.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError):
        assert mutant(header, root / "orchestrator/HANDOFF.md", "orchestrator/HANDOFF.md")


def test_mutant_ignoring_the_attestation_exit_code_turns_red(tmp_path: Path) -> None:
    """Delegation only works if the delegate's exit code is read."""
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    report.write_text(report.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert _codes(root, report) == ["E-REPORT-DRIFT"]

    tool = root / "scripts/report-attest.sh"
    tool.write_text("#!/usr/bin/env python3\nimport sys\nprint('CRD= SELF=')\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(AssertionError):
        assert _codes(root, report) == ["E-REPORT-DRIFT"]


def test_mutant_duplicate_warnings_per_event_turns_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#672's acceptance: warnings deduplicated per event."""
    root = _workspace(tmp_path)
    extra = "**Depends-On:** orchestrator/gone.md\nDepends-On: orchestrator/gone.md\n"
    report = _report(tmp_path, root, extra=extra)
    assert _codes(root, report) == ["E-DEPENDENCY-MISSING"]

    monkeypatch.setattr(
        module.GateResult, "add", lambda self, finding: self.findings.append(finding)
    )
    with pytest.raises(AssertionError):
        assert _codes(root, report) == ["E-DEPENDENCY-MISSING"]


def test_mutant_stale_row_treated_as_live_turns_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained row's verdict was consumed; accepting a second one is the defect."""
    root = _workspace(tmp_path)
    report = _report(tmp_path, root, extra="**Ledger-Row:** F800-old\n")
    assert _codes(root, report) == ["E-LEDGER-ROW-STALE"]

    monkeypatch.setattr(module, "DRAINED_STATUSES", set())
    monkeypatch.setattr(module, "PENDING_STATUSES", {"pending", "drained-pass"})
    with pytest.raises(AssertionError):
        assert _codes(root, report) == ["E-LEDGER-ROW-STALE"]


def test_mutant_artifact_pin_never_rehashed_turns_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin nobody re-hashes cannot detect the edit it exists to detect."""
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    (root / "orchestrator/artifact.md").write_text("edited after the pin\n", encoding="utf-8")
    assert _codes(root, report) == ["E-ARTIFACT-DRIFT"]

    monkeypatch.setattr(module, "_check_artifact_pin", lambda header, workspace: [])
    with pytest.raises(AssertionError):
        assert _codes(root, report) == ["E-ARTIFACT-DRIFT"]


def test_an_internal_crash_never_reads_as_a_pass(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = _report(tmp_path, root)
    with pytest.raises(GateCheckError):
        run_gate_check(report, root, None, Path("/nonexistent/attest.sh"), None)
