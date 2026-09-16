"""``cao-orchestrator lint-doctrine`` — composed-doctrine budget and coverage linter.

F809 #666 A14 (``#681``) with A10 (``#673``) folded in as check C3. This command
replaces ``scripts/doctrine-budget.sh``, which is deleted in the same batch: the
"encode by default" ruling (user, 2026-09-16) puts the composed-doctrine budget
behind a ``cao-orchestrator`` verb instead of a hand-applied prose rule.

Why it lives here and not on base ``cao``: every input it reads is a skill-owned
knowledge path (``doctrine/``, ``orchestrator/``), and
``test/architecture/test_no_knowledge_path_reads.py`` allows those reads only
under ``cli/orchestrator_commands/``.

Checks
------
C1  composed playbook bytes per manifest variant against the phase limit.
C2  ``orchestrator/GATE-RULES.md`` bytes, measured as-is.
C3  coverage: every ``mechanism EXISTS [id]`` row in the migration ledger, and
    every bracketed mechanism id cited in doctrine prose, resolves to real CODE —
    a hit that occurs only in a comment is ``CITATION-ONLY`` and fails, because a
    mention is not an implementation; every ``pending ASK [Axx]`` resolves to an
    OPEN milestone issue.  The id -> location map is GENERATED into
    ``orchestrator/mechanism-inventory.md`` (``--emit-inventory``); a stale
    committed inventory is itself a C3 finding.
C4  ledger completeness: every anchor in the composed output has a ledger row,
    and every ``duplicate``/``stale`` row's surviving authority resolves (AC3).
C5  exception validity: a ``[[exception]]`` whose ``until`` has passed fails
    instead of excusing.

Exit codes (the two defects of the shell script this replaces are fixed here)
----------------------------------------------------------------------------
0  clean, or budget findings in warn mode.
1  budget OVER (C1/C2) in block mode.
2  usage or internal error — **always 2, in either mode**.  The old script's
   ``die()`` exited 0 on every internal FATAL unless mode was ``block``, so a
   missing manifest or a composer crash read as a pass.
3  coverage/ledger/exception failure (C3/C4/C5), distinct from the byte budget
   so the coverage gate can be armed while the budget still only warns.

Precedence when several apply: 2 > 3 > 1 > 0.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import click

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_INTERNAL = 2
EXIT_COVERAGE = 3

# From AC1 r3 (blueprint f809-doctrine-mechanism-boundary.md).  `final` is a
# tracked ratchet, never a merge requirement — see `_blocking_budget`.
PHASE_LIMITS: dict[str, tuple[int, int]] = {
    "migration": (45_000, 13_000),
    "final": (20_000, 8_000),
}

DEFAULT_MANIFEST = "doctrine/manifests/orchestrator.toml"
DEFAULT_COMPOSER = "doctrine/compose/compose.py"
DEFAULT_GATE_RULES = "orchestrator/GATE-RULES.md"
DEFAULT_EXCEPTIONS = "doctrine/budget-exceptions.toml"
DEFAULT_LEDGER = "doctrine/MIGRATION-F809.md"
DEFAULT_INVENTORY = "orchestrator/mechanism-inventory.md"
DEFAULT_ASK_MAP = "orchestrator/blueprints/f809-doctrine-mechanism-boundary.md"
DEFAULT_MILESTONE = "wp-doctrine-mechanization"
DEFAULT_REPO = "chaogebaba/cli-subagents"

# Where a mechanism id may resolve, in scan order.  A directory is walked for
# SOURCE_SUFFIXES; a file is read as-is.  Kept short on purpose: an unbounded
# scan would resolve an id against prose and call a citation an implementation.
MECHANISM_SOURCES: tuple[tuple[str, str], ...] = (
    (".claude/settings.json", "hook"),
    (".claude/hooks", "hook"),
    ("scripts/gated-merge.sh", "script"),
    ("scripts/report-attest.sh", "script"),
    ("cli-agent-orchestrator/src", "runtime"),
)
SOURCE_SUFFIXES = (".py", ".sh", ".json")


def runtime_source() -> Path:
    """The installed ``cli_agent_orchestrator`` package directory.

    A runtime mechanism (``FROZEN-PIN`` -> ``services/authority_pin_service.py``)
    must resolve wherever the linter runs, not only in a checkout that happens to
    have the fork nested at ``cli-agent-orchestrator/``.  The build that is
    actually installed IS the runtime doctrine cites, so it is the right source.
    """
    import cli_agent_orchestrator

    return Path(cli_agent_orchestrator.__file__).resolve().parent


# compose.py's own block-id grammar, so "an anchor" means the same thing to the
# linter and to the compositor: a `^id` token alone at end of line.
ANCHOR_RE = re.compile(r"(?:(?<=\s)|^)\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$", re.M)
MECHANISM_CLAIM_RE = re.compile(r"mechanism EXISTS \[([^\]]+)\]")
ASK_CLAIM_RE = re.compile(r"pending ASK \[(A\d+)\]")
# A bracketed mechanism citation in prose: `[queue-native-seat-carrier]`.
# Only tokens the ledger already declares as mechanism ids are treated as
# claims, so prose labels like `[LIVE-ONLY]` are not mistaken for mechanisms.
PROSE_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9./_*-]*(?:;\s*[A-Za-z][A-Za-z0-9./_*-]*)*)\]")
ASK_MAP_RE = re.compile(r"#(\d+)\s+F\d+\s+(A\d+)")


def authoritative(disposition: str) -> str:
    """Drop parenthesised commentary before reading a row's claims.

    A ledger row states its disposition outside parentheses and narrates inside
    them.  Without this, a row that RECORDS a retired claim — "pending ASK [A02]
    ... (was mechanism EXISTS [queue-native-seat-carrier] ...)" — re-asserts the
    very claim it retired, and C3 reports a finding the ledger already fixed.
    """
    previous = None
    while previous != disposition:
        previous = disposition
        disposition = re.sub(r"\([^()]*\)", "", disposition)
    return disposition


# Word-anchored on purpose: a raw substring test matched "stale" inside
# "staleness is a vibe, not a disposition" and called the row recognized.
DISPOSITION_RE = re.compile(
    r"\b(?:retained/compressed|config projection|mechanism EXISTS|pending ASK"
    r"|recipe|duplicate|stale)\b"
)

# AC3's second branch: a row may retire an obligation with no successor, but the
# retirement has to be ADJUDICATED. "deleted" alone excused
# `duplicate -> a unit that was deleted long ago`; the marker now has to sit in a
# parenthesised note that also cites the decision that took it.
RETIREMENT_RE = re.compile(r"\(([^()]*\bdeleted\b[^()]*)\)", re.IGNORECASE)
ADJUDICATION_RE = re.compile(r"\b(?:B4-\d+|AC\d+|D\d+|r\d+|#\d+)\b")


class LintError(Exception):
    """Usage or internal failure — always exit 2, in either mode."""


@dataclass(frozen=True, order=True)
class Finding:
    check: str
    subject: str
    detail: str
    kind: str = "coverage"  # "budget" | "coverage"


@dataclass(frozen=True)
class Measurement:
    name: str
    size: int
    limit: int
    verdict: str  # OK | OVER | EXCEPTED
    excepted_until: str = ""


@dataclass(frozen=True)
class LedgerRow:
    label: str
    disposition: str
    line: int


@dataclass(frozen=True)
class Claim:
    """One mechanism id asserted by doctrine, with every site that asserts it."""

    mechanism_id: str
    sites: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    mechanism_id: str
    kind: str  # hook | script | runtime | CITATION-ONLY | UNRESOLVED
    location: str  # "path:line", or "" when unresolved
    matched: str = ""

    @property
    def path(self) -> str:
        """The location without its line number — what the inventory records."""
        return self.location.rsplit(":", 1)[0] if self.location else ""


@dataclass
class LintResult:
    measurements: list[Measurement] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    inventory: str = ""
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# scratch containment (F462)
# --------------------------------------------------------------------------
def scratch_root(explicit: Path | None) -> Path:
    """Resolve the scratch root, enforcing the /data/cao-scratch containment rule.

    An explicit ``--scratch`` wins (tests, and boxes where /data does not exist).
    Otherwise ``$CAO_SCRATCH_ROOT`` or ``/data/cao-scratch``; a missing default
    root is an internal error, never a silent fallback to ``/tmp``.
    """
    if explicit is not None:
        explicit.mkdir(parents=True, exist_ok=True)
        return explicit
    root = Path(os.environ.get("CAO_SCRATCH_ROOT", "/data/cao-scratch"))
    if not root.is_dir():
        raise LintError(f"scratch root is not mounted: {root} (pass --scratch to override)")
    run_dir = root / os.environ.get("CAO_TERMINAL_ID", "lint-doctrine")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------
def manifest_variants(manifest: Path, table: str = "orchestrator") -> tuple[str, ...]:
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LintError(f"manifest unreadable: {manifest}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LintError(f"manifest is not valid TOML: {manifest}: {exc}") from exc
    variants = tuple(sorted(str(name) for name in data.get(table, {})))
    if not variants:
        raise LintError(f"no [{table}.<variant>] table in {manifest}")
    return variants


def normalize_composed(data: bytes, workspace: Path, manifest: Path) -> bytes:
    """Make the composed bytes independent of where the repo is checked out.

    ``compose.py`` writes the ABSOLUTE manifest path into its ``GENERATED`` header,
    so the same doctrine measures differently in ``/home/me/repo`` and in a deep
    worktree — measured 2026-09-16: 44,610 vs 44,617 bytes for one identical tree.
    A budget that moves when you clone elsewhere is not a budget, so the path is
    normalized to its repo-relative form before measuring. This changes only what
    the linter measures; the composed prompt the seat reads is untouched.
    """
    try:
        relative = manifest.relative_to(workspace).as_posix()
    except ValueError:
        return data
    return data.replace(str(manifest).encode("utf-8"), relative.encode("utf-8"))


def compose_variant(
    workspace: Path, manifest: Path, composer: Path, variant: str, out: Path
) -> int:
    """Compose one variant into ``out`` and return its UTF-8 byte size."""
    if not composer.is_file():
        raise LintError(f"composer missing: {composer}")
    proc = subprocess.run(
        [
            sys.executable,
            str(composer),
            "--manifest",
            str(manifest),
            "--table",
            "orchestrator",
            "--variant",
            variant,
        ],
        cwd=workspace,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise LintError(f"compose failed for variant {variant!r}: {detail}")
    data = normalize_composed(proc.stdout, workspace, manifest)
    out.write_bytes(data)
    return len(data)


# --------------------------------------------------------------------------
# exception ledger (C5)
# --------------------------------------------------------------------------
def load_exceptions(path: Path, today: dt.date) -> tuple[dict[str, str], list[Finding]]:
    """Return ``{artifact: until}`` for still-valid entries, plus C5 findings.

    An entry whose ``until`` has passed FAILS (C5) rather than quietly excusing
    nothing: an expired exception is a decision nobody re-took.
    """
    if not path.is_file():
        return {}, []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LintError(f"exception ledger unreadable: {path}: {exc}") from exc
    valid: dict[str, str] = {}
    findings: list[Finding] = []
    for entry in data.get("exception", []):
        name = str(entry.get("name", "")).strip()
        raw_until = entry.get("until", "")
        until = raw_until.isoformat() if isinstance(raw_until, dt.date) else str(raw_until).strip()
        if not name or not until:
            findings.append(
                Finding("C5", name or "<unnamed>", f"{path}: exception needs both name and until")
            )
            continue
        if until < today.isoformat():
            findings.append(
                Finding(
                    "C5", name, f"{path}: exception expired {until} (today {today.isoformat()})"
                )
            )
            continue
        valid[name] = until
    return valid, findings


# --------------------------------------------------------------------------
# ledger parsing
# --------------------------------------------------------------------------
def parse_ledger(text: str) -> list[LedgerRow]:
    """Rows of the ``## Appendix …`` tables only — the Totals table is not a ledger."""
    rows: list[LedgerRow] = []
    in_appendix = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            in_appendix = line.startswith("## Appendix")
            continue
        if not in_appendix or not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0].lower().startswith("unit (label)"):
            continue
        rows.append(LedgerRow(cells[0], cells[1], number))
    return rows


