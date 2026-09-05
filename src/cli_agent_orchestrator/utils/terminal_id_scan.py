"""Stale-terminal-id scanner for dispatch messages (F754, issue #611).

Canonical copy. A supervisor that dispatches after a compaction can carry a
PRE-RELAUNCH seat id in its summary and tell every worker to call back an id
whose terminal row no longer exists (incident 2026-09-04: seven lanes told to
report to ``terminal 5561a7d1`` while the live seat was ``34a7b2c1``). The MCP
``assign``/``handoff``/``send_message`` tools run :func:`guard` over the
outgoing message (and over every ``/data/cao-scratch/briefs/*.md`` it points
at) and refuse with :data:`ERROR_CODE` rather than posting work with an
unroutable callback address.

Rule set, in full: the caller's own id is fine; a LIVE foreign id is fine
(cross-worker references are legitimate); anything else is refused. A message
with no id in it is never refused, and an unavailable live set never refuses.

Deliberately dependency-free (stdlib only) so the root repo's PreToolUse hook
can carry a byte-identical twin without importing the fork.
"""

# --- BEGIN SHARED SCANNER (F754) ---
# This block is BYTE-IDENTICAL in both copies of the scanner:
#   fork: src/cli_agent_orchestrator/utils/terminal_id_scan.py
#   root: .claude/hooks/lib/terminal_id_scan.py
# The fork copy is canonical. The root copy exists because a Claude Code hook
# must not import the fork (the hook runs in the supervisor's shell, against a
# server that may predate this fix). Drift between the two is caught from BOTH
# sides by test_shared_blocks_are_identical, comparing exactly the text between
# these two markers. Regenerate both copies rather than hand-editing either.
import re
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

# A CAO terminal id is 8 lowercase hex chars (models/terminal.py: TerminalId).
# The lookaround pair stops a 40-char git sha or a longer hex blob from being
# read as an id; the char class stays case-sensitive on purpose.
_HEX8 = r"(?<![0-9a-fA-F])(?P<id>[0-9a-f]{8})(?![0-9a-fA-F])"

# Contextual citation rules. A bare 8-hex token is NEVER a citation: it has to
# be introduced by one of these keywords, or every short sha in a brief would
# be scanned as a terminal id. Keywords are case-insensitive (inline (?i:...)
# groups); the id itself is not.
CITATION_RULES: Tuple[Tuple[str, str], ...] = (
    (
        "terminal",
        r"(?i:\bterminals?\b)[\s:=_/-]{0,3}(?i:id)?[\s:=_/-]{0,3}['\"`]?" + _HEX8,
    ),
    (
        "id-kwarg",
        r"(?i:\b(?:receiver_id|caller_id|sender_id|terminal_id|to_terminal|CAO_TERMINAL_ID)\b)"
        r"[\s:=]{0,3}['\"`]?" + _HEX8,
    ),
    (
        "callback",
        r"(?i:\b(?:call\s?back|callback|reply|report|respond|hand\s?off)\b)\s+(?i:\bto\b)\s+"
        r"(?:(?i:the\s+)?(?i:\b(?:terminal|seat)\b)\s+)?['\"`]?" + _HEX8,
    ),
    ("seat", r"(?i:\bseats?\b)[\s:=]{0,3}['\"`]?" + _HEX8),
)

_COMPILED_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in CITATION_RULES
)

# Brief files the message may point at (issue #611 part 2). Only this directory
# and only .md — the guard reads what it is told to read and nothing else.
BRIEF_DIR = "/data/cao-scratch/briefs/"
BRIEF_PATH_RE = re.compile(r"/data/cao-scratch/briefs/[A-Za-z0-9._+-]+\.md")

# A brief larger than this is not read (a runaway file must not stall dispatch).
MAX_BRIEF_BYTES = 262144

ERROR_CODE = "E-STALE-TERMINAL-ID"

# Verdicts.
OWN = "own"  # the caller's own id — always fine
LIVE = "live"  # a live foreign id — legitimate cross-worker reference
STALE = "stale"  # cites an id no terminal row answers to — refuse
UNKNOWN = "unknown"  # live set unavailable — never refuse on infrastructure failure


class Citation(NamedTuple):
    """One terminal-id reference found in a piece of text."""

    terminal_id: str
    rule: str
    source: str  # "message" or an absolute brief path
    line: int  # 1-based
    snippet: str


class Finding(NamedTuple):
    """A citation plus the verdict the rule set gives it."""

    citation: Citation
    verdict: str


def find_citations(text: str, source: str = "message") -> List[Citation]:
    """Return every terminal-id citation in ``text``, in document order.

    Deduplicated on (terminal_id, line): the same id named twice on one line is
    one citation, the same id on two lines is two (both are worth naming in a
    refusal, because both are edits the caller has to make).
    """
    if not text:
        return []
    line_starts = [0]
    for match in re.finditer(r"\n", text):
        line_starts.append(match.end())

    def _line_of(pos: int) -> int:
        low, high = 0, len(line_starts) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if line_starts[mid] <= pos:
                low = mid
            else:
                high = mid - 1
        return low + 1

    seen: Set[Tuple[str, int]] = set()
    found: List[Tuple[int, Citation]] = []
    for rule_name, pattern in _COMPILED_RULES:
        for match in pattern.finditer(text):
            terminal_id = match.group("id")
            line_no = _line_of(match.start())
            key = (terminal_id, line_no)
            if key in seen:
                continue
            seen.add(key)
            start = line_starts[line_no - 1]
            end = text.find("\n", start)
            raw_line = text[start:] if end == -1 else text[start:end]
            snippet = raw_line.strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            found.append(
                (match.start(), Citation(terminal_id, rule_name, source, line_no, snippet))
            )
    found.sort(key=lambda item: item[0])
    return [citation for _, citation in found]


