"""``cao-orchestrator certify`` — the only production writer of certification rows.

F788 #645 measured the consequence of there being none: the ``general`` position's
``(kiro_cli, general)`` and ``(codex, general)`` PASS rows sat at a sha pair the
resolver had long stopped computing, so every routing-driven assign for those
providers was refused ``E-PROVIDER-UNCERTIFIED`` and the fleet ran for weeks on the
explicit ``provider=`` bypass. Rows were written by hand (and, worse, by a test-only
helper in ``test/mcp_server/test_f497_routing_d9.py``), so nothing guaranteed the
recorded pair was the pair the reader computes.

This command closes that: it computes ``position_sha``/``overlay_sha`` with the SAME
helpers ``utils/routing.cell_certified`` uses (``profile_composition``), refuses to
write a row that is not backed by an evidence file citing a command and its output,
and replaces the row for a cell's current key rather than appending a second one.

Skill-owned by placement, not by mechanism: certification rows are CAO's own store
format, but *deciding a cell is certified* is an orchestration act, so the verb lives
on ``cao-orchestrator`` and base ``cao`` never grows a writer (wp-arch-modular-core A.4,
F809's encode-by-default ruling). CAO core reads these rows exactly as it did before.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
import frontmatter
import yaml

#: Axis -> frontmatter block. The two axes of D9: the provider axis
#: (``certification:``, read by ``routing.cell_certified``) and the herdr backend
#: axis (``herdr_certification:``, read by ``routing.herdr_cell_certified``).
CERT_BLOCKS: Dict[str, str] = {"provider": "certification", "herdr": "herdr_certification"}

#: The READER's match key on both axes (``routing.HERDR_CERT_FIELDS`` docstring):
#: provider plus the current sha pair.
ROW_KEY = ("provider", "position_sha", "overlay_sha")

#: The WRITER's replacement key is narrower on purpose: ONE row per (position,
#: provider) per axis. Two reasons, both learned from #645. A second row at the
#: same reader key would be unreachable — the reader stops at the first match, so
#: an appended re-certification of a FAIL cell would never be seen. And a row left
#: behind at an OLD sha pair is permanently stale, which would keep
#: ``cert-status`` red forever and retrain everyone to ignore it. The history of
#: what a cell was certified at lives in git, where an audit trail belongs.
REPLACEMENT_KEY = ("provider",)

OUTCOMES = ("PASS", "FAIL")

#: How much of the evidence file's own words the row carries. A row is read by a
#: human deciding whether to trust a cell; the full transcript lives at the cited
#: path and the sha256 ties the row to that exact file.
_SUMMARY_BUDGET = 320

_COMMAND_LINE = re.compile(r"^\s*\$\s+\S")
_COMMAND_LABEL = re.compile(r"^\s*(?:command|cmd|script|run|invocation)\s*:\s*\S", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(?:```|~~~)")


class CertifyError(click.ClickException):
    """A refusal with an E-token, so a caller can match on the reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


# --------------------------------------------------------------------------
# store resolution
# --------------------------------------------------------------------------


def resolve_positions_dir(
    workspace: Optional[Path] = None, positions_dir: Optional[Path] = None
) -> Path:
    """Which positions store this invocation reads and writes.

    Explicit ``--positions-dir`` wins. Otherwise the WORKSPACE copy
    (``<workspace>/profiles/positions``) is preferred over the installed store,
    because the workspace copy is the one under version control: a row written
    into the installed store alone is lost at the next ``./install.sh``.
    """
    if positions_dir is not None:
        resolved = Path(positions_dir).resolve()
        if not resolved.is_dir():
            raise CertifyError("E-STORE-MISSING", f"{resolved}: not a directory")
        return resolved
    ws = Path(workspace or Path.cwd()).resolve()
    candidate = ws / "profiles" / "positions"
    if candidate.is_dir():
        return candidate
    from cli_agent_orchestrator.constants import positions_store_dir

    store = positions_store_dir()
    if store.is_dir():
        return store
    raise CertifyError(
        "E-STORE-MISSING",
        f"no positions store at {candidate} or {store} — run from the workspace root "
        f"or pass --positions-dir",
    )