def _expand_claim(token: str) -> list[str]:
    """Expand the compound id forms the ledger actually uses.

    ``E-COMMIT-KEY-ABSENT/-BARE/-WRONG-SUFFIX`` -> three ids.  A trailing ``*``
    (``E-COMMIT-KEY-*``) stays a glob and is resolved as a prefix family.
    """
    token = token.strip().strip("`")
    if "/" not in token:
        return [token] if token else []
    head, *rest = token.split("/")
    ids = [head]
    prefix = head.rsplit("-", 1)[0] if "-" in head else head
    for tail in rest:
        tail = tail.strip()
        ids.append(prefix + tail if tail.startswith("-") else tail)
    return [one for one in ids if one]


def ledger_mechanism_ids(
    rows: Sequence[LedgerRow], ledger_path: str
) -> dict[str, list[tuple[str, str]]]:
    """``{mechanism id: [(precise site, stable site)]}`` from ``mechanism EXISTS`` rows.

    Findings quote the precise ``path:line``; the generated inventory records the
    stable ``path:<row label>``. A line number in a COMMITTED generated file makes
    that file a mandatory co-edit of every unrelated ledger insertion above it.
    """
    claims: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        for group in MECHANISM_CLAIM_RE.findall(authoritative(row.disposition)):
            for token in group.split(";"):
                for one in _expand_claim(token):
                    claims.setdefault(one, []).append(
                        (f"{ledger_path}:{row.line}", f"{ledger_path}:{row.label}")
                    )
    return claims


