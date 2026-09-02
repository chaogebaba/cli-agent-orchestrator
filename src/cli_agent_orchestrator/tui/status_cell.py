"""F702 (#557) D4: the fleet STATUS cell, as one pure function.

:func:`status_cell` maps a single terminal row of the ``build_fleet()``
projection (``services/fleet_service.py:236-272``) to one
:class:`rich.text.Text` carrying glyph, text, and style together. It is the
object the ``DataTable`` stores, so ``get_cell_at(...)`` returns it and tests
assert ``.plain`` and ``.style`` (blueprint B12).

**Pure.** No I/O, no clock, no cross-fetch state (D2): the same row always
renders the same cell. Nothing here raises — an unrecognised ``status`` or
``condition`` renders visibly as ``? <value>`` rather than blowing up a table
refresh.

Vocabulary covered, enumerated from the server:

* ``status`` — every :class:`~cli_agent_orchestrator.models.terminal.TerminalStatus`
  value (``models/terminal.py:23-32``): ``unknown``, ``idle``, ``processing``,
  ``completed``, ``waiting_user_answer``, ``render_uncertain``, ``error``.
* ``condition`` — the F611 (#467) fleet labels published by
  ``ConditionDelivery._fleet_label`` (``providers/condition.py:743-752``):
  ``CAPPED``, ``BLOCKED`` (from ``DIALOG_BLOCKED``), ``AUTH`` (from
  ``AUTH_EXPIRED``), and the remaining
  :class:`~cli_agent_orchestrator.providers.condition.ConditionKind` values
  verbatim — ``NET_INTERRUPTED``, ``CONTEXT_EXHAUSTED``, ``PROC_EXITED``,
  ``TRANSIENT_OVERLOAD``, ``BUSY``. ``None`` means no condition. The two raw
  kinds that the label mapping rewrites are accepted defensively as well.
* ``delegating`` / ``children_count`` — F568 D12c: an IDLE/COMPLETED seat with
  children in flight renders ``delegating (N)``.
* ``wedge_suspect`` — F295 Half 2 AC10.

Parity with the retiring stdlib script (root repo ``scripts/fleet-tui.py:229-242``)
is held on the **glyph and colour** of every ``status`` value and of
``wedge_suspect``. One deliberate divergence: that script truncates the status
name to eight characters (``f"· {st[:8]}"``) because it draws fixed-width ANSI,
which renders ``render_uncertain`` as ``render_u``. A ``DataTable`` sizes its own
columns, so the full value is written here.

``fusion_changed`` is **not** rendered in this cell — D3 gives it its own ``*``
column.
"""

from __future__ import annotations

from typing import Any, Dict, Final, Mapping, Tuple

from rich.text import Text

# ─── Styles ported from scripts/fleet-tui.py:229-242 ──────────────────────────
STYLE_WEDGE: Final[str] = "bold red"
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


def _base_cell(row: Mapping[str, Any]) -> Tuple[str, str]:
    """The status half of the cell: (text, style), before any condition suffix.

    Precedence: ``wedge_suspect`` (the script checks it first and it outranks
    every status), then ``delegating`` (F568 D12c — the server has already
    restricted it to IDLE/COMPLETED seats with children, so this branch never
    hides a working or errored seat), then the status vocabulary.
    """
    if row.get("wedge_suspect"):
        return "x WEDGE?", STYLE_WEDGE
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


def status_cell(row: Mapping[str, Any]) -> Text:
    """Render one fleet row's STATUS cell.

    Args:
        row: one entry of the ``terminals`` list from ``build_fleet()``. Only
            ``status``, ``condition``, ``delegating``, ``children_count`` and
            ``wedge_suspect`` are read; every key is optional and any value may
            be of an unexpected type.

    Returns:
        A :class:`rich.text.Text` whose ``.plain`` is the glyph plus label
        (plus ``[CONDITION]`` when the row carries one) and whose ``.style`` is
        the whole-cell style. Never raises.
    """
    text, style = _base_cell(row)
    raw_condition = row.get("condition")
    quiet_tag = False
    if raw_condition:
        condition = str(raw_condition)
        condition_style = _CONDITION_STYLES.get(condition)
        if condition_style is None:
            suffix = f" [? {condition}]"
            condition_style = STYLE_UNKNOWN_VALUE
        else:
            suffix = f" [{condition}]"
            quiet_tag = condition in _QUIET_CONDITION_TAGS
        base_length = len(text)
        text = f"{text}{suffix}"
        # A wedge is the loudest thing on the row; nothing overrides its style.
        if style != STYLE_WEDGE:
            style = condition_style
        cell = Text(text, style=style)
        if quiet_tag:
            cell.stylize(STYLE_QUIET_TAG, base_length, len(text))
        return cell
    return Text(text, style=style)
