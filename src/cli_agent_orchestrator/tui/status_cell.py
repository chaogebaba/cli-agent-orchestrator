"""Pure renderer for one fleet STATUS cell.

The cell renders live status, delegation and typed conditions from the current
fleet projection. Unknown values remain visible rather than breaking refresh.
"""

from __future__ import annotations

from typing import Any, Dict, Final, Mapping, Tuple

from rich.text import Text

# ─── Styles ported from scripts/fleet-tui.py:229-242 ──────────────────────────
STYLE_WORKING: Final[str] = "green"
STYLE_WAITING: Final[str] = "yellow"
STYLE_QUIET: Final[str] = "dim"
STYLE_DELEGATING: Final[str] = "cyan"
#: Anything outside the enumerated vocabularies — visible, never fatal.
STYLE_UNKNOWN_VALUE: Final[str] = "magenta"

# ─── status → (glyph + text, style) ───────────────────────────────────────────
# Every TerminalStatus value (models/terminal.py:23-32). Glyph/colour are the
# script's: ● green for processing, ◌ yellow for the two waiting-ish states,
# · dim for the rest.
_STATUS_CELLS: Final[Dict[str, Tuple[str, str]]] = {
    "processing": ("● working", STYLE_WORKING),
    "waiting_user_answer": ("◌ waiting", STYLE_WAITING),
    "idle": ("◌ idle", STYLE_WAITING),
    "completed": ("· completed", STYLE_QUIET),
    "error": ("· error", STYLE_QUIET),
    "unknown": ("· unknown", STYLE_QUIET),
    "render_uncertain": ("· render_uncertain", STYLE_QUIET),
}

# ─── condition → style ────────────────────────────────────────────────────────
# The three labels the delivery layer rewrites (CAPPED/BLOCKED/AUTH) plus the
# raw ConditionKind values it passes through. The two rewritten kinds
# (DIALOG_BLOCKED, AUTH_EXPIRED) cannot reach the wire today; they are mapped
# anyway so a change to _fleet_label cannot make a live condition render as
# unknown.
_CONDITION_STYLES: Final[Dict[str, str]] = {
    "CAPPED": "bold red",
    "BLOCKED": "bold red",
    "DIALOG_BLOCKED": "bold red",
    "AUTH": "bold red",
    "AUTH_EXPIRED": "bold red",
    "PROC_EXITED": "bold red",
    "NET_INTERRUPTED": "yellow",
    "CONTEXT_EXHAUSTED": "yellow",
    "TRANSIENT_OVERLOAD": "yellow",
    "BUSY": "green",
    "WAITING_ON_SUBAGENTS": "yellow",
}

#: Rendered when ``status`` is missing or empty — the script's ``or "?"`` branch.
_MISSING_STATUS: Final[Tuple[str, str]] = ("· ?", STYLE_QUIET)

#: Conditions whose ``[TAG]`` recedes instead of shouting. ``BUSY`` is the
#: high-frequency one — nearly every working seat carries it — so it rides as a
#: dim span over the cell's own colour rather than competing with the status
#: word for attention. The cell's ``style`` is untouched: the condition still
#: owns it (D4/B12), only the tag's own glyphs are dimmed.
_QUIET_CONDITION_TAGS: Final[frozenset[str]] = frozenset({"BUSY"})
#: The overlay applied to those tags.
STYLE_QUIET_TAG: Final[str] = "dim"

#: F752 (#609): condition labels that CONTRADICT a quiescent status. ``BUSY``
#: says the seat is working right now, so ``◌ idle [BUSY]`` is not a row an
#: operator can act on — it is two halves of the projection disagreeing. The
#: server no longer writes it, and this is the defence in depth: whatever the
#: wire carries, the cell never renders the contradiction.
_LIVE_WORK_CONDITIONS: Final[frozenset[str]] = frozenset({"BUSY"})
#: The statuses that make those labels a contradiction.
_QUIESCENT_STATUSES: Final[frozenset[str]] = frozenset({"idle", "completed"})

# ─── F777 (#634) scope add: a typed condition HEADLINES the STATUS cell ───────
#
# User word 2026-09-06: "when codex capped it will show capped, not completed".
# A typed, non-BUSY condition (already confidence-filtered to high/medium by the
# delivery seam, providers/condition.py §2.3) is the operator-actionable fact —
# a capped seat that reads `· completed` hides exactly what the operator needs
# to see. So such a condition REPLACES the bare status word as the cell's
# headline (`⚠ CAPPED`), styled by the condition, with the raw status kept
# visible as a dim parenthetical (`⚠ CAPPED (completed)`) and still carried in
# the COND column and the peek detail. BUSY is the one exception: it asserts
# LIVE WORK, not a stall, so it stays a dim `[BUSY]` tag on the status word
# (`● working [BUSY]`) exactly as before — never a headline.
#:
#: The glyph that opens a headlined condition. `⚠` reads as "attention" at a
#: glance without claiming which condition it is (the word says that).
_CONDITION_GLYPH: Final[str] = "⚠"
#: Conditions that stay a suffix tag rather than headlining. BUSY only, today.
_TAG_ONLY_CONDITIONS: Final[frozenset[str]] = frozenset({"BUSY"})
#: The bare status WORD (no glyph) shown in the `(…)` parenthetical when a
#: condition headlines the cell. Falls back to the raw status string for an
#: unknown value.
_STATUS_WORDS: Final[Dict[str, str]] = {
    "processing": "working",
    "waiting_user_answer": "waiting",
    "idle": "idle",
    "completed": "completed",
    "error": "error",
    "unknown": "unknown",
    "render_uncertain": "render_uncertain",
}
#: The style of the raw-status parenthetical — recessive, so the condition word
#: owns the operator's attention while the raw status stays legible.
STYLE_RAW_STATUS: Final[str] = "dim"


