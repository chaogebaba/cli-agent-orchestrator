"""``cao-orchestrator evidence verify`` — the suite/CI/live evidence verifier.

F809 #666 ASK A06 (``#668``).  Seven ledger rows carried an interim prose rule
that said, in effect, "a human checks this": the suite executor's selection
predicates (``^verification-suite-executor``), the box live-e2e round and its
change-impact matrix (``^verification-box-e2e`` / ``^verification-impact-matrix``),
the tcache force/bypass and tree-evidence rule (GATE-RULES EMPIRICAL #2 /2), the
structural verification of a CI attestation (#3 /1, ``^gates-ci-attestation``),
the mechanical mypy base/head compare (#9 /1) and the recording of the
``lint-imports`` result (#12 /1).  Under the encode-by-default ruling (user,
2026-09-16) each of those becomes a check here, and the prose keeps only the
judgment it never delegated.

Why it lives here and not on base ``cao``: it reads skill-owned knowledge paths
(``orchestrator/``, ``doctrine/``), which
``test/architecture/test_no_knowledge_path_reads.py`` allows only under
``cli/orchestrator_commands/``.

The boundary this command does NOT cross
----------------------------------------
The issue draws it explicitly: *"Keep impact, root cause, flake attribution and
independent adjudication in prose."*  So a FAILED-set difference between base and
head, or a new ``mypy`` diagnostic, is reported as a WARN and never blocks — the
reviewer rules on whether it is a regression or the F785 flake family.  What
blocks is **structurally invalid evidence**: evidence that cannot be trusted
whatever the adjudication says, because the report is missing, its attestation
does not match its bytes, a pinned artifact drifted, the run covers a tree that
is not the one under review, a declared dependency is absent, a touched provider
has no live-round row, or the ledger row that records the obligation is gone.

The manifest
------------
Evidence is declared in a TOML manifest the lane writes next to its report, and
every declaration is checked against real state (the file on disk, its sha256,
``git``, the ledger).  A declaration is never taken on trust; that is the whole
point of the verb.

.. code-block:: toml

    [evidence]
    feature = "F811"                    # required — keys the ledger row
    repo    = "fork"                    # root | fork
    base    = "<40-hex>"                # required — the base of the A/B
    head    = "<40-hex>"                # required — the tree under review
    report  = "/data/cao-scratch/f811/report.md"   # required, absolute
    ledger  = "orchestrator/HANDOFF.md" # optional; default DEFAULT_LEDGER

    [[artifact]]                        # every pinned authority artifact
    path   = "orchestrator/blueprints/f809-doctrine-mechanism-boundary.md"
    sha256 = "<64-hex>"

    [[suite]]                           # one per suite run offered as proof
    name         = "fork paired suite (head)"
    executor     = "box"                # box | ci | local
    command      = "uv run pytest -m 'not e2e and not slow'"
    tree         = "<40-hex>"           # the tree state the run covers
    exit_code    = 0
    counts       = "812 passed, 3 skipped"
    log          = "/data/cao-scratch/f811/head.log"   # box | local
    load_bearing = true
    cache        = "bypassed"           # bypassed | hit
    lease        = "0f3a…"              # box only
    # ci only: workflow, run_id, signature, conclusion, attested_at, derived_from
    # predicates: fleet_exhausted, ci_unreachable, local_reason

    [[failed_set]]                      # A/B FAILED sets, not counts
    test = "test/services/test_x.py::test_y"
    base = "fail"                       # fail | pass
    head = "fail"
    explanation = "F785 flake family"   # absent + head-only -> WARN

    [static.mypy]
    base = ["src/a.py:1: error: …"]
    head = ["src/a.py:1: error: …"]
    [static.lint_imports]
    exit_code = 0
    contract  = "skill-cli-only-via-public-api"

    [[live_round]]                      # one per provider the diff touched
    provider = "claude_code"
    box      = "grok-box-002"
    lease    = "0f3a…"
    callback = "own MCP send_message"
    log      = "/data/cao-scratch/f811/live.log"

    [live_round_waiver]                 # only when the whole fleet is down
    fleet_down = true
    substitute = "g7-sandbox"
    log        = "/data/cao-scratch/f811/g7.log"

    [[depends_on]]                      # a pinned Stage-A ledger, a prior report…
    kind   = "ledger-pin"
    path   = "/data/cao-scratch/f811/ledger-empirical-r1.md"
    sha256 = "<64-hex>"

Exit codes
----------
0  every check passed, or only WARN findings (unexplained differences).
1  at least one BLOCK finding — structurally invalid evidence.
2  usage or internal error.  **Always 2, never 0**: a missing manifest, a TOML
   syntax error or a crashed ``git`` must never read as a pass, which is the
   defect the shell tooling this replaces was measured to have.

There is no environment bypass.  ``--mode warn`` downgrades BLOCK findings for a
dry run and says so in the report, but it is an explicit argument in the command
line the reviewer quotes, not an ambient variable a lane can export.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import click

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_INTERNAL = 2

DEFAULT_LEDGER = "orchestrator/HANDOFF.md"
DEFAULT_MAX_AGE_HOURS = 72

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: ``report-attest.sh``'s Canonical Report Digest: sha256 of the file with the
#: ``Report-SHA256`` line elided, bold or plain.  Reimplemented rather than
#: shelled out to, so the verifier works on a box with no root checkout.
REPORT_SHA_RE = re.compile(rb"^(?:\*\*)?Report-SHA256(?::\*\*|:)[ \t]")

VALID_EXECUTORS = ("box", "ci", "local")
VALID_CACHE = ("bypassed", "hit")
VALID_OUTCOMES = ("pass", "fail")

#: What makes a provider's spawn/launch path "touched", from
#: ``doctrine/recipes/verification.md`` §Box live-e2e round.  The capture group
#: names the provider; a path that matches none of these touches no provider.
#: ORDER MATTERS: ``providers/kiro_capabilities.py`` is provider ``kiro``, and the
#: bare-module pattern would otherwise capture ``kiro_capabilities`` and demand a
#: live round for a provider that does not exist.
PROVIDER_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)providers/([a-z0-9_]+)_capabilities\.py$"),
    re.compile(r"(?:^|/)providers/([a-z0-9_]+)\.py$"),
    re.compile(r"(?:^|/)agent[_-]store/.*?/([a-z0-9_]+)\.(?:toml|json|md)$"),
)
#: A diff that touches the shared spawn code implicates every provider the
#: manifest itself knows about; it cannot be attributed to one.
SHARED_SPAWN_PATHS = ("terminal_service.py",)


class EvidenceError(Exception):
    """Usage or internal failure — always exit 2, never a silent pass."""


@dataclass(frozen=True, order=True)
class Finding:
    check: str
    subject: str
    detail: str
    blocking: bool = True

    def render(self, mode: str) -> str:
        label = "BLOCK" if self.blocking and mode == "block" else "WARN"
        return f"evidence: {label} {self.check} {self.subject}: {self.detail}"


@dataclass
class VerifyResult:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    matrix: dict[str, str] = field(default_factory=dict)

    def fail(self, check: str, subject: str, detail: str) -> None:
        self.findings.append(Finding(check, subject, detail))

    def warn(self, check: str, subject: str, detail: str) -> None:
        self.findings.append(Finding(check, subject, detail, blocking=False))


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def canonical_report_digest(path: Path) -> str:
    """The CRD of ``path`` — sha256 over every line except the attestation line.

    Byte-for-byte what ``scripts/report-attest.sh`` computes with
    ``grep -Ev … | sha256sum``.  Two details of ``grep`` matter and are easy to
    get wrong: it is line-oriented, so a file whose last line lacks a newline
    still digests as though it had one; and an empty file yields no output at
    all, not a bare newline.  Reimplemented rather than shelled out to, so the
    verifier runs on a box that has no root checkout and no ``report-attest.sh``.
    """
    data = path.read_bytes()
    if not data:
        return hashlib.sha256().hexdigest()
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    kept = b"".join(line + b"\n" for line in lines if not REPORT_SHA_RE.match(line))
    return hashlib.sha256(kept).hexdigest()


def declared_report_sha(path: Path) -> str | None:
    for raw in path.read_bytes().split(b"\n"):
        if REPORT_SHA_RE.match(raw):
            value = re.sub(rb"^(?:\*\*)?Report-SHA256(?::\*\*|:)[ \t]*", b"", raw)
            return value.decode("utf-8", "replace").strip()
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in ``repo``.  A missing binary is internal, not a finding."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError as exc:  # pragma: no cover - defensive
        raise EvidenceError(f"git is not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EvidenceError(f"git {' '.join(args)} timed out after 120s") from exc


def commit_exists(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    proc = git(repo, "diff", "--name-only", f"{base}...{head}")
    if proc.returncode != 0:
        raise EvidenceError(
            f"git diff {base}...{head} failed in {repo}: {proc.stderr.strip() or 'no stderr'}"
        )
    return [line for line in proc.stdout.splitlines() if line]


def providers_touched(paths: Iterable[str]) -> tuple[set[str], bool]:
    """(providers named by the diff, whether shared spawn code was touched)."""
    found: set[str] = set()
    shared = False
    for path in paths:
        for pattern in PROVIDER_PATH_RES:
            match = pattern.search(path)
            if match:
                found.add(match.group(1))
                break
        if any(path.endswith(name) for name in SHARED_SPAWN_PATHS):
            shared = True
    return found, shared


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvidenceError(f"manifest is not a regular file: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EvidenceError(f"{path}: manifest is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise EvidenceError(f"{path}: cannot read manifest: {exc}") from exc
    if not isinstance(data.get("evidence"), dict):
        raise EvidenceError(f"{path}: manifest has no [evidence] table")
    head = data["evidence"]
    for key in ("feature", "base", "head", "report"):
        if not str(head.get(key, "")).strip():
            raise EvidenceError(f"{path}: [evidence] is missing required key {key!r}")
    for key in ("base", "head"):
        if not HEX40.match(str(head[key])):
            raise EvidenceError(f"{path}: [evidence] {key} is not a full 40-hex sha: {head[key]!r}")
    return data


def _rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = data.get(key, [])
    if isinstance(rows, dict):
        return [rows]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise EvidenceError(f"[[{key}]] must be a list of tables")
    return rows


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def check_report(manifest: dict[str, Any], result: VerifyResult) -> None:
    """E1 report present/absent, E2 hash match/mismatch.

    The report is the artifact every other claim hangs off.  Absent, unattested
    or drifted, nothing downstream is worth checking — but the run continues so
    the reviewer gets the whole picture in one pass instead of a peel-the-onion
    sequence of re-runs.
    """
    raw = str(manifest["evidence"]["report"])
    report = Path(raw)
    if not report.is_absolute():
        result.fail("E1", "report", f"path is not absolute: {raw}")
        return
    if not report.is_file():
        result.fail("E1", "report", f"E-REPORT-MISSING no such file: {report}")
        return
    declared = declared_report_sha(report)
    if declared is None:
        result.fail("E2", "report", f"E-REPORT-UNATTESTED no Report-SHA256 line in {report}")
        return
    actual = canonical_report_digest(report)
    if declared != actual:
        result.fail(
            "E2",
            "report",
            f"E-REPORT-DRIFT {report}: declared {declared[:12]} != computed {actual[:12]}",
        )


def check_artifacts(manifest: dict[str, Any], root: Path, result: VerifyResult) -> None:
    """E2 (continued) — every pinned authority artifact still hashes as pinned."""
    for index, row in enumerate(_rows(manifest, "artifact"), start=1):
        subject = str(row.get("path", f"artifact#{index}"))
        declared = str(row.get("sha256", "")).strip().lower()
        if not HEX64.match(declared):
            result.fail("E2", subject, f"sha256 is not 64 hex: {row.get('sha256')!r}")
            continue
        candidate = Path(subject)
        path = candidate if candidate.is_absolute() else root / candidate
        if not path.is_file():
            result.fail("E2", subject, f"E-ARTIFACT-ABSENT no such file: {path}")
            continue
        actual = file_sha256(path)
        if actual != declared:
            result.fail(
                "E2",
                subject,
                f"E-ARTIFACT-DRIFT declared {declared[:12]} != on disk {actual[:12]}",
            )


def check_ledger(manifest: dict[str, Any], root: Path, repo: Path, result: VerifyResult) -> None:
    """E3 ledger row present/stale.

    The live ledger is the record that the obligation exists at all.  A row that
    is absent means the feature was verified and then forgotten; a row whose
    owning commit does not resolve, or is not reachable from the head under
    review, means the row describes a different tree than the evidence does —
    stale, and not less dangerous than absent.
    """
    head = manifest["evidence"]
    feature = str(head["feature"]).strip()
    ledger_rel = str(head.get("ledger", DEFAULT_LEDGER))
    candidate = Path(ledger_rel)
    ledger = candidate if candidate.is_absolute() else root / candidate
    if not ledger.is_file():
        result.fail("E3", feature, f"ledger file does not exist: {ledger}")
        return
    rows = [line for line in ledger.read_text(encoding="utf-8").splitlines() if feature in line]
    if not rows:
        result.fail(
            "E3",
            feature,
            f"E-LEDGER-ROW-ABSENT no row naming {feature} in {ledger_rel}",
        )
        return
    shas = {sha for line in rows for sha in re.findall(r"\b[0-9a-f]{8,40}\b", line)}
    if not shas:
        result.fail(
            "E3",
            feature,
            f"E-LEDGER-ROW-STALE row in {ledger_rel} cites no owning commit",
        )
        return
    head_sha = str(head["head"])
    for sha in sorted(shas):
        if not commit_exists(repo, sha):
            continue
        if sha == head_sha[: len(sha)] or is_ancestor(repo, sha, head_sha):
            return
    result.fail(
        "E3",
        feature,
        f"E-LEDGER-ROW-STALE no commit cited in {ledger_rel} "
        f"({', '.join(sorted(s[:8] for s in shas))}) resolves and is reachable from "
        f"{head_sha[:8]}",
    )


def check_dependencies(manifest: dict[str, Any], root: Path, result: VerifyResult) -> None:
    """E4 declared dependency satisfied/missing.

    A Stage-B verdict that cites an unpinned Stage-A ledger is not a pass
    (GATE-RULES, *Tiered dispatch*).  The manifest declares each dependency with
    its sha256; here the file has to exist and still hash to it.
    """
    for index, row in enumerate(_rows(manifest, "depends_on"), start=1):
        kind = str(row.get("kind", "dependency"))
        raw = str(row.get("path", "")).strip()
        subject = f"{kind}#{index}"
        if not raw:
            result.fail("E4", subject, "dependency declares no path")
            continue
        subject = f"{kind} {raw}"
        candidate = Path(raw)
        path = candidate if candidate.is_absolute() else root / candidate
        if not path.is_file():
            result.fail("E4", subject, f"E-DEPENDENCY-MISSING no such file: {path}")
            continue
        declared = str(row.get("sha256", "")).strip().lower()
        if not declared:
            result.fail("E4", subject, "E-DEPENDENCY-UNPINNED dependency declares no sha256")
            continue
        if not HEX64.match(declared):
            result.fail("E4", subject, f"sha256 is not 64 hex: {row.get('sha256')!r}")
            continue
        actual = file_sha256(path)
        if actual != declared:
            result.fail(
                "E4",
                subject,
                f"E-DEPENDENCY-DRIFT declared {declared[:12]} != on disk {actual[:12]}",
            )


def check_suites(manifest: dict[str, Any], repo: Path, result: VerifyResult) -> None:
    """E5 suite evidence: executor predicates, tree state, cache, CI attestation.

    Three interim rules collapse into this one function — the executor
    precedence of ``^verification-suite-executor``, the tcache force/bypass rule
    of GATE-RULES EMPIRICAL #2 /2, and the structural attestation check of #3 /1.
    """
    head = manifest["evidence"]
    base_sha, head_sha = str(head["base"]), str(head["head"])
    suites = _rows(manifest, "suite")
    if not suites:
        result.fail("E5", "suite", "manifest declares no [[suite]] run")
        return
    for index, row in enumerate(suites, start=1):
        name = str(row.get("name") or f"suite#{index}")
        executor = str(row.get("executor", "")).strip()
        if executor not in VALID_EXECUTORS:
            result.fail(
                "E5", name, f"executor must be one of {'|'.join(VALID_EXECUTORS)}, got {executor!r}"
            )
            continue
        if not str(row.get("command", "")).strip():
            result.fail("E5", name, "run declares no command — a claim is not a log")
            continue
        _check_executor_predicate(name, row, executor, result)
        _check_tree_state(name, row, base_sha, head_sha, repo, result)
        _check_cache(name, row, result)
        if executor == "ci":
            _check_attestation(name, row, head_sha, repo, result)
        else:
            log = str(row.get("log", "")).strip()
            if not log:
                result.fail(
                    "E5", name, f"{executor} run declares no log — log-as-proof, not a claim"
                )
            elif not Path(log).is_file():
                result.fail("E5", name, f"E-LOG-MISSING declared log does not exist: {log}")
            if row.get("exit_code") is None:
                result.fail("E5", name, "run declares no exit_code")
            if not str(row.get("counts", "")).strip():
                result.fail("E5", name, "run declares no counts from the run log")


def _check_executor_predicate(
    name: str, row: dict[str, Any], executor: str, result: VerifyResult
) -> None:
    """The selection predicates, mechanised.

    Box is PRIMARY; CI is the fallback and only once the fleet is exhausted;
    local fenced make only when neither is available, or when the test needs the
    live local environment.  A box run without a lease is a raw-ssh run, which
    the recipe forbids because it races the fleet reconciler.
    """
    if executor == "box":
        if not str(row.get("lease", "")).strip():
            result.fail(
                "E5",
                name,
                "box run declares no grokfleet lease — an unleased run is a raw-ssh run",
            )
        return
    if executor == "ci":
        if not row.get("fleet_exhausted"):
            result.fail(
                "E5",
                name,
                "CI is the FALLBACK executor: declare fleet_exhausted after probing the fleet",
            )
        return
    if not str(row.get("local_reason", "")).strip():
        if not (row.get("fleet_exhausted") and row.get("ci_unreachable")):
            result.fail(
                "E5",
                name,
                "local run needs fleet_exhausted + ci_unreachable, or a local_reason "
                "(the test needs the live local environment)",
            )


def _check_tree_state(
    name: str,
    row: dict[str, Any],
    base_sha: str,
    head_sha: str,
    repo: Path,
    result: VerifyResult,
) -> None:
    """Tree-evidence verification: a run proves the tree it actually ran on."""
    tree = str(row.get("tree", "")).strip()
    if not tree:
        result.fail("E5", name, "run declares no tree — evidence is per TREE STATE")
        return
    if tree not in (base_sha, head_sha):
        result.fail(
            "E5",
            name,
            f"E-TREE-MISMATCH run covers {tree[:8]}, which is neither base {base_sha[:8]} "
            f"nor head {head_sha[:8]}",
        )
        return
    if not commit_exists(repo, tree):
        result.fail("E5", name, f"E-TREE-UNRESOLVED {tree[:8]} is not a commit in {repo}")


def _check_cache(name: str, row: dict[str, Any], result: VerifyResult) -> None:
    """tcache: a HIT is same-uid local state, not evidence for a load-bearing run."""
    cache = str(row.get("cache", "")).strip()
    if not cache:
        if row.get("load_bearing"):
            result.fail("E5", name, "load-bearing run does not declare cache state (bypassed|hit)")
        return
    if cache not in VALID_CACHE:
        result.fail("E5", name, f"cache must be one of {'|'.join(VALID_CACHE)}, got {cache!r}")
        return
    if cache == "hit" and row.get("load_bearing"):
        result.fail(
            "E5",
            name,
            "E-CACHE-UNSIGNED load-bearing run offered a tcache HIT; re-run with "
            "--force / TCACHE=off",
        )


def _check_attestation(
    name: str, row: dict[str, Any], head_sha: str, repo: Path, result: VerifyResult
) -> None:
    """``^gates-ci-attestation``: tree, signature, predicate, chain, age, results.

    All six, or the attestation does not replace a local slot-gated run.  A run
    URL alone is never sufficient, so a manifest that carries a URL and nothing
    else fails on every other leg rather than passing on the URL's plausibility.
    """
    tree = str(row.get("tree", "")).strip()
    if tree and tree != head_sha:
        result.fail(
            "E5", name, f"E-ATTEST-TREE attestation covers {tree[:8]}, head is {head_sha[:8]}"
        )
    if not str(row.get("signature", "")).strip():
        result.fail("E5", name, "E-ATTEST-SIGNATURE attestation declares no signature")
    if not str(row.get("workflow", "")).strip():
        result.fail("E5", name, "E-ATTEST-PREDICATE attestation declares no workflow")
    derived = str(row.get("derived_from", "")).strip()
    if not derived:
        result.fail("E5", name, "E-ATTEST-CHAIN attestation declares no derived_from commit")
    elif not commit_exists(repo, derived):
        result.fail(
            "E5", name, f"E-ATTEST-CHAIN derived_from {derived[:8]} is not a commit in {repo}"
        )
    elif derived != head_sha and not is_ancestor(repo, derived, head_sha):
        result.fail(
            "E5",
            name,
            f"E-ATTEST-CHAIN derived_from {derived[:8]} is not reachable from head "
            f"{head_sha[:8]}",
        )
    conclusion = str(row.get("conclusion", "")).strip()
    if conclusion != "success":
        result.fail("E5", name, f"E-ATTEST-RESULT conclusion is {conclusion or '<absent>'!r}")
    if not str(row.get("counts", "")).strip():
        result.fail("E5", name, "E-ATTEST-RESULT attestation reports no results triple")


def check_attestation_age(
    manifest: dict[str, Any], max_age_hours: int, now: dt.datetime, result: VerifyResult
) -> None:
    """The age leg of ``^gates-ci-attestation``, split out so ``now`` is injectable.

    A test that cannot pin the clock ends up asserting against ``utcnow()``, which
    is how an age check acquires a slow leak and starts failing in CI months
    later.  The command passes the real clock; the suite passes a fixed one.
    """
    for index, row in enumerate(_rows(manifest, "suite"), start=1):
        if str(row.get("executor", "")).strip() != "ci":
            continue
        name = str(row.get("name") or f"suite#{index}")
        raw = str(row.get("attested_at", "")).strip()
        if not raw:
            result.fail("E5", name, "E-ATTEST-AGE attestation declares no attested_at")
            continue
        try:
            stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            result.fail("E5", name, f"E-ATTEST-AGE attested_at is not ISO-8601: {raw!r}")
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        age_hours = (now - stamp).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            result.fail(
                "E5",
                name,
                f"E-ATTEST-AGE attestation is {age_hours:.1f}h old, limit is {max_age_hours}h",
            )


def check_static(manifest: dict[str, Any], result: VerifyResult) -> None:
    """E6 static diagnostics: mypy base/head compare, lint-imports recorded.

    The COMPARE is mechanical and lands here; the VERDICT on a new diagnostic is
    not.  A head-only diagnostic is a WARN, because whether it is a regression or
    a pre-existing condition newly surfaced is exactly the judgment the issue
    reserves for prose.  What blocks is failing to record the comparison at all.
    """
    static = manifest.get("static")
    if not isinstance(static, dict):
        result.fail("E6", "static", "manifest declares no [static] table")
        return
    mypy = static.get("mypy")
    if not isinstance(mypy, dict):
        result.fail("E6", "mypy", "manifest declares no [static.mypy] base/head compare")
    else:
        base = {str(item) for item in mypy.get("base", [])}
        head = {str(item) for item in mypy.get("head", [])}
        if "base" not in mypy or "head" not in mypy:
            result.fail("E6", "mypy", "[static.mypy] needs both a base and a head list")
        for diagnostic in sorted(head - base):
            result.warn("E6", "mypy", f"head-only diagnostic (adjudicate): {diagnostic}")
        for diagnostic in sorted(base - head):
            result.notes.append(
                f"evidence: NOTE E6 mypy: base-only diagnostic cleared: {diagnostic}"
            )
    imports = static.get("lint_imports")
    if not isinstance(imports, dict):
        result.fail("E6", "lint-imports", "manifest records no [static.lint_imports] result")
        return
    if imports.get("exit_code") is None:
        result.fail("E6", "lint-imports", "[static.lint_imports] records no exit_code")
    elif int(imports["exit_code"]) != 0:
        result.fail("E6", "lint-imports", f"lint-imports exited {imports['exit_code']}, must pass")


def check_failed_sets(manifest: dict[str, Any], result: VerifyResult) -> None:
    """E7 A/B FAILED sets — compared here, adjudicated in prose.

    Counts are not compared at all: the rule is FAILED sets, and two runs can
    agree on a count while disagreeing on which tests failed.
    """
    for index, row in enumerate(_rows(manifest, "failed_set"), start=1):
        test = str(row.get("test") or f"failed_set#{index}")
        base = str(row.get("base", "")).strip()
        head = str(row.get("head", "")).strip()
        if base not in VALID_OUTCOMES or head not in VALID_OUTCOMES:
            result.fail(
                "E7",
                test,
                f"base/head outcomes must be {'|'.join(VALID_OUTCOMES)}, got {base!r}/{head!r}",
            )
            continue
        if base == "pass" and head == "fail" and not str(row.get("explanation", "")).strip():
            result.warn("E7", test, "head-only failure with no explanation — adjudicate")


def check_impact_matrix(manifest: dict[str, Any], repo: Path, result: VerifyResult) -> None:
    """E8 change-impact matrix, GENERATED from the diff rather than transcribed.

    ``^verification-impact-matrix`` made the matrix a required report section and
    left its construction to the reviewer.  Here the required rows are computed
    from ``base...head``: every provider whose spawn/launch path the diff touched
    needs a live-round row that delivered its OWN callback.  Behavioural
    completeness of the round stays judgment; the row's existence does not.
    """
    head = manifest["evidence"]
    paths = changed_paths(repo, str(head["base"]), str(head["head"]))
    touched, shared = providers_touched(paths)
    rounds = _rows(manifest, "live_round")
    covered = {str(row.get("provider", "")).strip() for row in rounds}
    covered.discard("")
    if shared:
        for provider in sorted(covered):
            result.matrix[provider] = "required (shared spawn code touched)"
        if not covered:
            result.fail(
                "E8",
                "shared-spawn",
                "diff touches shared spawn code but the manifest declares no live round",
            )
    for provider in sorted(touched):
        result.matrix[provider] = "required (provider path touched)"
    raw_waiver = manifest.get("live_round_waiver")
    waiver: dict[str, Any] = raw_waiver if isinstance(raw_waiver, dict) else {}
    if waiver.get("fleet_down"):
        if not str(waiver.get("substitute", "")).strip() or not str(waiver.get("log", "")).strip():
            result.fail(
                "E8",
                "waiver",
                "fleet-down waiver needs a substitute round and its log "
                "(a local G7 sandbox round, never the production port)",
            )
        else:
            substitute_log = Path(str(waiver["log"]))
            if not substitute_log.is_file():
                result.fail(
                    "E8", "waiver", f"substitute round log does not exist: {substitute_log}"
                )
        for provider in sorted(touched - covered):
            result.matrix[provider] = "waived (fleet down)"
        return
    for provider in sorted(touched - covered):
        result.fail(
            "E8",
            provider,
            "E-MATRIX-MISSING provider spawn path touched but no live-round row",
        )
    for index, row in enumerate(rounds, start=1):
        provider = str(row.get("provider") or f"live_round#{index}")
        if not str(row.get("callback", "")).strip():
            result.fail(
                "E8",
                provider,
                "live round records no OWN callback — pane text never counts",
            )
        log = str(row.get("log", "")).strip()
        if not log:
            result.fail("E8", provider, "live round declares no log")
        elif not Path(log).is_file():
            result.fail("E8", provider, f"E-LOG-MISSING live-round log does not exist: {log}")
        result.matrix.setdefault(provider, "covered")


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def run_verify(
    manifest_path: Path,
    *,
    root: Path,
    repo: Path,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    now: dt.datetime | None = None,
) -> VerifyResult:
    manifest = load_manifest(manifest_path)
    result = VerifyResult()
    if not repo.is_dir():
        raise EvidenceError(f"--repo-path is not a directory: {repo}")
    if git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise EvidenceError(f"--repo-path is not a git work tree: {repo}")
    for sha_key in ("base", "head"):
        sha = str(manifest["evidence"][sha_key])
        if not commit_exists(repo, sha):
            raise EvidenceError(f"[evidence] {sha_key} {sha[:8]} is not a commit in {repo}")
    check_report(manifest, result)
    check_artifacts(manifest, root, result)
    check_ledger(manifest, root, repo, result)
    check_dependencies(manifest, root, result)
    check_suites(manifest, repo, result)
    check_attestation_age(manifest, max_age_hours, now or dt.datetime.now(dt.timezone.utc), result)
    check_static(manifest, result)
    check_failed_sets(manifest, result)
    check_impact_matrix(manifest, repo, result)
    return result


def exit_code_for(result: VerifyResult, mode: str) -> int:
    if mode == "block" and any(finding.blocking for finding in result.findings):
        return EXIT_INVALID
    return EXIT_OK


def render_matrix(result: VerifyResult) -> str:
    lines = ["| Provider | Status |", "|---|---|"]
    for provider in sorted(result.matrix):
        lines.append(f"| {provider} | {result.matrix[provider]} |")
    return "\n".join(lines) + "\n"


def render_report(result: VerifyResult, mode: str) -> str:
    lines = [finding.render(mode) for finding in sorted(result.findings)]
    lines.extend(result.notes)
    if result.matrix:
        lines.append(
            "evidence: matrix " + ", ".join(f"{k}={v}" for k, v in sorted(result.matrix.items()))
        )
    blocking = sum(1 for finding in result.findings if finding.blocking)
    warnings = len(result.findings) - blocking
    if not result.findings:
        lines.append(f"evidence: clean (mode={mode})")
    else:
        lines.append(f"evidence: {blocking} blocking, {warnings} warn (mode={mode})")
    return "\n".join(lines)


@click.group("evidence")
def evidence() -> None:
    """Verify the suite/CI/live evidence a gate round declares (F809 A06, #668)."""


@evidence.command("verify")
@click.argument("manifest", type=click.Path(path_type=Path))
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root holding orchestrator/ and doctrine/ (default: current directory).",
)
@click.option(
    "--repo-path",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Git work tree the base/head shas live in (default: --workspace).",
)
@click.option(
    "--mode",
    type=click.Choice(["block", "warn"]),
    default="block",
    show_default=True,
    help="block: structurally invalid evidence exits 1. warn: report only, exit 0.",
)
@click.option(
    "--max-age-hours",
    type=int,
    default=DEFAULT_MAX_AGE_HOURS,
    show_default=True,
    help="Oldest CI attestation that may stand in for a local run.",
)
@click.option(
    "--emit-matrix",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the GENERATED change-impact matrix to this path.",
)
def verify(
    manifest: Path,
    workspace: Path | None,
    repo_path: Path | None,
    mode: str,
    max_age_hours: int,
    emit_matrix: Path | None,
) -> None:
    """Verify the evidence MANIFEST against the report, git and the ledger."""
    root = (workspace or Path.cwd()).resolve()
    repo = (repo_path or root).resolve()
    try:
        if max_age_hours <= 0:
            raise EvidenceError(f"--max-age-hours must be positive, got {max_age_hours}")
        result = run_verify(
            manifest.resolve() if manifest.is_absolute() else (Path.cwd() / manifest).resolve(),
            root=root,
            repo=repo,
            max_age_hours=max_age_hours,
        )
        if emit_matrix is not None:
            emit_matrix.parent.mkdir(parents=True, exist_ok=True)
            emit_matrix.write_text(render_matrix(result), encoding="utf-8")
    except EvidenceError as exc:
        click.echo(f"evidence: FATAL {exc}", err=True)
        sys.exit(EXIT_INTERNAL)
    except Exception as exc:  # pragma: no cover - defensive: never 0 on a crash
        click.echo(f"evidence: FATAL unexpected {type(exc).__name__}: {exc}", err=True)
        sys.exit(EXIT_INTERNAL)
    click.echo(render_report(result, mode))
    sys.exit(exit_code_for(result, mode))