def current_sha_pair(position: str, provider: str, positions_dir: Path) -> Tuple[str, str]:
    """The CURRENT (position_sha, overlay_sha) for a cell.

    Delegates to ``clause_lint._current_sha_pair``, which is itself a mirror of
    ``routing.cell_certified``'s computation — one hashing implementation, so a
    row this command writes is a row the resolver accepts. Computing it here
    independently is precisely the F788 defect.
    """
    from cli_agent_orchestrator.utils.clause_lint import _current_sha_pair

    return _current_sha_pair(position, provider, positions_dir)


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """A validated evidence file: a command citation, its output, and a digest."""

    path: Path
    sha256: str
    command: str
    summary: str

    def row_field(self) -> str:
        return f"{self.summary} | cmd: {self.command} | evidence: {self.path} sha256={self.sha256[:16]}"


def read_evidence(path: Path) -> Evidence:
    """Validate an evidence file, or refuse.

    The contract is deliberately mechanical: a PASS row asserts somebody ran
    something and read what came back, so the file must CITE a command and show
    at least one line of its output. Prose alone is not evidence, and an empty
    file certainly is not — the F788 rows that went stale were written with no
    artifact behind them at all.

    A command citation is a ``$ cmd`` line, a ``command:``/``script:``/``run:``
    label, or the opening line of a fenced block. Output is any further non-empty
    line that is not itself another command citation.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise CertifyError("E-EVIDENCE-MISSING", f"{resolved}: no such evidence file")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise CertifyError("E-EVIDENCE-EMPTY", f"{resolved}: evidence file is empty")

    lines = text.splitlines()
    command: Optional[str] = None
    output: Optional[str] = None
    in_fence = False
    for raw in lines:
        line = raw.rstrip()
        if _FENCE.match(line):
            # The fence line itself is never the command; the first line inside is.
            in_fence = not in_fence
            continue
        if not line.strip():
            continue
        is_citation = bool(_COMMAND_LINE.match(line) or _COMMAND_LABEL.match(line))
        if command is None:
            if is_citation or in_fence:
                command = line.strip()
            continue
        if not is_citation:
            output = line.strip()
            break
    if command is None:
        raise CertifyError(
            "E-EVIDENCE-NO-COMMAND",
            f"{resolved}: no command cited — the file must contain a '$ <cmd>' line, a "
            f"'command:' label, or a fenced block holding the command that was run",
        )
    if output is None:
        raise CertifyError(
            "E-EVIDENCE-NO-OUTPUT",
            f"{resolved}: a command is cited ({command!r}) but no output follows it — a "
            f"certification records what the command PRINTED, not that it was typed",
        )

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    summary = _summarise(lines, command)
    return Evidence(path=resolved, sha256=digest, command=command, summary=summary)


def _summarise(lines: List[str], command: str) -> str:
    """The file's own first substantive line, bounded — a human-readable handle."""
    for raw in lines:
        line = raw.strip().lstrip("#").strip()
        if line and not _FENCE.match(raw) and line != command:
            return line[:_SUMMARY_BUDGET]
    return command[:_SUMMARY_BUDGET]


# --------------------------------------------------------------------------
# the writer
# --------------------------------------------------------------------------


def _frontmatter_span(text: str) -> Optional[Tuple[int, int]]:
    """(start, end) offsets of the frontmatter BODY, or ``None`` when there is none.

    A position file with no frontmatter at all is legal (several carry only a
    persona body), and certifying one must ADD the block rather than refuse.
    """
    if not text.startswith("---"):
        return None
    first_nl = text.index("\n") + 1
    match = re.search(r"^-{3,}\s*$", text[first_nl:], re.MULTILINE)
    if match is None:
        raise CertifyError("E-POSITION-MALFORMED", "frontmatter block is not closed")
    return first_nl, first_nl + match.start()