#: F792 (#649): the EXPECTED "seat idle-waiting on its own background agents"
#: condition. It is NOT a stall and NOT live work — it renders as a calm
#: ``· waiting`` headline (its own quiet style), never the loud ``⚠`` headline a
#: CAPPED/AUTH condition gets and never a ``[TAG]`` suffix. Rendered on an
#: idle/completed seat by construction, so it is deliberately absent from
#: ``_LIVE_WORK_CONDITIONS`` (it does not contradict a quiescent status).
_WAITING_ON_SUBAGENTS: Final[str] = "WAITING_ON_SUBAGENTS"
#: The cell text + style for the waiting-on-subagents condition.
_WAITING_ON_SUBAGENTS_CELL: Final[Tuple[str, str]] = ("· waiting", STYLE_WAITING)


def _base_cell(row: Mapping[str, Any]) -> Tuple[str, str]:
    """The status half of the cell: (text, style), before any condition suffix.

    Precedence: ``delegating`` (F568 D12c — the server has already
    restricted it to IDLE/COMPLETED seats with children, so this branch never
    hides a working or errored seat), then the status vocabulary.
    """
    if row.get("delegating"):
        count = row.get("children_count")
        n = count if isinstance(count, int) and not isinstance(count, bool) else 0
        return f"◇ delegating ({n})", STYLE_DELEGATING
    raw = row.get("status")
    if not raw:
        return _MISSING_STATUS
    status = str(raw)
    known = _STATUS_CELLS.get(status)
    if known is not None:
        return known
    return f"? {status}", STYLE_UNKNOWN_VALUE


def _contradicts_status(raw_status: Any, raw_condition: Any) -> bool:
    """True when the condition claims live work but the status says otherwise.

    Read off the row's raw ``status``, not the rendered cell: ``delegating``
    rewrites the text but not the underlying status, and a
    delegating seat is IDLE/COMPLETED by construction — its stale ``BUSY`` is
    exactly as wrong there as on a plain idle row.
    """
    if not raw_status:
        return False
    return str(raw_status) in _QUIESCENT_STATUSES and str(raw_condition) in _LIVE_WORK_CONDITIONS


def _raw_status_word(row: Mapping[str, Any]) -> str:
    """The bare status word for a headlined condition's ``(…)`` parenthetical.

    Always the row's underlying ``status`` (never the ``delegating`` rewrite),
    because that is the raw status the operator
    wants preserved when a condition takes the headline. Unknown/empty status
    falls back to the raw string so nothing is silently dropped.
    """
    raw = row.get("status")
    if not raw:
        return "?"
    status = str(raw)
    return _STATUS_WORDS.get(status, status)


def status_cell(row: Mapping[str, Any]) -> Text:
    """Render one fleet row's STATUS cell.

    Args:
        row: one entry of the ``terminals`` list from ``build_fleet()``. Only
            ``status``, ``condition``, ``delegating`` and ``children_count``
            are read; every key is optional and any value may
            be of an unexpected type.

    Returns:
        A :class:`rich.text.Text` carrying glyph, label and whole-cell style.
        Never raises.

    F777 (#634) scope add: a typed, non-BUSY condition HEADLINES the cell —
    ``⚠ CAPPED (completed)`` rather than ``· completed [CAPPED]`` — styled by
    the condition, with the raw status word kept as a dim parenthetical. BUSY
    stays a dim ``[BUSY]`` suffix tag on the status word because it asserts
    live work rather than an actionable stall.
    """
    text, style = _base_cell(row)
    raw_condition = row.get("condition")
    if raw_condition and _contradicts_status(row.get("status"), raw_condition):
        raw_condition = None
    if not raw_condition:
        return Text(text, style=style)

    # F792 (#649): the waiting-on-subagents condition renders as a calm
    # `· waiting` headline (never `⚠`, never a `[TAG]` suffix).
    if str(raw_condition) == _WAITING_ON_SUBAGENTS:
        wait_text, wait_style = _WAITING_ON_SUBAGENTS_CELL
        return Text(wait_text, style=wait_style)

    condition = str(raw_condition)
    condition_style = _CONDITION_STYLES.get(condition)
    tag_only = condition in _TAG_ONLY_CONDITIONS

    # BUSY keeps the status word and appends the tag because it is live work
    # rather than an actionable stall.
    if tag_only:
        if condition_style is None:
            suffix = f" [? {condition}]"
            cell_style = STYLE_UNKNOWN_VALUE
        else:
            suffix = f" [{condition}]"
            cell_style = condition_style
        base_length = len(text)
        text = f"{text}{suffix}"
        cell = Text(text, style=cell_style)
        if condition in _QUIET_CONDITION_TAGS:
            cell.stylize(STYLE_QUIET_TAG, base_length, len(text))
        return cell

    # A non-BUSY condition HEADLINES: `⚠ CAPPED (completed)`, condition-styled,
    # with the raw status word kept legible-but-recessive in the parenthetical.
    if condition_style is None:
        head = f"{_CONDITION_GLYPH} ? {condition}"
        cell_style = STYLE_UNKNOWN_VALUE
    else:
        head = f"{_CONDITION_GLYPH} {condition}"
        cell_style = condition_style
    paren = f" ({_raw_status_word(row)})"
    cell = Text(f"{head}{paren}", style=cell_style)
    cell.stylize(STYLE_RAW_STATUS, len(head), len(head) + len(paren))
    return cell