def prose_mechanism_sites(
    files: Sequence[tuple[str, str]], known_ids: Iterable[str]
) -> dict[str, list[tuple[str, str]]]:
    """Find bracketed citations of a KNOWN mechanism id in doctrine prose.

    Restricting to ids the ledger already declares is what keeps prose labels
    (``[LIVE-ONLY]``) and ASK tags out of the mechanism universe.
    """
    known = set(known_ids)
    sites: dict[str, list[tuple[str, str]]] = {}
    for relpath, text in files:
        for number, line in enumerate(text.splitlines(), start=1):
            for group in PROSE_TOKEN_RE.findall(line):
                for token in group.split(";"):
                    for one in _expand_claim(token):
                        if one in known:
                            sites.setdefault(one, []).append((f"{relpath}:{number}", relpath))
    return sites


# --------------------------------------------------------------------------
# mechanism resolution + inventory (C3)
# --------------------------------------------------------------------------
def mechanism_files(
    workspace: Path, sources: Sequence[tuple[str, str]]
) -> list[tuple[str, str, Path]]:
    """``[(relpath, kind, path)]`` in a deterministic order."""
    found: list[tuple[str, str, Path]] = []
    seen: set[Path] = set()

    def add_tree(base: Path, kind: str, label_root: Path) -> None:
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            # The skill CLI CITES mechanism ids (this module's own docstring names
            # FROZEN-PIN); a citation is not an implementation, and resolving an id
            # against the linter that checks it would make C3 self-satisfying.
            if "orchestrator_commands" in path.parts:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                label = path.relative_to(label_root).as_posix()
            except ValueError:
                label = path.as_posix()
            found.append((label, kind, path))

    for rel, kind in sources:
        base = workspace / rel
        if base.is_file():
            resolved = base.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append((rel, kind, base))
        elif base.is_dir():
            add_tree(base, kind, workspace)
    try:
        add_tree(runtime_source(), "runtime", runtime_source().parent)
    except Exception:  # pragma: no cover - source checkout without the package
        pass
    return found