class _BlockDumper(yaml.SafeDumper):  # type: ignore[misc]
    """A dumper that indents sequence items under their key.

    PyYAML's default puts ``- provider:`` at the key's own indentation; every
    position file in the store indents it. Matching the house style keeps a
    written row to a one-row diff instead of re-indenting the whole block.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def render_block(block: str, rows: List[Dict[str, Any]]) -> str:
    """Render one frontmatter block as YAML, one row per key, never line-wrapped.

    ``width`` matters: PyYAML folds long scalars at 80 columns by default, which
    would turn every evidence string into a multi-line block and make a
    re-written file diff against itself.
    """
    dumped: str = yaml.dump(
        {block: rows},
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10**6,
    )
    return dumped.rstrip("\n")


def _splice_block(text: str, block: str, rows: List[Dict[str, Any]]) -> str:
    """Replace (or append) ONE frontmatter block, leaving every other byte alone.

    A whole-file ``frontmatter.dumps`` round-trip is the obvious implementation and
    the wrong one: it re-quotes and re-flows frontmatter this command does not own
    (measured on ``profiles/positions/general.md``: 31 diff lines, none of them the
    row being written). The writer owns the certification block and nothing else.
    """
    rendered = render_block(block, rows)
    span = _frontmatter_span(text)
    if span is None:
        return f"---\n{rendered}\n---\n{text}"
    start, end = span
    fm = text[start:end]

    fm_lines = fm.splitlines()
    begin: Optional[int] = None
    for idx, line in enumerate(fm_lines):
        if line.startswith(f"{block}:"):
            begin = idx
            break
    if begin is None:
        new_fm_lines = fm_lines + rendered.splitlines()
    else:
        stop = len(fm_lines)
        for idx in range(begin + 1, len(fm_lines)):
            line = fm_lines[idx]
            if not line.strip():
                continue
            if not line[0].isspace() and not line.startswith("-"):
                stop = idx
                break
        new_fm_lines = fm_lines[:begin] + rendered.splitlines() + fm_lines[stop:]
    return text[:start] + "\n".join(new_fm_lines) + "\n" + text[end:]


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.certify.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_row(
    *,
    provider: str,
    position_sha_value: str,
    overlay_sha_value: str,
    outcome: str,
    date: str,
    evidence: Evidence,
    axis: str,
    herdr_sha256: Optional[str] = None,
    herdr_version: Optional[str] = None,
    protocol: Optional[str] = None,
    reduced_assurance: Optional[str] = None,
) -> Dict[str, Any]:
    """One certification row, fields in the order ``routing.HERDR_CERT_FIELDS`` names."""
    row: Dict[str, Any] = {"provider": provider}
    if axis == "herdr":
        if herdr_version:
            row["herdr_version"] = herdr_version
        row["herdr_sha256"] = herdr_sha256 or ""
        if protocol:
            row["protocol"] = protocol
    row["position_sha"] = position_sha_value
    row["overlay_sha"] = overlay_sha_value
    row["outcome"] = outcome
    row["date"] = date
    row["evidence"] = evidence.row_field()
    if reduced_assurance:
        row["reduced_assurance"] = reduced_assurance
    return row


def write_certification_row(
    positions_dir: Path,
    position: str,
    provider: str,
    *,
    axis: str = "provider",
    outcome: str = "PASS",
    evidence: Evidence,
    date: Optional[str] = None,
    herdr_sha256: Optional[str] = None,
    herdr_version: Optional[str] = None,
    protocol: Optional[str] = None,
    reduced_assurance: Optional[str] = None,
) -> Dict[str, Any]:
    """Write ONE row at the cell's CURRENT sha pair; return the row written.

    Idempotent per cell: the position's block holds exactly ONE row per provider
    per axis, and re-certifying REPLACES it in place (see :data:`REPLACEMENT_KEY`)
    rather than appending. Appending would leave either an unreachable duplicate
    at the same reader key, or a permanently-stale row at the old pair — the two
    shapes that made #645 invisible.
    """
    if axis not in CERT_BLOCKS:
        raise CertifyError("E-AXIS-UNKNOWN", f"{axis}: axis is provider|herdr")
    if outcome not in OUTCOMES:
        raise CertifyError("E-OUTCOME-UNKNOWN", f"{outcome}: outcome is PASS|FAIL")
    path = positions_dir / f"{position}.md"
    if not path.is_file():
        raise CertifyError("E-POSITION-MISSING", f"{path}: no such position file")
    if axis == "herdr" and not herdr_sha256:
        raise CertifyError(
            "E-HERDR-BINARY-UNKNOWN",
            "the herdr axis pins the installed binary; no herdr on PATH and no "
            "--herdr-sha256 given — a row certifying a binary we cannot see is not "
            "evidence about this machine",
        )

    pos_sha, ov_sha = current_sha_pair(position, provider, positions_dir)
    row = build_row(
        provider=provider,
        position_sha_value=pos_sha,
        overlay_sha_value=ov_sha,
        outcome=outcome,
        date=date or _dt.datetime.now(_dt.timezone.utc).date().isoformat(),
        evidence=evidence,
        axis=axis,
        herdr_sha256=herdr_sha256,
        herdr_version=herdr_version,
        protocol=protocol,
        reduced_assurance=reduced_assurance,
    )

    text = path.read_text(encoding="utf-8")
    block = CERT_BLOCKS[axis]
    parsed = frontmatter.loads(text)
    existing = parsed.metadata.get(block) or []
    rows: List[Dict[str, Any]] = [r for r in existing if isinstance(r, dict)]
    key = tuple(row[k] for k in REPLACEMENT_KEY)

    def _key(candidate: Dict[str, Any]) -> Tuple[Any, ...]:
        return tuple(str(candidate.get(k, "")) for k in REPLACEMENT_KEY)

    matches = [idx for idx, candidate in enumerate(rows) if _key(candidate) == key]
    if matches:
        # Keep the cell's position in the block (a reviewer reads these in order)
        # and collapse any duplicates a hand-edit left behind.
        rows = [r for idx, r in enumerate(rows) if idx not in set(matches[1:])]
        rows[matches[0]] = row
    else:
        rows.append(row)
    _atomic_write(path, _splice_block(text, block, rows))
    return row


def installed_store_drift(position: str, provider: str, positions_dir: Path) -> Optional[str]:
    """A warning when the INSTALLED store's fragments differ from the ones just hashed.

    Writing into the workspace is correct (it is what version control keeps), but
    the resolver reads the installed store. If the two disagree, the row is true
    and inert until ``./install.sh`` re-syncs — worth one line, never a refusal.
    """
    from cli_agent_orchestrator.constants import positions_store_dir

    store = positions_store_dir()
    if not store.is_dir() or store.resolve() == positions_dir.resolve():
        return None
    if not (store / f"{position}.md").is_file():
        return f"installed store {store} has no {position}.md — run ./install.sh"
    try:
        store_pair = current_sha_pair(position, provider, store)
    except Exception:  # pragma: no cover - unreadable store is not this command's problem
        return None
    if store_pair != current_sha_pair(position, provider, positions_dir):
        return (
            f"installed store {store} is at a DIFFERENT sha pair {store_pair} — this row "
            f"does not take effect until ./install.sh re-syncs profiles/"
        )
    return None


# --------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------


@click.command("certify")
@click.option("--position", "-P", required=True, help="Position name (file stem under positions/).")
@click.option("--provider", "-p", required=True, help="Provider the cell is certified for.")
@click.option(
    "--axis",
    type=click.Choice(sorted(CERT_BLOCKS)),
    default="provider",
    show_default=True,
    help="provider -> certification:, herdr -> herdr_certification:.",
)
@click.option(
    "--evidence",
    required=True,
    type=click.Path(path_type=Path),
    help="File citing the command that was run and its output. Mandatory.",
)
@click.option("--outcome", type=click.Choice(OUTCOMES), default="PASS", show_default=True)
@click.option("--date", default=None, help="Row date (default: today, UTC).")
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Workspace root holding profiles/positions (default: current directory).",
)
@click.option(
    "--positions-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Write into this positions store instead of the workspace/installed one.",
)
@click.option("--herdr-version", default=None, help="herdr axis: the binary's version string.")
@click.option("--herdr-sha256", default=None, help="herdr axis: override the resolved binary sha.")
@click.option("--protocol", default=None, help="herdr axis: the protocol the smoke exercised.")
@click.option(
    "--reduced-assurance",
    default=None,
    help="What this PASS does NOT cover (WP-HERDR amendment 7).",
)
def certify(
    position: str,
    provider: str,
    axis: str,
    evidence: Path,
    outcome: str,
    date: Optional[str],
    workspace: Optional[Path],
    positions_dir: Optional[Path],
    herdr_version: Optional[str],
    herdr_sha256: Optional[str],
    protocol: Optional[str],
    reduced_assurance: Optional[str],
) -> None:
    """Record a certification row for a (position, provider) cell at its current shas."""
    store = resolve_positions_dir(workspace, positions_dir)
    ev = read_evidence(evidence)
    if axis == "herdr" and not herdr_sha256:
        from cli_agent_orchestrator.utils.routing import installed_herdr_sha256

        herdr_sha256 = installed_herdr_sha256()
    row = write_certification_row(
        store,
        position,
        provider,
        axis=axis,
        outcome=outcome,
        evidence=ev,
        date=date,
        herdr_sha256=herdr_sha256,
        herdr_version=herdr_version,
        protocol=protocol,
        reduced_assurance=reduced_assurance,
    )
    click.echo(f"wrote {CERT_BLOCKS[axis]} row -> {store / f'{position}.md'}")
    click.echo(render_block(CERT_BLOCKS[axis], [row]))
    drift = installed_store_drift(position, provider, store)
    if drift:
        click.echo(f"WARNING: {drift}", err=True)
