"""``cao-orchestrator gate-check`` — the gate transaction and producer contract.

F809 #666 ASK A01 (``#672``), batch B2. The "encode by default" ruling (user,
2026-09-16) converts the interim prose rules that told a lane to *remember* the
gate transaction into a deterministic check over repo state: the report exists,
its bytes are the bytes the callback attested, the live-ledger row it claims is
real and still open, and every dependency it declares resolves.

Why it lives here and not on base ``cao``: it reads skill-owned knowledge paths
(``orchestrator/HANDOFF.md``, the pinned artifact tree), which
``test/architecture/test_no_knowledge_path_reads.py`` allows only under
``cli/orchestrator_commands/``.

Why ``gate-check`` and not the blueprint's literal ``gate check``
----------------------------------------------------------------
The r4 row spells the verb ``cao-orchestrator gate check``. Base ``cao`` already
carries a *different* ``gate`` command (WP-ARCH Amendment A slice 2a, the
read-only gate run view), and ``test/cli/test_lite_command_surface.py`` asserts
name-level disjointness in both directions — a skill group named ``gate`` would
make the surface test unsatisfiable and, worse, give two different commands the
same name across the two console scripts. The verb is therefore flat, exactly as
``lint-doctrine`` and ``fold-corpus`` are.

What it does NOT do
-------------------
It does not re-implement the canonical report digest. #672's owner-scope is
explicit — "add report-before-callback and required entry-point coverage, not
duplicate hash logic" — so the attestation check SHELLS OUT to
``scripts/report-attest.sh --check``, the tool that defines the digest, and
reads its exit code. Header generation likewise stays with that script (it
inserts and refreshes ``Report-SHA256:``); this command verifies, never writes.
Callback ORDERING at the runtime seam stays where r4 left it (the frozen
authority pin in the server, and the merge-time report-drift refusal in
``scripts/gated-merge.sh``); what is mechanized here is the producer-side state a
supervisor can check before accepting a verdict.

Every mechanism id this module has to NAME is named in a ``#`` comment, never in
a docstring: this file is a mechanism source for C3, and a docstring line is not
a comment line, so an id spelled here in prose would capture the id's resolution
from the code that really implements it.

Checks
------
G1  report: the path the callback names is a non-empty regular file.
G2  hash: the report is attested and self-consistent (CRD == declared
    ``Report-SHA256``), an explicitly supplied ``--callback-hash`` equals that
    declared value, and the pinned artifact's sha256 equals ``Artifact-SHA256``.
G3  ledger row: every ``Ledger-Row:`` the report declares resolves to a row in
    the live ledger, and that row is still open — a drained/verified row means
    the verdict it carries was already consumed, so a new one is stale.
G4  dependency: every ``Depends-On: <path>[@<sha256>]`` resolves to a file that
    exists and, when a digest is declared, still hashes to it.

Untrusted state never reads as a pass. A declared ledger row whose ledger file
is unreadable, or a declared artifact whose tree is missing, is a FINDING — the
defect this batch exists to stop is a check that exits 0 because it could not
run.

Exit codes
----------
0  clean.
1  one or more findings — a prerequisite is missing or invalid.
2  usage or internal error, in every path, so a broken invocation can never be
   mistaken for a pass.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click

from cli_agent_orchestrator.cli.orchestrator_commands.ledger import (
    DRAINED_STATUSES,
    PENDING_STATUSES,
    live_ledger_rows,
)

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_INTERNAL = 2

DEFAULT_LEDGER = "orchestrator/HANDOFF.md"
DEFAULT_ATTEST_TOOL = "scripts/report-attest.sh"

# Mirrors ``scripts/report-attest.sh`` check_header_contract(): the header window
# is line 1 through the first bare ``---`` delimiter below line 1, or line 40,
# whichever comes first. Parsing the same window is what makes a finding here and
# a refusal there describe the same report.
HEADER_WINDOW_MAX = 40
DELIMITER_RE = re.compile(r"^---\s*$")
# F746 (#603): keys are bold-optional — ``**Key:** value`` and ``Key: value`` are
# the same header key. ``Key=value`` is not a header key.
HEADER_KEY_RE = re.compile(r"^(?:\*\*)?([A-Za-z][A-Za-z0-9-]*)(?::\*\*|:)\s*(.*?)\s*$")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# ``Depends-On: <path>`` or ``Depends-On: <path>@<sha256>``. The digest is split
# off the RIGHT, so a path containing '@' still parses.
DEPENDENCY_RE = re.compile(r"^(?P<path>.+?)(?:@(?P<sha>[0-9a-fA-F]{64}))?$")


class GateCheckError(Exception):
    """Usage or internal failure — always exit 2, never a pass."""


@dataclass(frozen=True, order=True)
class Finding:
    check: str
    subject: str
    code: str
    detail: str


@dataclass
class GateResult:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        """Record a finding once per event.

        #672's acceptance asks for warnings deduplicated per event: a report that
        declares the same dependency twice, or two header spellings of one key,
        describes ONE broken prerequisite and must not be reported twice.
        """
        if finding not in self.findings:
            self.findings.append(finding)


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------
def header_window(text: str) -> list[str]:
    """The lines ``report-attest.sh`` treats as the header."""
    window: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if number > 1 and DELIMITER_RE.match(line):
            break
        window.append(line)
        if number >= HEADER_WINDOW_MAX:
            break
    return window


def parse_header(text: str) -> dict[str, list[str]]:
    """``{key: [value, ...]}`` for the header window, keys case-normalized.

    Values are a LIST because ``Depends-On`` and ``Ledger-Row`` are repeatable;
    single-valued keys simply carry one entry.
    """
    header: dict[str, list[str]] = {}
    for line in header_window(text):
        match = HEADER_KEY_RE.match(line)
        if match is None:
            continue
        key, value = match.group(1).lower(), match.group(2)
        if value:
            header.setdefault(key, []).append(value)
    return header


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# G1 report
# --------------------------------------------------------------------------
def check_report(report: Path) -> list[Finding]:
    subject = report.as_posix()
    if not report.exists():
        return [
            Finding(
                "G1",
                subject,
                "E-REPORT-MISSING",
                "the callback names a report that does not exist; write and attest the "
                "report BEFORE emitting the verdict",
            )
        ]
    if not report.is_file():
        return [Finding("G1", subject, "E-REPORT-MISSING", "not a regular file")]
    if report.stat().st_size == 0:
        return [Finding("G1", subject, "E-REPORT-MISSING", "report is empty")]
    return []


# --------------------------------------------------------------------------
# G2 hash
# --------------------------------------------------------------------------
def check_hashes(
    report: Path,
    header: dict[str, list[str]],
    workspace: Path,
    callback_hash: str | None,
    attest_tool: Path,
) -> list[Finding]:
    findings: list[Finding] = []
    subject = report.as_posix()

    declared = header.get("report-sha256", [])
    if not declared:
        findings.append(
            Finding(
                "G2",
                subject,
                "E-REPORT-UNATTESTED",
                f"no Report-SHA256 header; run `{attest_tool.as_posix()} {report}` first",
            )
        )
    else:
        # The digest is defined by report-attest.sh, so ASK it rather than
        # recomputing the elision rule here (#672 owner-scope).
        if not attest_tool.is_file():
            raise GateCheckError(f"attestation tool not found: {attest_tool}")
        try:
            attested = subprocess.run(
                [str(attest_tool), "--check", str(report.resolve())],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:  # pragma: no cover - exec failure
            raise GateCheckError(f"cannot run {attest_tool}: {error}") from error
        if attested.returncode == EXIT_INTERNAL:
            raise GateCheckError(
                f"{attest_tool.name} --check refused the report: "
                f"{attested.stderr.strip() or attested.stdout.strip()}"
            )
        if attested.returncode != 0:
            findings.append(
                Finding(
                    "G2",
                    subject,
                    "E-REPORT-DRIFT",
                    "report bytes no longer hash to the attested digest "
                    f"({attested.stdout.strip()}); the report was edited after attestation",
                )
            )
        if callback_hash is not None and callback_hash != declared[0]:
            findings.append(
                Finding(
                    "G2",
                    subject,
                    "E-REPORT-DRIFT",
                    f"callback carries {callback_hash}, the report declares {declared[0]}",
                )
            )

    findings.extend(_check_artifact_pin(header, workspace))
    return findings


def _check_artifact_pin(header: dict[str, list[str]], workspace: Path) -> list[Finding]:
    repo_path = header.get("artifact-repo-path", [])
    declared = header.get("artifact-sha256", [])
    if not repo_path or not declared:
        return []
    subject = repo_path[0]
    pinned = workspace / repo_path[0]
    if not pinned.is_file():
        return [
            Finding(
                "G2",
                subject,
                "E-ARTIFACT-ABSENT",
                f"the pinned artifact is not in the tree at {workspace.as_posix()}",
            )
        ]
    actual = sha256_of(pinned)
    if actual != declared[0]:
        return [
            Finding(
                "G2",
                subject,
                "E-ARTIFACT-DRIFT",
                f"pinned {declared[0]}, tree holds {actual}",
            )
        ]
    return []


# --------------------------------------------------------------------------
# G3 ledger row
# --------------------------------------------------------------------------
def check_ledger_rows(
    header: dict[str, list[str]], ledger_path: Path, ledger_label: str
) -> list[Finding]:
    claimed = header.get("ledger-row", [])
    if not claimed:
        return []
    findings: list[Finding] = []
    if not ledger_path.is_file():
        # Untrusted state is a finding, never a pass: the row cannot be shown to
        # exist, so the transaction cannot be shown to be complete.
        return [
            Finding(
                "G3",
                ledger_label,
                "E-LEDGER-UNREADABLE",
                f"report claims {', '.join(claimed)} but the live ledger is missing",
            )
        ]
    try:
        rows = live_ledger_rows(ledger_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise GateCheckError(f"cannot read {ledger_path}: {error}") from error
    if rows is None:
        return [
            Finding(
                "G3",
                ledger_label,
                "E-LEDGER-UNREADABLE",
                "no live ledger section in the file",
            )
        ]
    by_feature = {feature: status.strip().lower() for feature, status in rows}
    for feature in claimed:
        status = by_feature.get(feature)
        if status is None:
            findings.append(
                Finding(
                    "G3",
                    feature,
                    "E-LEDGER-ROW-ABSENT",
                    f"no row for this feature in {ledger_label}; append the ledger entry "
                    "before the verdict is accepted",
                )
            )
        elif status in DRAINED_STATUSES:
            findings.append(
                Finding(
                    "G3",
                    feature,
                    "E-LEDGER-ROW-STALE",
                    f"row is already '{status}' — its verdict was consumed; a new verdict "
                    "against a drained row is stale",
                )
            )
        elif status not in PENDING_STATUSES:
            findings.append(
                Finding(
                    "G3",
                    feature,
                    "E-LEDGER-ROW-STALE",
                    f"unrecognized ledger status '{status}' — state is untrusted",
                )
            )
    return findings


# --------------------------------------------------------------------------
# G4 declared dependency
# --------------------------------------------------------------------------
def check_dependencies(header: dict[str, list[str]], workspace: Path) -> list[Finding]:
    findings: list[Finding] = []
    for raw in header.get("depends-on", []):
        match = DEPENDENCY_RE.match(raw.strip())
        if match is None or not match.group("path").strip():
            findings.append(
                Finding("G4", raw, "E-DEPENDENCY-MALFORMED", "expected `<path>[@<sha256>]`")
            )
            continue
        declared_path = match.group("path").strip()
        declared_sha = (match.group("sha") or "").lower()
        target = Path(declared_path)
        if not target.is_absolute():
            target = workspace / target
        if not target.is_file():
            findings.append(
                Finding(
                    "G4",
                    declared_path,
                    "E-DEPENDENCY-MISSING",
                    "declared gate dependency does not exist; the gate was dispatched "
                    "before its prerequisite landed",
                )
            )
            continue
        if declared_sha:
            actual = sha256_of(target)
            if actual != declared_sha:
                findings.append(
                    Finding(
                        "G4",
                        declared_path,
                        "E-DEPENDENCY-DRIFT",
                        f"pinned {declared_sha}, tree holds {actual}",
                    )
                )
    return findings


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def check_required(header: dict[str, list[str]], required: tuple[str, ...]) -> list[Finding]:
    """Every ``--require KEY`` must be declared, so a tier can demand a key."""
    findings: list[Finding] = []
    for key in required:
        if not header.get(key.lower()):
            findings.append(
                Finding(
                    "G0",
                    key,
                    "E-CONTRACT-INCOMPLETE",
                    "required header key is absent from the report's header window",
                )
            )
    return findings


def run_gate_check(
    report: Path,
    workspace: Path,
    ledger: Path | None,
    attest_tool: Path | None,
    callback_hash: str | None,
    required: tuple[str, ...] = (),
) -> GateResult:
    result = GateResult()
    if callback_hash is not None and not SHA256_RE.match(callback_hash):
        raise GateCheckError(f"--callback-hash is not 64 lowercase hex: {callback_hash}")

    missing_report = check_report(report)
    if missing_report:
        # Everything downstream reads the report; reporting four consequences of
        # one absent file is the duplicate-warning defect, not extra coverage.
        for finding in missing_report:
            result.add(finding)
        result.notes.append("G2/G3/G4 not run: the report is unavailable")
        return result

    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise GateCheckError(f"cannot read {report}: {error}") from error
    header = parse_header(text)

    ledger_rel = (ledger or Path(DEFAULT_LEDGER)).as_posix()
    ledger_path = ledger if ledger and ledger.is_absolute() else workspace / ledger_rel
    tool = (
        attest_tool
        if attest_tool and attest_tool.is_absolute()
        else workspace / (attest_tool.as_posix() if attest_tool else DEFAULT_ATTEST_TOOL)
    )

    for finding in check_required(header, required):
        result.add(finding)
    for finding in check_hashes(report, header, workspace, callback_hash, tool):
        result.add(finding)
    for finding in check_ledger_rows(header, ledger_path, ledger_rel):
        result.add(finding)
    for finding in check_dependencies(header, workspace):
        result.add(finding)
    return result


def exit_code_for(result: GateResult) -> int:
    return EXIT_VIOLATION if result.findings else EXIT_OK


def render_report(result: GateResult, report: Path) -> str:
    lines = [
        f"gate-check: {finding.code} {finding.check} {finding.subject}: {finding.detail}"
        for finding in sorted(result.findings)
    ]
    lines.extend(f"gate-check: note {note}" for note in result.notes)
    if not result.findings:
        lines.append(f"gate-check: transaction complete ({report.as_posix()})")
    return "\n".join(lines)


@click.command("gate-check")
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path),
    required=True,
    help="The gate report the callback names.",
)
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root the pin, ledger and dependencies resolve against (default: cwd).",
)
@click.option(
    "--ledger",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Live ledger backing Ledger-Row (default: {DEFAULT_LEDGER}).",
)
@click.option(
    "--attest-tool",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Digest authority invoked with --check (default: {DEFAULT_ATTEST_TOOL}).",
)
@click.option(
    "--callback-hash",
    default=None,
    help="Report-SHA256 the callback carries; must equal the report's own.",
)
@click.option(
    "--require",
    "required",
    multiple=True,
    help="Header key the report MUST declare (repeatable), e.g. --require Ledger-Row.",
)
def gate_check(
    report_path: Path,
    workspace: Path | None,
    ledger: Path | None,
    attest_tool: Path | None,
    callback_hash: str | None,
    required: tuple[str, ...],
) -> None:
    """Verify a gate transaction: report, hash, ledger row, declared dependency."""
    root = (workspace or Path.cwd()).resolve()
    if not root.is_dir():
        click.echo(f"gate-check: E-USAGE workspace is not a directory: {root}", err=True)
        raise SystemExit(EXIT_INTERNAL)
    report = report_path if report_path.is_absolute() else Path.cwd() / report_path
    try:
        result = run_gate_check(report, root, ledger, attest_tool, callback_hash, required)
    except GateCheckError as error:
        click.echo(f"gate-check: E-USAGE {error}", err=True)
        raise SystemExit(EXIT_INTERNAL) from error
    except Exception as error:  # noqa: BLE001 - an internal crash must never pass
        click.echo(f"gate-check: E-INTERNAL {error!r}", err=True)
        raise SystemExit(EXIT_INTERNAL) from error
    click.echo(render_report(result, report))
    raise SystemExit(exit_code_for(result))