def _claim_pattern(mechanism_id: str) -> re.Pattern[str]:
    if mechanism_id.endswith("*"):
        return re.compile(re.escape(mechanism_id[:-1]) + r"[A-Za-z0-9_-]+")
    # A trailing hyphen component is allowed: FROZEN-PIN resolves to the
    # FROZEN-PIN-ATTESTATION block the server emits.
    return re.compile(re.escape(mechanism_id) + r"(?![A-Za-z0-9_])")


def _first_code_match(pattern: re.Pattern[str], text: str) -> tuple[re.Match[str] | None, bool]:
    """``(match, comment_only)`` — the first hit on a code line, else on a comment.

    POLICY, pinned here rather than left implicit in a fallback, because drawing
    exactly this line is what A10 #673 exists for: a comment naming an id is a
    CITATION, not an implementation. A code hit wins outright. A comment-only hit
    is still RECORDED, since locating the mention is what an author needs, but it
    is reported as ``CITATION-ONLY`` and fails C3 like an absent mechanism —
    "doctrine claims a mechanism no code implements" is true either way.

    Measured 2026-09-16: all nineteen live claims resolve from a code line, so the
    strict policy costs nothing today.
    """
    comment_hit: re.Match[str] | None = None
    for match in pattern.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if not text[line_start : match.start()].lstrip().startswith(("#", "//", "*")):
            return match, False
        if comment_hit is None:
            comment_hit = match
    return comment_hit, comment_hit is not None