def find_brief_paths(text: str) -> List[str]:
    """Return the brief-file paths the message points at, in first-seen order."""
    if not text:
        return []
    ordered: List[str] = []
    seen: Set[str] = set()
    for match in BRIEF_PATH_RE.finditer(text):
        path = match.group(0)
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _default_reader(path: str) -> Optional[str]:
    """Read a brief, or return None when it cannot be read.

    A brief that is missing, unreadable, or oversized is SKIPPED, not refused:
    the guard's job is stale ids, and a nonexistent brief is a different bug.
    """
    if not path.startswith(BRIEF_DIR) or not path.endswith(".md"):
        return None
    try:
        import os

        if os.path.getsize(path) > MAX_BRIEF_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def classify(
    terminal_id: str,
    own_id: Optional[str],
    live_ids: Optional[Set[str]],
) -> str:
    """The whole rule set, in one place.

    own id -> ok; live foreign id -> ok; anything else -> refuse. When the live
    set could not be fetched (``live_ids is None``) nothing is refused.
    """
    if own_id and terminal_id == own_id:
        return OWN
    if live_ids is None:
        return UNKNOWN
    if terminal_id in live_ids:
        return LIVE
    return STALE


def collect_citations(
    message: str,
    reader: Optional[Callable[[str], Optional[str]]] = None,
) -> List[Citation]:
    """Every citation in a dispatch message AND in every brief it points at.

    Split out from :func:`scan` so a caller can learn WHICH ids are cited
    before deciding how to resolve liveness — a server that predates
    ``GET /terminals`` is probed one cited id at a time, and that needs the
    candidate set first. Briefs are read once, here.
    """
    read = reader if reader is not None else _default_reader
    citations = list(find_citations(message, "message"))
    for path in find_brief_paths(message):
        body = read(path)
        if body:
            citations.extend(find_citations(body, path))
    return citations


def candidate_ids(citations: Sequence[Citation]) -> Set[str]:
    """The distinct ids a message cites — what liveness has to be resolved for."""
    return {c.terminal_id for c in citations}


def verdicts(
    citations: Sequence[Citation],
    own_id: Optional[str],
    live_ids: Optional[Set[str]],
) -> List[Finding]:
    return [Finding(c, classify(c.terminal_id, own_id, live_ids)) for c in citations]


def scan(
    message: str,
    own_id: Optional[str],
    live_ids: Optional[Set[str]],
    reader: Optional[Callable[[str], Optional[str]]] = None,
) -> List[Finding]:
    """Scan a dispatch message plus every brief it points at.

    Returns ALL findings (including the fine ones) so a caller can report or
    log them; :func:`stale_findings` narrows to the ones that must refuse.
    """
    return verdicts(collect_citations(message, reader=reader), own_id, live_ids)


def stale_findings(findings: Sequence[Finding]) -> List[Finding]:
    return [finding for finding in findings if finding.verdict == STALE]


def format_refusal(
    stale: Sequence[Finding],
    own_id: Optional[str],
    live_ids: Optional[Set[str]],
    action: str = "message",
) -> str:
    """Render the structured refusal (issue #611).

    The caller's REAL id is in the text on purpose: the fix must be one edit
    away, not a lookup away.
    """
    caller = own_id or "<unset: CAO_TERMINAL_ID is not set>"
    live_list = ", ".join(sorted(live_ids)) if live_ids else ""
    cited: List[str] = []
    for finding in stale:
        citation = finding.citation
        where = "message" if citation.source == "message" else citation.source
        cited.append(f"{where}:{citation.line} cites {citation.terminal_id}")
    head = "; ".join(cited) if cited else "message cites a stale terminal id"
    lines = [
        f"{ERROR_CODE}: {head}; your id is {caller}; live ids: [{live_list}]",
        f"No {action} was sent. Every id above is neither your own nor a live terminal —",
        "it is almost certainly a pre-relaunch seat id carried over by a compaction",
        f"summary. Replace it with {caller} (or with the live id you meant) and retry.",
    ]
    for finding in stale:
        citation = finding.citation
        lines.append(f"  [{citation.rule}] {citation.source}:{citation.line}: {citation.snippet}")
    return "\n".join(lines)


def guard(
    message: str,
    own_id: Optional[str],
    live_ids: Optional[Set[str]],
    reader: Optional[Callable[[str], Optional[str]]] = None,
    action: str = "message",
) -> Optional[str]:
    """One-call entry point: refusal text, or None when the dispatch is fine."""
    stale = stale_findings(scan(message, own_id, live_ids, reader=reader))
    if not stale:
        return None
    return format_refusal(stale, own_id, live_ids, action=action)


def live_ids_from_rows(rows: Sequence[Dict[str, object]]) -> Set[str]:
    """Extract the id set from a ``GET /terminals`` payload.

    Liveness is "a terminal row answers to this id". The incident id was one
    whose row had been DELETED ("Terminal '5561a7d1' not found"), so row
    presence is exactly the discriminator; terminal status is deliberately not
    consulted (an ERROR-status terminal is still addressable).
    """
    ids: Set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = row.get("id") or row.get("terminal_id")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}", value):
            ids.add(value)
    return ids


# --- END SHARED SCANNER (F754) ---