def resolve_mechanisms(
    ids: Iterable[str], files: Sequence[tuple[str, str, Path]]
) -> dict[str, Resolution]:
    pending = {one: _claim_pattern(one) for one in ids}
    resolved: dict[str, Resolution] = {}
    citations: dict[str, Resolution] = {}
    for relpath, kind, path in files:
        if not pending:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for mechanism_id in sorted(pending):
            match, comment_only = _first_code_match(pending[mechanism_id], text)
            if match is None:
                continue
            line = text.count("\n", 0, match.start()) + 1
            if comment_only:
                # Keep looking — a later source may implement it for real. The
                # citation is kept only if nothing else resolves the id.
                citations.setdefault(
                    mechanism_id,
                    Resolution(mechanism_id, "CITATION-ONLY", f"{relpath}:{line}", match.group(0)),
                )
                continue
            resolved[mechanism_id] = Resolution(
                mechanism_id, kind, f"{relpath}:{line}", match.group(0)
            )
            del pending[mechanism_id]
    for mechanism_id in pending:
        resolved[mechanism_id] = citations.get(
            mechanism_id, Resolution(mechanism_id, "UNRESOLVED", "")
        )
    return resolved


def render_inventory(
    resolutions: dict[str, Resolution], sites: dict[str, list[tuple[str, str]]]
) -> str:
    """Deterministic id -> location table.

    No timestamps and NO LINE NUMBERS: two runs are byte-identical, and an
    unrelated insertion above a claim or above its implementation does not make
    the committed copy stale. Findings still quote exact ``path:line``.
    """
    lines = [
        "<!-- GENERATED by `cao-orchestrator lint-doctrine --emit-inventory`"
        " (F809 #666, A14/#681).",
        "     NEVER hand-edit: every run overwrites this file, and a stale copy is a",
        "     C3 finding.  One row per mechanism id doctrine CLAIMS — `mechanism EXISTS",
        "     [id]` in the migration ledger, or a bracketed citation of such an id in a",
        "     doctrine section — resolved against the mechanism sources below. -->",
        "# Mechanism inventory",
        "",
        "Sources scanned, in order: " + ", ".join(f"`{rel}`" for rel, _ in MECHANISM_SOURCES) + ".",
        "",
        "| id | kind | resolves to | claimed by |",
        "|---|---|---|---|",
    ]
    for mechanism_id in sorted(resolutions):
        row = resolutions[mechanism_id]
        where = f"`{row.path}`" if row.path else "—"
        claimed = "; ".join(
            f"`{stable}`" for stable in sorted({site[1] for site in sites.get(mechanism_id, ())})
        )
        lines.append(f"| `{mechanism_id}` | {row.kind} | {where} | {claimed or '—'} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# ASK -> issue (C3, second half)
# --------------------------------------------------------------------------
def parse_ask_map(text: str) -> dict[str, int]:
    """``{A06: 668}`` from the blueprint's pinned enumeration line."""
    return {ask: int(number) for number, ask in ASK_MAP_RE.findall(text)}


def issue_states(repo: str, milestone: str, issues_json: Path | None) -> dict[int, str] | None:
    """``{number: STATE}`` from a pinned JSON file, else from ``gh``.

    Returns ``None`` when neither is available: a linter that fails on a box
    with no network is not usable, so the ASK half of C3 is SKIPPED, loudly,
    rather than reported as a pass or as a failure.
    """
    if issues_json is not None:
        try:
            rows = json.loads(issues_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LintError(f"issues json unreadable: {issues_json}: {exc}") from exc
        return {int(row["number"]): str(row["state"]).upper() for row in rows}
    if shutil.which("gh") is None:
        return None
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "-R",
            repo,
            "--milestone",
            milestone,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number,state",
        ],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return {int(row["number"]): str(row["state"]).upper() for row in rows}


# --------------------------------------------------------------------------
# C4 — ledger completeness
# --------------------------------------------------------------------------
def _surviving_authority_resolves(
    disposition: str,
    known_anchors: set[str],
    labels: Sequence[str],
    workspace: Path,
) -> bool:
    """AC3: a duplicate/stale row must name a target that still exists."""
    anchors = set(ANCHOR_RE.findall(disposition)) | {
        token for token in re.findall(r"\^([A-Za-z0-9][A-Za-z0-9_-]*)", disposition)
    }
    if anchors:
        return anchors <= known_anchors
    target = disposition.split("→", 1)[1].strip() if "→" in disposition else disposition
    recipe = re.search(r"recipe\s+([A-Za-z0-9._-]+\.md)", target)
    if recipe and (workspace / "doctrine" / "recipes" / recipe.group(1)).is_file():
        return True
    retirement = RETIREMENT_RE.search(target)
    if retirement and ADJUDICATION_RE.search(retirement.group(1)):
        # AC3's other branch: an obligation may retire with no successor, but only
        # as an ADJUDICATED retirement — a parenthesised note that cites the
        # decision. Bare prose ("a unit that was deleted long ago") is not one.
        return True
    head = re.split(r"[(;]", target, maxsplit=1)[0].strip()
    if head and any(label.startswith(head) for label in labels):
        return True
    section = head.lower().replace(" ", "-").replace("&", "and")
    for candidate in (section, section.split("-")[0]):
        if candidate and list((workspace / "doctrine" / "sections").rglob(f"{candidate}.md")):
            return True
    return False


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def _blocking_budget(phase: str, mode: str) -> bool:
    """`final` is a tracked ratchet, never a merge requirement (AC1 r3)."""
    return mode == "block" and phase == "migration"


def run_lint(
    workspace: Path,
    *,
    manifest: Path,
    composer: Path,
    gate_rules: Path,
    exceptions: Path,
    ledger: Path,
    ask_map: Path,
    variants: Sequence[str] = (),
    phase: str = "migration",
    mode: str = "warn",
    scratch: Path | None = None,
    inventory_out: Path | None = None,
    committed_inventory: Path | None = None,
    issues_json: Path | None = None,
    repo: str = DEFAULT_REPO,
    milestone: str = DEFAULT_MILESTONE,
    today: dt.date | None = None,
) -> LintResult:
    if phase not in PHASE_LIMITS:
        raise LintError(f"unknown phase {phase!r} (migration|final)")
    if mode not in ("warn", "block"):
        raise LintError(f"unknown mode {mode!r} (warn|block)")
    for required in (manifest, gate_rules, ledger):
        if not required.is_file():
            raise LintError(f"file missing: {required}")
    today = today or dt.date.today()
    playbook_limit, gate_limit = PHASE_LIMITS[phase]
    result = LintResult()

    valid_exceptions, expiry_findings = load_exceptions(exceptions, today)
    result.findings.extend(expiry_findings)

    def measure(name: str, size: int, limit: int) -> None:
        if size <= limit:
            result.measurements.append(Measurement(name, size, limit, "OK"))
            return
        if name in valid_exceptions:
            result.measurements.append(
                Measurement(name, size, limit, "EXCEPTED", valid_exceptions[name])
            )
            return
        result.measurements.append(Measurement(name, size, limit, "OVER"))
        result.findings.append(
            Finding(
                "C1" if name.startswith("playbook") else "C2",
                name,
                f"{size} bytes over the {phase} limit of {limit}",
                "budget",
            )
        )

    # --- C1/C2 ------------------------------------------------------------
    run_dir = scratch_root(scratch)
    work = Path(tempfile.mkdtemp(prefix="lint-doctrine.", dir=str(run_dir)))
    composed_anchors: dict[str, set[str]] = {}
    try:
        for variant in variants or manifest_variants(manifest):
            out = work / f"orchestrator-{variant}.md"
            size = compose_variant(workspace, manifest, composer, variant, out)
            measure(f"playbook:{variant}", size, playbook_limit)
            composed_anchors[variant] = set(
                ANCHOR_RE.findall(out.read_text(encoding="utf-8", errors="replace"))
            )
        measure("gate-rules", len(gate_rules.read_bytes()), gate_limit)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # --- ledger -----------------------------------------------------------
    ledger_text = ledger.read_text(encoding="utf-8")
    ledger_rel = ledger.relative_to(workspace).as_posix()
    rows = parse_ledger(ledger_text)
    if not rows:
        raise LintError(f"no '## Appendix' ledger rows in {ledger}")
    ledger_anchors = {
        anchor for row in rows for anchor in re.findall(r"\^([A-Za-z0-9][A-Za-z0-9_-]*)", row.label)
    }
    labels = [row.label for row in rows]

    # --- C3: mechanism coverage ------------------------------------------
    claims = ledger_mechanism_ids(rows, ledger_rel)
    section_files: list[tuple[str, str]] = []
    for path in sorted((workspace / "doctrine" / "sections").rglob("*.md")):
        section_files.append(
            (
                path.relative_to(workspace).as_posix(),
                path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    section_files.append(
        (gate_rules.relative_to(workspace).as_posix(), gate_rules.read_text(encoding="utf-8"))
    )
    for mechanism_id, sites in prose_mechanism_sites(section_files, claims).items():
        claims.setdefault(mechanism_id, []).extend(sites)
    resolutions = resolve_mechanisms(claims, mechanism_files(workspace, MECHANISM_SOURCES))
    for mechanism_id in sorted(resolutions):
        resolution = resolutions[mechanism_id]
        if resolution.kind not in ("UNRESOLVED", "CITATION-ONLY"):
            continue
        where = "; ".join(sorted({site[0] for site in claims.get(mechanism_id, ())}))
        if resolution.kind == "CITATION-ONLY":
            detail = (
                f"claimed at {where}; the only hit is a COMMENT at {resolution.location} — "
                "a mention is not an implementation"
            )
        else:
            detail = f"claimed at {where} but resolves to no code in any mechanism source"
        result.findings.append(Finding("C3", mechanism_id, detail))
    result.inventory = render_inventory(resolutions, claims)
    if inventory_out is not None:
        inventory_out.parent.mkdir(parents=True, exist_ok=True)
        inventory_out.write_text(result.inventory, encoding="utf-8")
    elif committed_inventory is not None and committed_inventory.is_file():
        if committed_inventory.read_text(encoding="utf-8") != result.inventory:
            result.findings.append(
                Finding(
                    "C3",
                    committed_inventory.relative_to(workspace).as_posix(),
                    "committed inventory is stale — rerun with --emit-inventory",
                )
            )

    # --- C3: pending ASK -> OPEN issue ------------------------------------
    asks = {ask for row in rows for ask in ASK_CLAIM_RE.findall(authoritative(row.disposition))}
    if asks:
        mapping = parse_ask_map(ask_map.read_text(encoding="utf-8")) if ask_map.is_file() else {}
        states = issue_states(repo, milestone, issues_json)
        if states is None:
            result.skipped.append(
                "C3 pending-ASK -> issue state (no --issues-json and no usable gh)"
            )
        else:
            for ask in sorted(asks):
                number = mapping.get(ask)
                if number is None:
                    result.findings.append(
                        Finding(
                            "C3", ask, f"ledger cites a pending ASK with no issue in {ask_map.name}"
                        )
                    )
                elif states.get(number, "MISSING") != "OPEN":
                    result.findings.append(
                        Finding(
                            "C3",
                            ask,
                            f"issue #{number} is {states.get(number, 'MISSING')}, not OPEN — "
                            "flip the ledger rows or reopen",
                        )
                    )

    # --- C4: ledger completeness -----------------------------------------
    known_anchors = ledger_anchors | {a for group in composed_anchors.values() for a in group}
    reported: set[str] = set()
    for variant in sorted(composed_anchors):
        for anchor in sorted(composed_anchors[variant] - ledger_anchors):
            if anchor in reported:
                continue
            reported.add(anchor)
            result.findings.append(
                Finding(
                    "C4",
                    f"^{anchor}",
                    f"composed in playbook:{variant} with no row in {ledger_rel}",
                )
            )
    for row in rows:
        disposition = row.disposition.lower()
        if not (disposition.startswith("duplicate") or disposition.startswith("stale")):
            continue
        if not _surviving_authority_resolves(row.disposition, known_anchors, labels, workspace):
            result.findings.append(
                Finding(
                    "C4",
                    row.label,
                    f"{ledger_rel}:{row.line}: surviving authority does not resolve",
                )
            )
    for row in rows:
        if not DISPOSITION_RE.search(row.disposition):
            result.findings.append(
                Finding(
                    "C4",
                    row.label,
                    f"{ledger_rel}:{row.line}: unrecognized disposition {row.disposition!r}",
                )
            )
    return result


def exit_code_for(result: LintResult, phase: str, mode: str) -> int:
    if any(finding.check in ("C3", "C4", "C5") for finding in result.findings):
        return EXIT_COVERAGE
    if any(finding.kind == "budget" for finding in result.findings) and _blocking_budget(
        phase, mode
    ):
        return EXIT_BUDGET
    return EXIT_OK


def render_report(result: LintResult, phase: str, mode: str) -> str:
    lines: list[str] = []
    for row in result.measurements:
        suffix = f" (excepted until {row.excepted_until})" if row.excepted_until else ""
        lines.append(f"lint-doctrine: {row.name} {row.size}/{row.limit} {row.verdict}{suffix}")
    for note in result.skipped:
        lines.append(f"lint-doctrine: SKIPPED {note}")
    for finding in sorted(result.findings):
        label = "WARN" if finding.kind == "budget" and not _blocking_budget(phase, mode) else "FAIL"
        lines.append(f"lint-doctrine: {label} {finding.check} {finding.subject}: {finding.detail}")
    if not result.findings:
        lines.append(f"lint-doctrine: clean (phase={phase}, mode={mode})")
    return "\n".join(lines)


@click.command("lint-doctrine")
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root holding doctrine/ and orchestrator/ (default: current directory).",
)
@click.option(
    "--manifest",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Compose manifest (default: {DEFAULT_MANIFEST}).",
)
@click.option(
    "--variant",
    "variant_list",
    multiple=True,
    help="Manifest variant to measure (repeatable; default: every variant).",
)
@click.option(
    "--gate-rules",
    type=click.Path(path_type=Path),
    default=None,
    help=f"GATE-RULES file measured as-is (default: {DEFAULT_GATE_RULES}).",
)
@click.option(
    "--exceptions",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Budget exception ledger (default: {DEFAULT_EXCEPTIONS}).",
)
@click.option(
    "--ledger",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Migration ledger (default: {DEFAULT_LEDGER}).",
)
@click.option(
    "--ask-map",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Blueprint carrying the pinned ASK->issue enumeration (default: {DEFAULT_ASK_MAP}).",
)
@click.option(
    "--phase",
    type=click.Choice(sorted(PHASE_LIMITS)),
    default="migration",
    show_default=True,
    help="Byte-budget phase; `final` is a ratchet, never blocking.",
)
@click.option(
    "--mode",
    type=click.Choice(["warn", "block"]),
    default="warn",
    show_default=True,
    help="Budget mode. Coverage (C3/C4/C5) is armed in BOTH modes.",
)
@click.option(
    "--scratch",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Scratch dir for composed output (default: /data/cao-scratch/<terminal>).",
)
@click.option(
    "--emit-inventory",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Write the generated mechanism inventory (default target: {DEFAULT_INVENTORY}).",
)
@click.option(
    "--issues-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Pinned `gh issue list --json number,state` output (skips gh).",
)
@click.option(
    "--repo", default=DEFAULT_REPO, show_default=True, help="Issue repo for the ASK check."
)
@click.option(
    "--milestone",
    default=DEFAULT_MILESTONE,
    show_default=True,
    help="Milestone whose issues back the pending ASKs.",
)
def lint_doctrine(
    workspace: Path | None,
    manifest: Path | None,
    variant_list: tuple[str, ...],
    gate_rules: Path | None,
    exceptions: Path | None,
    ledger: Path | None,
    ask_map: Path | None,
    phase: str,
    mode: str,
    scratch: Path | None,
    emit_inventory: Path | None,
    issues_json: Path | None,
    repo: str,
    milestone: str,
) -> None:
    """Lint composed doctrine: byte budget, mechanism coverage, ledger completeness."""
    root = (workspace or Path.cwd()).resolve()
    try:
        if not root.is_dir():
            raise LintError(f"workspace is not a directory: {root}")
        result = run_lint(
            root,
            manifest=manifest or root / DEFAULT_MANIFEST,
            composer=root / DEFAULT_COMPOSER,
            gate_rules=gate_rules or root / DEFAULT_GATE_RULES,
            exceptions=exceptions or root / DEFAULT_EXCEPTIONS,
            ledger=ledger or root / DEFAULT_LEDGER,
            ask_map=ask_map or root / DEFAULT_ASK_MAP,
            variants=variant_list,
            phase=phase,
            mode=mode,
            scratch=scratch,
            inventory_out=emit_inventory,
            committed_inventory=root / DEFAULT_INVENTORY,
            issues_json=issues_json,
            repo=repo,
            milestone=milestone,
        )
    except LintError as exc:
        click.echo(f"lint-doctrine: FATAL {exc}", err=True)
        sys.exit(EXIT_INTERNAL)
    except Exception as exc:  # pragma: no cover - defensive: never 0 on a crash
        click.echo(f"lint-doctrine: FATAL unexpected {type(exc).__name__}: {exc}", err=True)
        sys.exit(EXIT_INTERNAL)
    click.echo(render_report(result, phase, mode))
    sys.exit(exit_code_for(result, phase, mode))
