"""F702 (#557) D3: the `cao-fleet` Textual app — table, widgets, key bindings.

The app owns no fleet logic. It composes three pure/tested pieces:

* :mod:`cli_agent_orchestrator.tui.fleet_state` — the immutable snapshot (J1),
* :mod:`cli_agent_orchestrator.tui.fetcher` — the sequential poll loop (J1),
* :mod:`cli_agent_orchestrator.tui.status_cell` — the STATUS cell (J3),

and adds only rendering plus the key bindings the retiring stdlib script has
today (root repo ``scripts/fleet-tui.py:598-619``), verbatim.

**Renderer memory (D2).** Widgets read the current :class:`FleetState` only.
Nothing per-terminal is derived across fetches, so #439's sticky-latch class
cannot recur here. IDLE comes from tmux ``window_activity`` on each tick, as
the script does (``fleet-tui.py:115-118``). The single exception is the
working-elapsed readout below, which remembers when a spell started because no
server field records it; it never feeds ``status`` and a row that stops working
drops its spell on the next frame, so it cannot latch.

**Side effects (AC4).** Every subprocess goes through one injectable runner.
Exactly one of them mutates anything: ``tmux select-window`` on jump
(``fleet-tui.py:484``). ``list-windows``, ``list-panes``, ``capture-pane`` and
``display-message`` are reads. The one other child process this app starts is
the events-feed sync script (``fleet-tui.py:203-226``), which writes only under
``/data/cao-scratch``.

**Failure posture (AC1).** A fetch failure is *staleness*, never an error row
(#441): the last good rows stay on screen and the header badge grows a
``stale Ns`` suffix. Before the first successful fetch the badge reads
``not yet fetched`` (``FleetState.fetched_at is None``). A failure is also
*loud*: a red ``fleet endpoint unreachable`` line appears above the table with
the time of the last good fetch (``fleet-tui.py:334-335``).

**Parity restorations (F702 #557 parity round).** Against the retiring script
this module now also carries: the selection gutter ``▶`` (``:399``), the row
colour language — supervisor magenta, worker id cyan, IDLE age colouring,
unlabeled task dim (``:245-250,410-415``) — the on-screen key hints
(``:430-433``), the clock and fetch latency in the header (``:338-339``), the
throttled ``fleet-events-sync.sh`` trigger (``:203-226``), the 20-entry error
ring (``:82-84``) with its debug and snapshot surfaces, and the ``--interval`` /
``--once`` / tmux-session-auto-detect launch contract (``:548-565``).

**Section layout (F702 #557 "look" round).** The frame is the script's, section
for section and blank line for blank line (``:336-450``): the ``▌`` header, the
table under its own header row and thin rule, ``▌ recent``, one status line,
the key hints, and — last, filling whatever height is left — ``▌ peek`` under
its double rule. The peek is the *bottom* section, not a band in the middle,
and ``test_the_sections_are_in_the_scripts_order_with_the_peek_last`` is what
holds it there. Chrome is the script's too: the ``▌`` mark opens all three
section titles, the accents are its two (``:62-65``), and no widget draws a
border of its own, so the only horizontal lines on screen are the two rules.

**Working-elapsed readout (same round).** "How long has this subagent been
working?" is answered in the IDLE column: a working row renders ``● 12m`` in
green instead of a window-activity age that is always near zero while output is
flowing. The column keeps its name, its position and its idle behaviour for
every other row. No server field carries the spell's start today — see the
readout's own note below for the ``status_since`` key that should — so it is
measured from the first poll that saw the row working and a spell already
running when the app started renders ``12m+``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Final,
    List,
    Mapping,
    Sequence,
    Tuple,
)

# The Textual dependency is the optional `fleet` extra (D1/B10): a server-only
# install carries no TUI library. Importing this module without it is a clean
# one-line exit, not a traceback -- the console script's only job is to say
# which extra is missing.
try:  # pragma: no cover - exercised by the install path, not the suite
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.coordinate import Coordinate
    from textual.message import Message
    from textual.widgets import DataTable, Static
except ModuleNotFoundError as exc:  # pragma: no cover - same
    raise SystemExit(
        "cao-fleet needs the 'fleet' extra (Textual is not installed).\n"
        "    uv tool install --force 'cli-agent-orchestrator[fleet]'\n"
        f"(import failed: {exc})"
    ) from exc

from rich.text import Text

from cli_agent_orchestrator.tui.columns import (
    ALL_COLUMNS,
    ALL_VIEW,
    ELAPSED_COLUMN,
    MARKER_BLANK,
    MARKER_INDEX,
    MARKER_SELECTED,
    PARITY_COLUMNS,
    PARITY_VIEW,
)
from cli_agent_orchestrator.tui.fetcher import FETCH_INTERVAL, fetch_json, run_fetch_loop
from cli_agent_orchestrator.tui.fleet_state import FleetState, StatusClock, TerminalState
from cli_agent_orchestrator.tui.status_cell import status_cell

__all__ = [
    "FleetApp",
    "FleetUpdated",
    "hint_text",
    "is_working",
    "main",
    "render_once",
    "elapsed_cell",
]

DEFAULT_ENDPOINT = "http://127.0.0.1:9889"
SCRATCH = Path("/data/cao-scratch")
LABELS_PATH = SCRATCH / "fleet-labels.tsv"
EVENTS_PATH = SCRATCH / "fleet-events.log"
SNAPSHOT_DIR = SCRATCH / "fleet-snapshots"

EVENTS_TAIL = 6
PEEK_LINES_DEFAULT = 14
PEEK_LINES_MIN = 6
PEEK_LINES_MAX = 40
PEEK_STEP = 4

#: Rolling TUI-error ring, as the script keeps it (``fleet-tui.py:82-84``).
ERROR_RING_SIZE = 20
#: How many of them the debug overlay shows (``fleet-tui.py:423``).
ERROR_RING_SHOWN = 8

# ─── Row colour language, ported from scripts/fleet-tui.py:245-250,410-415 ────
#: Supervisor rows: WIN/PROFILE magenta, ID magenta+bold (``:411-412``).
STYLE_SUPERVISOR: Final[str] = "magenta"
STYLE_SUPERVISOR_ID: Final[str] = "bold magenta"
#: Worker rows: WIN dim, ID cyan (``:414-415``).
STYLE_WORKER_ID: Final[str] = "cyan"
STYLE_DIM: Final[str] = "dim"
#: IDLE column, ``idle_color`` (``:245-250``): dim under 5 s, yellow at 5 min.
STYLE_IDLE_STALE: Final[str] = "yellow"
IDLE_FRESH_SECONDS: Final[float] = 5.0
IDLE_STALE_SECONDS: Final[float] = 300.0
#: The selection gutter glyph.
STYLE_MARKER: Final[str] = "bold"
#: The loud server-down line (``:335``).
STYLE_ALERT: Final[str] = "bold red"

# ─── Section chrome, ported from scripts/fleet-tui.py:53-68,336-449 ───────────
# The script's two-accent scheme: ACCENT2 titles every section, ACCENT1 (blue)
# is the selection bar. Changing these two constants rethemes the whole view,
# exactly as the comment at ``fleet-tui.py:62-64`` promises.
#: ``fleet-tui.py:65`` — the user-picked light-yellow swatch, verbatim.
ACCENT_TITLE: Final[str] = "rgb(250,242,93)"
#: ``fleet-tui.py:63`` — the selection bar. The script writes ANSI blue under
#: reverse video; Textual's ``ansi_blue`` is remapped by the active theme (it
#: renders purple on the default one), so the bar names the colour outright.
ACCENT_SELECTION: Final[str] = "#0000cd"
#: The glyph that opens every section title (``:337,419,441``).
SECTION_MARK: Final[str] = "▌"
#: Section titles: ``▌ CAO fleet``, ``▌ recent``, ``▌ peek``.
STYLE_SECTION: Final[str] = f"bold {ACCENT_TITLE}"
#: The dim tail of the header line — clock, worker count, latency, badge.
STYLE_SECTION_TAIL: Final[str] = "dim"
#: The badge turns yellow once the snapshot is older than one poll interval.
STYLE_STALE: Final[str] = "yellow"
#: The table's own header row (``:372``) and the thin rule under it (``:373``).
STYLE_TABLE_HEADER: Final[str] = "dim bold"
STYLE_RULE: Final[str] = "dim"
RULE_GLYPH: Final[str] = "─"
#: The peek banner's double rule (``:445``), in the title accent.
PEEK_RULE_GLYPH: Final[str] = "═"
#: Recent-event lines: dim, indented two spaces (``:420``).
STYLE_EVENT: Final[str] = "dim"
EVENT_INDENT: Final[str] = "  "
#: The key hint line (``:432``): bold key, dim label, dim separator.
STYLE_HINT_KEY: Final[str] = "bold"
STYLE_HINT_LABEL: Final[str] = "dim"
HINT_SEPARATOR: Final[str] = " · "
#: Two spaces between columns and two for the selection gutter (``:361,374``).
COLUMN_GUTTER: Final[int] = 2
GUTTER_WIDTH: Final[int] = 2
#: A last-resort width when the screen has not been sized yet.
FALLBACK_WIDTH: Final[int] = 80
#: How many lines of section chrome the peek pane spends before the capture:
#: the banner and the double rule.
PEEK_CHROME_LINES: Final[int] = 2

# ─── The ELAPSED column (F702 #557, "look" then "elapsed" rounds) ────────────
# The column shows how long each seat has been in the status it is in now:
# working, idle, errored or completed alike. It replaces the script's
# window-activity age, which was seconds since the pane last printed something
# and so read near zero for exactly the seats that were busiest.
#
# Where the number comes from — and the field the server should grow — is
# documented on :class:`~cli_agent_orchestrator.tui.fleet_state.StatusClock`,
# which owns the tracking. This module only decides how the number looks.
#
# Working rows are marked so the column is readable at a glance without a
# second column: the STATUS cell's own ``●`` and its green, against dim for
# every other state. That is the whole visual difference, and it is the same
# two-signal language the script used for status (``fleet-tui.py:229-242``).
#: The status that means a turn is open.
WORKING_STATUS: Final[str] = "processing"
#: The provider condition that can mean the same, where the status is silent.
WORKING_CONDITION: Final[str] = "BUSY"
#: Statuses that positively assert the seat is at rest. A ``BUSY`` condition on
#: one of these is a stale provider flag the fusion has overtaken, never a
#: reason to call the seat working — see :func:`is_working`.
RESTING_STATUSES: Final[frozenset[str]] = frozenset({"idle", "waiting_user_answer", "error"})
#: The glyph the STATUS cell already uses for a working seat (``:236``).
WORKING_GLYPH: Final[str] = "●"
#: Appended when the status began before this app was watching — a lower bound.
WORK_APPROX_SUFFIX: Final[str] = "+"
#: The ELAPSED cell's style while a seat is working — the status colour.
STYLE_WORKING: Final[str] = "green"
#: Rendered when the clock has no spell for a row (only before its first fold).
ELAPSED_UNKNOWN: Final[str] = "-"
#: How often the ELAPSED column re-renders, independently of the fetch loop.
ELAPSED_TICK_SECONDS: Final[float] = 1.0

#: The tmux verbs this app is allowed to run. Only ``select-window`` mutates
#: anything; the rest are reads (AC4).
TMUX_READS = frozenset({"list-windows", "list-panes", "capture-pane", "display-message"})
TMUX_WRITES = frozenset({"select-window"})

#: ``(session, args) -> stdout``; ``None`` means the call failed.
Runner = Callable[[Sequence[str]], "str | None"]

# ─── Events-feed sync (F481, ported from fleet-tui.py:193-226) ────────────────
#: Minimum gap between two ``fleet-events-sync.sh`` firings.
EVENTS_SYNC_INTERVAL = 30.0
EVENTS_SYNC_SCRIPT_NAME = "fleet-events-sync.sh"
#: Explicit path override, for a deployment that keeps the script elsewhere.
EVENTS_SYNC_ENV = "CAO_FLEET_EVENTS_SYNC"

#: On-screen key hints (``fleet-tui.py:430-433``), plus `c` for the new columns.
#: ``test_fleet_app`` asserts every binding key is covered by one of these, so
#: a new binding cannot land without a hint.
KEY_HINTS: Final[Tuple[Tuple[str, str], ...]] = (
    ("↑↓/jk", "select"),
    ("⏎/o", "jump"),
    ("p", "peek"),
    ("+/-", "size"),
    ("g/G", "ends"),
    ("c", "columns"),
    ("d", "debug"),
    ("s", "snapshot"),
    ("q", "quit"),
)


def hint_text() -> str:
    """The one-line key hint, as the script draws it (``fleet-tui.py:432``)."""
    return HINT_SEPARATOR.join(f"{key} {label}" for key, label in KEY_HINTS)


def hint_renderable() -> Text:
    """:func:`hint_text` with the script's emphasis: bold key, dim label.

    ``fleet-tui.py:432`` writes ``BOLD key`` + ``DIM label`` joined by a dim
    ``·``. The plain text is byte-for-byte :func:`hint_text`, so the one-shot
    frame and the live view cannot drift.
    """
    line = Text(no_wrap=True)
    for index, (key, label) in enumerate(KEY_HINTS):
        if index:
            line.append(HINT_SEPARATOR, style=STYLE_HINT_LABEL)
        line.append(key, style=STYLE_HINT_KEY)
        line.append(f" {label}", style=STYLE_HINT_LABEL)
    return line


def column_widths(headers: Sequence[str], values: Sequence[Sequence[str]]) -> List[int]:
    """The script's column rule: ``max(header, widest cell) + 2`` (``:361-370``).

    Shared by the live table and by ``--once`` so a column can never be two
    widths wide in the two renderings.
    """
    return [
        max([len(header)] + [len(row[index]) for row in values if index < len(row)]) + COLUMN_GUTTER
        for index, header in enumerate(headers)
    ]


def header_line(headers: Sequence[str], widths: Sequence[int]) -> str:
    """The table's header row: a two-space gutter, then each name left-justified."""
    return " " * GUTTER_WIDTH + "".join(name.ljust(width) for name, width in zip(headers, widths))


def run_tmux(args: Sequence[str]) -> str | None:
    """Default runner: one ``tmux`` invocation, 3 s cap, never raises.

    Mirrors ``fleet-tui.py:100-112`` — a failed tmux call degrades the widget
    that asked for it, it does not break the frame.
    """
    try:
        done = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def spawn_detached(script: Path) -> "subprocess.Popen[bytes]":
    """Start ``script`` in its own session, discarding its streams.

    The default spawner for the events sync (``fleet-tui.py:217-224``): never
    waited on, never inherits the TUI's terminal.
    """
    return subprocess.Popen(
        [str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def find_events_sync_script(events_path: Path = EVENTS_PATH) -> Path | None:
    """Locate ``fleet-events-sync.sh``, or ``None`` when it is not deployed.

    The script lives in the supervising repo, not in this package, so the path
    cannot be derived from ``__file__`` the way it is in the stdlib script
    (``fleet-tui.py:198-200``). Three candidates, in order:

    1. ``$CAO_FLEET_EVENTS_SYNC`` — an explicit path, honoured even if broken
       (a set-but-wrong override is a misconfiguration worth surfacing as "no
       sync", not something to paper over with a fallback);
    2. a sibling of the events log itself (``/data/cao-scratch``), which is the
       directory both halves of the contract already share;
    3. anything of that name on ``PATH``.
    """
    override = os.environ.get(EVENTS_SYNC_ENV)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    sibling = events_path.parent / EVENTS_SYNC_SCRIPT_NAME
    if sibling.is_file():
        return sibling
    found = shutil.which(EVENTS_SYNC_SCRIPT_NAME)
    return Path(found) if found else None


def detect_tmux_session(runner: Runner = run_tmux) -> str | None:
    """The tmux session this process runs inside (``fleet-tui.py:548-557``).

    ``None`` outside tmux, or when tmux cannot answer — the caller turns that
    into the "pass --session" exit.
    """
    if not os.environ.get("TMUX"):
        return None
    out = runner(["display-message", "-p", "#S"])
    if not out:
        return None
    return out.strip() or None


def fmt_age(seconds: float | None) -> str:
    """Seconds since last pane output, as the script formats it (``:253-261``)."""
    if seconds is None:
        return "-"
    whole = int(seconds)
    if whole < 120:
        return f"{whole}s"
    if whole < 7200:
        return f"{whole // 60}m"
    return f"{whole // 3600}h"


def is_working(term: TerminalState) -> bool:
    """Whether this seat is *currently working*, for the IDLE cell's purposes.

    The fused ``status`` decides, and the ``BUSY`` condition (F611 #467) breaks
    only the ties it leaves:

    * ``processing`` is working, full stop;
    * ``idle``, ``waiting_user_answer`` and ``error`` are **not**, whatever the
      condition says. These are positive statements of rest, and a live fleet
      does show ``◌ idle [BUSY]`` — a provider BUSY flag the fusion has already
      overtaken. Trusting it there would put a climbing green clock next to the
      word "idle" one column over;
    * ``completed``, ``unknown`` and ``render_uncertain`` are not statements of
      rest — the turn ended, or could not be read — so a ``BUSY`` condition is
      allowed to mean the turn is in fact still open. This is the case that
      matters: a working ``claude_code`` seat regularly renders
      ``· completed [BUSY]`` while the fusion catches up, and an operator asking
      "how long has this been going" wants the answer then too.

    The rule's whole point is that this readout never contradicts the STATUS
    cell beside it.
    """
    if term.status == WORKING_STATUS:
        return True
    if term.status in RESTING_STATUSES:
        return False
    return term.condition == WORKING_CONDITION


def elapsed_cell(seconds: float, exact: bool, working: bool) -> str:
    """One ELAPSED cell: ``● 12m`` for a working seat, ``12m`` for any other.

    ``+`` marks a lower bound — the seat was already in this status the first
    time the clock saw it, so the transition into it was never witnessed.
    """
    glyph = f"{WORKING_GLYPH} " if working else ""
    return f"{glyph}{fmt_age(seconds)}" + ("" if exact else WORK_APPROX_SUFFIX)


def read_labels(path: Path) -> Dict[str, str]:
    """``fleet-labels.tsv`` as ``{terminal_id: label}`` (``fleet-tui.py:172-182``).

    Strictly two tab-separated fields; a line without a tab is skipped, and an
    unreadable file yields no labels rather than an error.
    """
    labels: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return labels
    for line in text.splitlines():
        if "\t" in line:
            tid, label = line.split("\t", 1)
            labels[tid.strip()] = label.strip()
    return labels


def read_events(path: Path, n: int = EVENTS_TAIL) -> List[str]:
    """Last ``n`` lines of the events log (``fleet-tui.py:185-190``)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return lines[-n:]


def read_window_ages(
    tmux: Runner, session: str, *, now: Callable[[], float] = time.time
) -> Dict[str, float]:
    """``{window_index: seconds since last output}`` (``fleet-tui.py:115-118``)."""
    out = tmux(["list-windows", "-t", session, "-F", "#{window_index} #{window_activity}"])
    ages: Dict[str, float] = {}
    if not out:
        return ages
    wall = now()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            ages[parts[0]] = max(0.0, wall - int(parts[1]))
    return ages


def sort_terminals(terminals: Sequence[TerminalState]) -> List[TerminalState]:
    """Supervisors first, then workers, each by window index.

    Parity with ``get_workers`` (``fleet-tui.py:264-270``), including its
    *string* sort key and its ``"9"`` default for a terminal with no window.
    """

    def key(term: TerminalState) -> str:
        return str(term.window_index if term.window_index is not None else "9")

    supervisors = [t for t in terminals if not t.parent_id]
    workers = [t for t in terminals if t.parent_id]
    return sorted(supervisors, key=key) + sorted(workers, key=key)


def status_row(term: TerminalState) -> Mapping[str, Any]:
    """The row mapping :func:`status_cell` reads, projected from a `TerminalState`.

    Deliberately narrow: `status_cell` is documented to read exactly these five
    keys, and building the projection by hand keeps ``TerminalState.extra``
    (a read-only mapping) out of a deep copy.
    """
    return {
        "status": term.status,
        "condition": term.condition,
        "delegating": term.delegating,
        "children_count": term.children_count,
        "wedge_suspect": term.wedge_suspect,
    }


def window_key(term: TerminalState) -> str:
    """The WIN cell's text: the tmux window index, or ``?`` when there is none.

    ``window_index`` arrives from the server as a *string*
    (``clients/tmux.py:1423``); :func:`~cli_agent_orchestrator.tui.fleet_state`
    normalises it to ``int | None``, and ``None`` is the only case that renders
    ``?``. Window ``0`` is a real window and must never render as ``?`` — the
    stdlib script's ``or "?"`` (``fleet-tui.py:351``) got that wrong.
    """
    return str(term.window_index) if term.window_index is not None else "?"


def row_values(
    term: TerminalState,
    labels: Mapping[str, str],
    *,
    with_new_columns: bool = False,
    elapsed_cells: Mapping[str, str] | None = None,
) -> List[str]:
    """Every cell of one row as plain text, gutter excluded.

    Shared by the Textual table (which wraps each value in a styled
    :class:`~rich.text.Text`) and by ``--once``, so the two renderings cannot
    disagree about what a row says.

    ``elapsed_cells`` maps a terminal id to its ELAPSED readout (from
    :func:`elapsed_cell`), which is the app's :class:`StatusClock` rendered.
    ``--once`` is a single fetch with no history to time a transition against,
    so it passes nothing and every ELAPSED cell reads ``-``: an honest "not
    known from one frame" rather than a window-activity age wearing the new
    column's name.
    """
    default_label = "(supervisor seat)" if not term.parent_id else "(unlabeled)"
    elapsed = ELAPSED_UNKNOWN
    if elapsed_cells is not None:
        elapsed = elapsed_cells.get(term.id, ELAPSED_UNKNOWN)
    values = [
        window_key(term),
        term.id,
        term.profile or "?",
        labels.get(term.id, default_label)[:40],
        status_cell(status_row(term)).plain,
        elapsed,
    ]
    if with_new_columns:
        values += [
            term.condition or "-",
            f"delegating ({term.children_count})" if term.delegating else "-",
            "*" if term.fusion_changed else "",
            term.lifecycle,
            term.resolved_model or "-",
        ]
    return values


def row_styles(term: TerminalState, labelled: bool, *, working: bool = False) -> List[str]:
    """The per-cell styles of one row, in the order of :func:`row_values`.

    The colour language of ``fleet-tui.py:410-415``: a supervisor row is
    magenta with a bold id, a worker row has a dim window index and a cyan id,
    and an unlabeled TASK is dim. STATUS carries its own style from
    :func:`status_cell`, so its slot here is empty.

    ``working`` gives the ELAPSED cell the status colour, so a working seat's
    elapsed time is green beside its green ``● working``; every other state is
    dim. That one difference is what makes the column readable at a glance,
    and it is why no second column was needed to carry it.
    """
    is_supervisor = not term.parent_id
    return [
        STYLE_SUPERVISOR if is_supervisor else STYLE_DIM,  # WIN
        STYLE_SUPERVISOR_ID if is_supervisor else STYLE_WORKER_ID,  # ID
        STYLE_SUPERVISOR if is_supervisor else "",  # PROFILE
        "" if labelled else STYLE_DIM,  # TASK
        "",  # STATUS — status_cell owns it
        STYLE_WORKING if working else STYLE_DIM,  # ELAPSED
    ]


class FleetUpdated(Message):
    """One snapshot from the fetcher worker."""

    def __init__(self, state: FleetState) -> None:
        self.state = state
        super().__init__()


class FleetApp(App[None]):
    """The fleet table.

    Every collaborator that touches the outside world — the HTTP fetch, the
    sleep, the clock, tmux, the events-sync child process and the three scratch
    paths — is a constructor argument, so the pilot tests in
    ``test/tui/test_fleet_app.py`` drive the real widget code with nothing live
    behind it.
    """

    # Section order and spacing are the retiring script's, line for line
    # (``fleet-tui.py:336-450``): header, blank, table, blank, recent, blank,
    # status+hints, blank, peek — the peek LAST, filling what is left. Every
    # blank line is a ``margin-top: 1`` on the section that follows it, so a
    # hidden section takes its separator with it and no stray blank is left
    # behind. The one exception is ``#flash``: it is a *reserved* line that
    # stays blank until a notice needs it, so it is the separator above the
    # hints and a transient notice never shifts a section by a row.
    # No widget draws its own border: the two rules under the table
    # header and the peek banner are the only horizontal lines, exactly as the
    # script draws them.
    CSS = """
    Screen { layout: vertical; }
    #badge { height: 1; }
    #alert { height: 1; color: red; text-style: bold; }
    #table-head { height: 1; margin-top: 1; }
    #table-rule { height: 1; }
    #fleet { height: auto; max-height: 1fr; background: transparent; }
    #events-title { height: 1; margin-top: 1; }
    #events { height: auto; }
    #debug { height: auto; max-height: 12; margin-top: 1; overflow-y: hidden; }
    #flash { height: 1; color: yellow; }
    #hints { height: 1; }
    #peek { height: 1fr; margin-top: 1; }
    DataTable { background: transparent; }
    /* An id selector: DataTable's own DEFAULT_CSS styles this component class
       from inside a nested `DataTable { ... }` block, which outranks a plain
       type selector and would repaint the bar in the theme's accent. */
    #fleet > .datatable--cursor {
        background: #0000cd;
        color: #ffffff;
        text-style: bold;
    }
    """

    # Verbatim from scripts/fleet-tui.py:598-619, plus `c` for the new columns.
    # `priority=True` throughout: this key set is the app's contract, so no
    # focused widget's own binding (a DataTable cursor key, a container's
    # scroll key) may shadow or double-handle one of them.
    BINDINGS = [
        Binding("q", "quit", "quit", priority=True),
        Binding("up", "cursor_up", "up", show=False, priority=True),
        Binding("k", "cursor_up", "up", priority=True),
        Binding("down", "cursor_down", "down", show=False, priority=True),
        Binding("j", "cursor_down", "down", priority=True),
        Binding("g", "cursor_top", "top", priority=True),
        Binding("G", "cursor_bottom", "bottom", priority=True),
        Binding("enter", "jump", "jump", show=False, priority=True),
        Binding("o", "jump", "jump", priority=True),
        Binding("p", "toggle_peek", "peek", priority=True),
        Binding("plus", "peek_bigger", "peek+", show=False, priority=True),
        Binding("equals_sign", "peek_bigger", "peek+", priority=True),
        Binding("minus", "peek_smaller", "peek-", priority=True),
        Binding("d", "toggle_debug", "debug", priority=True),
        Binding("s", "snapshot", "snapshot", priority=True),
        Binding("c", "toggle_columns", "columns", priority=True),
    ]

    def __init__(
        self,
        session: str,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        runner: Runner = run_tmux,
        fetch: Callable[..., Any] = fetch_json,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        now: Callable[[], float] = time.time,
        labels_path: Path = LABELS_PATH,
        events_path: Path = EVENTS_PATH,
        snapshot_dir: Path = SNAPSHOT_DIR,
        poll_interval: float = FETCH_INTERVAL,
        sync_script: Path | None = None,
        spawn: Callable[[Path], Any] = spawn_detached,
    ) -> None:
        super().__init__()
        self.session = session
        self.endpoint = endpoint.rstrip("/")
        self._runner = runner
        self._fetch = fetch
        self._sleep = sleep
        self._now = now
        self._labels_path = labels_path
        self._events_path = events_path
        self._snapshot_dir = snapshot_dir
        self._poll_interval = poll_interval
        self._sync_script = sync_script
        self._spawn = spawn

        self.state: FleetState = FleetState.empty()
        self.show_new_columns = False
        self.peek_visible = True
        self.peek_lines = PEEK_LINES_DEFAULT
        self.debug_visible = False
        self.flash = ""
        #: Every tmux argv this app has run, in order — the AC4 audit trail.
        self.tmux_calls: List[List[str]] = []
        #: ``(HH:MM:SS, message)`` ring (``fleet-tui.py:82-84``).
        self.errors: List[Tuple[str, str]] = []
        self._last_raw: Any = None
        self._last_fetch_ms: int | None = None
        self._events_shown: List[str] = []
        #: Last ``window_activity`` read of the frame, reused by the peek banner
        #: so one refresh never issues two ``list-windows`` calls.
        self._ages: Dict[str, float] = {}
        self._events_sync_last = 0.0
        self._events_sync_proc: Any = None
        #: Rendered column widths of the current frame, gutter first — the
        #: single source the table and its ``#table-head`` line both size from.
        self._widths: List[int] = []
        #: Time-in-current-status per terminal. The only thing this app
        #: remembers between fetches; see :class:`StatusClock` for why that is
        #: safe and for the server field that would retire it.
        self._clock = StatusClock()

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"{self.endpoint}/sessions/{self.session}/fleet"

    def stamp(self) -> str:
        """``HH:MM:SS`` off the injected clock — never ``time.time`` directly."""
        return time.strftime("%H:%M:%S", time.localtime(self._now()))

    def record_error(self, message: str) -> None:
        """Append to the 20-entry ring (``fleet-tui.py:82-84``)."""
        self.errors.append((self.stamp(), str(message)[:300]))
        del self.errors[:-ERROR_RING_SIZE]

    def tmux(self, args: Sequence[str]) -> str | None:
        """Run one tmux command through the injected runner, recording the argv."""
        argv = list(args)
        verb = argv[0] if argv else ""
        if verb not in TMUX_READS and verb not in TMUX_WRITES:
            raise AssertionError(f"tmux verb not permitted by this app: {verb!r}")
        self.tmux_calls.append(argv)
        out = self._runner(argv)
        if out is None:
            self.record_error(f"tmux {verb}: failed")
        return out

    def _timed_fetch(self, url: str, timeout: float = 5.0) -> Any:
        """Wrap the injected fetch so the debug pane has latency and raw JSON.

        The wrapper is the app's, not the fetcher's: J1's loop stays a pure
        fetch/post/sleep with no reporting duties.
        """
        started = self._now()
        raw = self._fetch(url, timeout)
        self._last_fetch_ms = int((self._now() - started) * 1000)
        self._last_raw = raw
        return raw

    @work(exit_on_error=False)
    async def fetch_worker(self) -> None:
        """The one long-lived worker (D2): fetch, post, sleep, forever."""
        kwargs: Dict[str, Any] = {
            "fetch": self._timed_fetch,
            "now": self._now,
            "poll_interval": self._poll_interval,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        await run_fetch_loop(
            self.url,
            lambda state: self.post_message(FleetUpdated(state)),
            **kwargs,
        )

    # ── events-feed sync (F481) ──────────────────────────────────────────────

    def events_sync_script(self) -> Path | None:
        """The configured script path, or the resolved one (D6 gap 5)."""
        if self._sync_script is not None:
            return self._sync_script if self._sync_script.is_file() else None
        return find_events_sync_script(self._events_path)

    def maybe_sync_events(self) -> bool:
        """Fire ``fleet-events-sync.sh`` non-blocking, throttled to ≥30 s.

        Verbatim posture from ``fleet-tui.py:203-226``: never wait on the child,
        never let one failure stop the frame, and skip entirely while a previous
        run is still going. Returns True only when this call started a run.
        """
        proc = self._events_sync_proc
        if proc is not None:
            if proc.poll() is None:
                return False
            self._events_sync_proc = None
        wall = self._now()
        if (wall - self._events_sync_last) < EVENTS_SYNC_INTERVAL:
            return False
        script = self.events_sync_script()
        if script is None:
            # Deliberately does *not* stamp the throttle: a script that appears
            # later must be picked up on the next frame, as the script does.
            return False
        self._events_sync_last = wall
        try:
            self._events_sync_proc = self._spawn(script)
        except Exception as exc:
            self._events_sync_proc = None
            self.record_error(f"events-sync: {exc}")
            return False
        return True

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # Top to bottom, the script's order (``fleet-tui.py:336-450``). The peek
        # is LAST: it takes every row the sections above did not use.
        with Vertical():
            yield Static(id="badge")
            yield Static(id="alert")
            yield Static(id="table-head")
            yield Static(id="table-rule")
            yield DataTable(id="fleet")
            yield Static(id="events-title")
            yield Static(id="events")
            yield Static(id="debug")
            yield Static(id="flash")
            yield Static(id="hints")
            yield Static(id="peek")

    def on_mount(self) -> None:
        table = self.table
        table.cursor_type = "row"
        # The script draws no stripes and no cell padding: columns are sized by
        # :func:`column_widths`, which already carries the two-space gutter
        # (``fleet-tui.py:361-370``). Letting ``DataTable`` add its own padding
        # on top would shift every column two places right of the header.
        table.zebra_stripes = False
        table.cell_padding = 0
        table.show_header = False  # the header is drawn as ``#table-head``
        table.can_focus = False  # every key is an app binding (AC4: one jump path)
        self._install_columns()
        self.query_one("#peek", Static).display = self.peek_visible
        self.query_one("#debug", Static).display = self.debug_visible
        self.query_one("#hints", Static).update(hint_renderable())
        self.refresh_view()
        self.fetch_worker()
        # The clock ticks on its own: the fetch loop is too slow to watch a
        # number count up, and too expensive to run at this rate.
        self.set_interval(ELAPSED_TICK_SECONDS, self.refresh_elapsed)

    def frame_width(self) -> int:
        """Full-width for the two rules and the selection bar.

        The script asks ``shutil.get_terminal_size()`` on every frame
        (``:331``); inside Textual the screen already knows, and the shell call
        is only the fallback for a frame drawn before the first layout.
        """
        width = self.size.width
        if width <= 0:  # pragma: no cover - only before the first layout pass
            width = shutil.get_terminal_size().columns
        return max(FALLBACK_WIDTH // 4, width)

    @property
    def table(self) -> DataTable[Any]:
        return self.query_one("#fleet", DataTable)

    @property
    def columns(self) -> Sequence[str]:
        """The named (non-gutter) column names, in order (AC5)."""
        return ALL_COLUMNS if self.show_new_columns else PARITY_COLUMNS

    @property
    def view_columns(self) -> Sequence[str]:
        """What the table installs: the selection gutter, then :attr:`columns`."""
        return ALL_VIEW if self.show_new_columns else PARITY_VIEW

    def column_index(self, name: str) -> int:
        """Table index of a named column, gutter accounted for."""
        return list(self.view_columns).index(name)

    def _install_columns(self, widths: Sequence[int] | None = None) -> None:
        """(Re)install the columns at fixed widths, gutter first.

        Widths are explicit rather than auto so the table lines up with the
        ``#table-head`` line drawn above it: both come from
        :func:`column_widths`, and the gutter is the script's two columns
        (``fleet-tui.py:374,399``) rather than one glyph plus cell padding.
        """
        table = self.table
        table.clear(columns=True)
        sizes = list(widths) if widths is not None else []
        for index, name in enumerate(self.view_columns):
            if index < len(sizes):
                width = sizes[index]
            else:
                width = GUTTER_WIDTH if name == "" else max(len(name) + COLUMN_GUTTER, 1)
            table.add_column(name, width=width)
        self._widths = [int(column.width) for column in table.columns.values()]

    # ── rendering ────────────────────────────────────────────────────────────

    def on_fleet_updated(self, message: FleetUpdated) -> None:
        if message.state.last_error is not None:
            self.record_error(f"fetch: {message.state.last_error}")
        self.state = message.state
        self.refresh_view()

    def refresh_view(self) -> None:
        """Redraw every widget from the current snapshot. Never raises.

        The script auto-snapshots and *exits* on a render crash
        (``fleet-tui.py:586-591``). Here the snapshot is still written and the
        traceback still lands in the error ring, but the app stays up: a bad
        frame is not a reason to lose the fleet view.
        """
        try:
            self._render_all()
        except Exception:
            self.record_error(traceback.format_exc(limit=3))
            try:
                path: Path | None = self.write_snapshot(reason="render-crash")
            except Exception:
                path = None
            self.flash = (
                f"render error; snapshot: {path}" if path else "render error (snapshot failed)"
            )
            try:
                self.query_one("#flash", Static).update(self.flash)
            except Exception:
                pass

    def _render_all(self) -> None:
        self.refresh_table()
        self.refresh_badge()
        self.refresh_events()
        self.refresh_peek()
        self.refresh_debug()
        self.query_one("#flash", Static).update(self.flash)

    def window_ages(self) -> Dict[str, float]:
        """``{window_index: seconds since last output}`` (``fleet-tui.py:115-118``)."""
        self._ages = read_window_ages(self.tmux, self.session, now=self._now)
        return self._ages

    def visible_terminals(self) -> List[TerminalState]:
        return sort_terminals(self.state.terminals)

    # ── the ELAPSED column ───────────────────────────────────────────────────

    def elapsed_cells(self) -> Dict[str, str]:
        """``{terminal_id: "● 12m"}`` for every row the clock is timing."""
        now = self._now()
        cells: Dict[str, str] = {}
        for term in self.state.terminals:
            spell = self._clock.spell(term.id)
            if spell is None:
                continue
            cells[term.id] = elapsed_cell(
                max(0.0, now - spell.since), spell.exact, is_working(term)
            )
        return cells

    def elapsed_text(self, term: TerminalState, now: float) -> Text:
        """One ELAPSED cell, styled: green while working, dim otherwise."""
        spell = self._clock.spell(term.id)
        working = is_working(term)
        if spell is None:
            return Text(ELAPSED_UNKNOWN, style=STYLE_DIM)
        plain = elapsed_cell(max(0.0, now - spell.since), spell.exact, working)
        return Text(plain, style=STYLE_WORKING if working else STYLE_DIM)

    def refresh_elapsed(self) -> None:
        """Re-render just the ELAPSED cells. Driven by a 1 Hz timer.

        The fetch loop runs every couple of seconds and does real work on each
        pass — reading the labels file and shelling out to tmux — so it is the
        wrong thing to run once a second just to advance a clock. This touches
        the one column that changes on its own, writes a cell only when its text
        actually differs, and never resizes a column: the ELAPSED column is the
        one stretched to the screen edge, so a value growing from ``59s`` to
        ``1m`` always has room.
        """
        table = self.table
        if not table.row_count:
            return
        column = self.column_index(ELAPSED_COLUMN)
        now = self._now()
        for index, term in enumerate(self.visible_terminals()):
            if index >= table.row_count:
                break
            wanted = self.elapsed_text(term, now)
            coordinate = Coordinate(index, column)
            current = table.get_cell_at(coordinate)
            if isinstance(current, Text) and current.plain == wanted.plain:
                continue
            table.update_cell_at(coordinate, wanted, update_width=False)

    def row_cells(
        self,
        term: TerminalState,
        labels: Mapping[str, str],
        values: Sequence[str] | None = None,
    ) -> List[Any]:
        """One row, in the order of :attr:`view_columns`.

        Every cell is a :class:`rich.text.Text` (D4/B12) so ``get_cell_at``
        hands a test back both the plain value and the style — which is how the
        restored colour language is asserted.
        """
        if values is None:
            values = row_values(
                term,
                labels,
                with_new_columns=self.show_new_columns,
                elapsed_cells=self.elapsed_cells(),
            )
        styles = row_styles(term, term.id in labels, working=is_working(term))
        cells: List[Any] = [Text(MARKER_BLANK, style=STYLE_MARKER)]
        for index, value in enumerate(values):
            if index == PARITY_COLUMNS.index("STATUS"):
                cells.append(status_cell(status_row(term)))
            else:
                cells.append(Text(value, style=styles[index] if index < len(styles) else ""))
        return cells

    def frame_widths(self, values: Sequence[Sequence[str]]) -> List[int]:
        """Gutter width, then one width per visible column, stretched to fill.

        The named columns follow the script's ``max(header, cell) + 2`` rule.
        Whatever is left of the screen is handed to the last column, so the
        selection bar and the rule under the header end at the same place the
        script's full-width ones do (``fleet-tui.py:373,401``).
        """
        widths = [GUTTER_WIDTH] + column_widths(self.columns, values)
        spare = self.frame_width() - sum(widths)
        if spare > 0:
            widths[-1] += spare
        return widths

    def refresh_table_head(self) -> None:
        """The header row and the thin rule under it (``fleet-tui.py:372-373``)."""
        widths = self._widths[1:] if self._widths else []
        line = header_line(self.columns, widths) if widths else ""
        self.query_one("#table-head", Static).update(Text(line, style=STYLE_TABLE_HEADER))
        rule = RULE_GLYPH * self.frame_width()
        self.query_one("#table-rule", Static).update(Text(rule, style=STYLE_RULE))

    def refresh_table(self) -> None:
        table = self.table
        selected = self.selected_id()
        labels = read_labels(self._labels_path)
        self.window_ages()
        rows = self.visible_terminals()
        # Fold this snapshot into the clock before anything reads it, so a
        # status that changed on this fetch is already timed from now.
        self._clock.observe(rows, self._now())
        elapsed = self.elapsed_cells()
        values = [
            row_values(
                term,
                labels,
                with_new_columns=self.show_new_columns,
                elapsed_cells=elapsed,
            )
            for term in rows
        ]
        self._install_columns(self.frame_widths(values))
        self.refresh_table_head()
        for term, row in zip(rows, values):
            table.add_row(*self.row_cells(term, labels, row), key=term.id)
        if not rows:
            return
        ids = [t.id for t in rows]
        index = ids.index(selected) if selected in ids else 0
        table.move_cursor(row=index)
        self.refresh_marker()

    def refresh_marker(self) -> None:
        """Put ``▶`` on the cursor row and nothing on the others (``:399``)."""
        table = self.table
        if not table.row_count:
            return
        cursor = table.cursor_row
        for index in range(table.row_count):
            wanted = MARKER_SELECTED if index == cursor else MARKER_BLANK
            coordinate = Coordinate(index, MARKER_INDEX)
            current = table.get_cell_at(coordinate)
            plain = current.plain if isinstance(current, Text) else str(current)
            if plain != wanted:
                table.update_cell_at(
                    coordinate, Text(wanted, style=STYLE_MARKER), update_width=False
                )

    def on_data_table_row_highlighted(self, message: Any) -> None:
        """Keep the gutter in step with every way the cursor can move."""
        self.refresh_marker()

    def badge_text(self) -> str:
        """Header: session, clock, worker count, fetch latency, live/stale.

        Parity with ``fleet-tui.py:338-339`` plus the Textual app's own
        live/stale badge (AC1), which the script had no concept of.
        """
        workers = sum(1 for t in self.state.terminals if t.parent_id)
        latency = "-" if self._last_fetch_ms is None else f"{self._last_fetch_ms}ms"
        if self.state.fetched_at is None:
            badge = "not yet fetched"
        elif self.state.stale_for > self._poll_interval:
            badge = f"stale {int(self.state.stale_for)}s"
        else:
            badge = "live"
        return (
            f"{SECTION_MARK} CAO fleet · {self.session}  {self.stamp()} · {workers} workers"
            f" · fetch {latency} · {badge}"
        )

    def badge_renderable(self) -> Text:
        """:meth:`badge_text` split the way the script paints it (``:337-339``).

        ``▌ CAO fleet · <session>`` in the title accent, then a dim tail — the
        clock, the worker count and the fetch latency. The live/stale badge is
        the Textual app's own addition (AC1) and rides in that same dim tail,
        turning yellow only once the snapshot has actually gone stale.
        """
        text = self.badge_text()
        head = f"{SECTION_MARK} CAO fleet · {self.session}"
        line = Text(no_wrap=True)
        line.append(head, style=STYLE_SECTION)
        tail = text[len(head) :]
        stale = self.state.fetched_at is not None and self.state.stale_for > self._poll_interval
        line.append(tail, style=STYLE_STALE if stale else STYLE_SECTION_TAIL)
        return line

    def alert_text(self) -> str:
        """The loud server-down line, or ``""`` when the endpoint is answering.

        ``fleet-tui.py:334-335`` draws a red ``fleet endpoint unreachable``
        header whenever the fetch failed; the last-good timestamp is added here
        so a stale screen says how stale it is without opening the debug pane.
        """
        error = self.state.last_error
        if not error:
            return ""
        if self.state.fetched_at is None:
            last_good = "never"
        else:
            last_good = time.strftime("%H:%M:%S", time.localtime(self.state.fetched_at))
        return f"fleet endpoint unreachable: {error} · last good {last_good}"

    def refresh_badge(self) -> None:
        self.query_one("#badge", Static).update(self.badge_renderable())
        alert = self.query_one("#alert", Static)
        text = self.alert_text()
        alert.display = bool(text)
        alert.update(Text(text, style=STYLE_ALERT))

    def refresh_events(self) -> None:
        """The ``▌ recent`` section: dim lines, indented two spaces (``:419-420``).

        The whole section — title included — disappears when the feed is empty,
        as the script's ``if ev:`` guard does, so an absent events file leaves
        no empty box behind.
        """
        self.maybe_sync_events()
        title = self.query_one("#events-title", Static)
        body = self.query_one("#events", Static)
        lines = read_events(self._events_path)
        title.display = bool(lines)
        body.display = bool(lines)
        if lines == self._events_shown:
            return
        self._events_shown = lines
        title.update(Text(f"{SECTION_MARK} recent", style=STYLE_SECTION))
        body.update(Text("\n".join(f"{EVENT_INDENT}{line}" for line in lines), style=STYLE_EVENT))

    def selected_id(self) -> str | None:
        """The terminal id under the row cursor, or ``None`` on an empty table."""
        table = self.table
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return None if key.value is None else str(key.value)

    def selected_terminal(self) -> TerminalState | None:
        selected = self.selected_id()
        if selected is None:
            return None
        return next((t for t in self.state.terminals if t.id == selected), None)

    def capture_window(self, window_index: int, peek: Static | None = None) -> List[str]:
        """Tail of the selected window's CAO pane (``fleet-tui.py:128-148``).

        F544: a window target resolves to the *active* pane, so the first pane
        id is resolved first and captured by id.
        """
        target = f"{self.session}:{window_index}"
        panes = self.tmux(["list-panes", "-t", target, "-F", "#{pane_id}"])
        if panes:
            first = panes.splitlines()[0].strip()
            if first:
                target = first
        out = self.tmux(["capture-pane", "-p", "-e", "-S", "-200", "-t", target])
        if out is None:
            return ["(capture failed)"]
        lines = [line.rstrip() for line in out.splitlines()]
        while lines and not lines[-1].strip():
            lines.pop()
        wanted = self.peek_lines if peek is None else self.peek_capacity(peek)
        return lines[-wanted:] if lines else ["(blank)"]

    def peek_title(self, term: TerminalState) -> str:
        """The peek banner (``fleet-tui.py:441-443``): who, where, how idle.

        Two different clocks, both named for what they measure. ``for`` is time
        in the current status, the same number the row's ELAPSED cell shows.
        ``quiet`` is time since the pane last printed anything, from the frame's
        one cached ``list-windows`` call — the old IDLE number, which is a real
        signal in a detail view (a working seat that has gone quiet for minutes
        is worth a look) and only misleading when it wears the word "idle" in a
        column of every seat.
        """
        status = status_cell(status_row(term)).plain.removeprefix("· ")
        spell = self._clock.spell(term.id)
        if spell is None:
            held = ELAPSED_UNKNOWN
        else:
            held = elapsed_cell(max(0.0, self._now() - spell.since), spell.exact, False)
        return (
            f"▌ peek · {term.profile}-{term.id} · win {window_key(term)}"
            f" · {status} · for {held} · quiet {fmt_age(self._ages.get(window_key(term)))}"
        )

    def peek_capacity(self, peek: Static) -> int:
        """How many capture lines fit: the leftover space, or ``peek_lines``.

        The script gives the peek every row the rest of the frame did not use
        (``fleet-tui.py:448-449``); ``+``/``-`` then sets a floor on top of that
        rather than being the only thing that decides the height.
        """
        available = peek.size.height - PEEK_CHROME_LINES  # banner + double rule
        return max(self.peek_lines, available)

    def peek_banner(self, term: TerminalState | None) -> Text:
        """The two chrome lines of the peek section (``fleet-tui.py:441-445``).

        Title in the section accent, then a full-width double rule in the same
        accent — the heavier rule is what marks the peek as the last section
        rather than another row of the table above it.
        """
        title = self.peek_title(term) if term is not None else f"{SECTION_MARK} peek"
        banner = Text(title, style=STYLE_SECTION, no_wrap=True)
        banner.append("\n")
        banner.append(PEEK_RULE_GLYPH * self.frame_width(), style=STYLE_SECTION)
        return banner

    def refresh_peek(self) -> None:
        peek = self.query_one("#peek", Static)
        peek.display = self.peek_visible
        peek.styles.min_height = self.peek_lines + PEEK_CHROME_LINES
        if not self.peek_visible:
            return
        term = self.selected_terminal()
        if term is None or term.window_index is None:
            peek.update(Text("\n").join([self.peek_banner(term), Text("(nothing selected)")]))
            return
        body = Text.from_ansi("\n".join(self.capture_window(term.window_index, peek)))
        peek.update(Text("\n").join([self.peek_banner(term), body]))

    def error_ring_lines(self, limit: int = ERROR_RING_SHOWN) -> List[str]:
        """The tail of the error ring, or a single "(empty)" line."""
        if not self.errors:
            return ["(no tui errors)"]
        return [f"{stamp} {message}" for stamp, message in self.errors[-limit:]]

    def debug_text(self) -> str:
        latency = "-" if self._last_fetch_ms is None else f"{self._last_fetch_ms}ms"
        raw = json.dumps(self._last_raw, indent=2, default=str) if self._last_raw else "(none)"
        ring = "\n".join(self.error_ring_lines())
        return (
            f"endpoint={self.url} fetch={latency} "
            f"stale_for={self.state.stale_for:.1f}s last_error={self.state.last_error}\n"
            f"{ring}\n{raw}"
        )

    def refresh_debug(self) -> None:
        debug = self.query_one("#debug", Static)
        debug.display = self.debug_visible
        if self.debug_visible:
            debug.update(self.debug_text())

    # ── actions (one per binding) ────────────────────────────────────────────

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()
        self.refresh_marker()
        self.refresh_peek()

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()
        self.refresh_marker()
        self.refresh_peek()

    def action_cursor_top(self) -> None:
        self.table.move_cursor(row=0)
        self.refresh_marker()
        self.refresh_peek()

    def action_cursor_bottom(self) -> None:
        if self.table.row_count:
            self.table.move_cursor(row=self.table.row_count - 1)
        self.refresh_marker()
        self.refresh_peek()

    def action_jump(self) -> None:
        """The app's only mutation: one ``tmux select-window`` (AC4)."""
        term = self.selected_terminal()
        if term is None or term.window_index is None:
            self.set_flash("nothing selected")
            return
        target = f"{self.session}:{term.window_index}"
        if self.tmux(["select-window", "-t", target]) is None:
            self.set_flash(f"jump failed ({target})")
        else:
            self.set_flash(f"jumped to {term.profile}-{term.id} — prefix+l comes back")

    def action_toggle_peek(self) -> None:
        self.peek_visible = not self.peek_visible
        self.refresh_peek()

    def action_peek_bigger(self) -> None:
        self.peek_lines = min(PEEK_LINES_MAX, self.peek_lines + PEEK_STEP)
        self.refresh_peek()

    def action_peek_smaller(self) -> None:
        self.peek_lines = max(PEEK_LINES_MIN, self.peek_lines - PEEK_STEP)
        self.refresh_peek()

    def action_toggle_debug(self) -> None:
        self.debug_visible = not self.debug_visible
        self.refresh_debug()

    def action_toggle_columns(self) -> None:
        """Reveal or hide the five new columns; parity is the default (AC5)."""
        self.show_new_columns = not self.show_new_columns
        self._install_columns()
        self.refresh_table()

    def action_snapshot(self) -> None:
        self.set_flash(f"snapshot written: {self.write_snapshot()}")

    def set_flash(self, message: str) -> None:
        self.flash = message
        self.query_one("#flash", Static).update(message)

    def write_snapshot(self, reason: str = "manual") -> Path:
        """Write a debugging snapshot (``fleet-tui.py:273-296``). Not a tmux call.

        Carries every section the script's snapshot has, in its order: the
        header, the error ring, the labels file, the events tail and the raw
        fleet JSON. A snapshot missing the raw payload is the one that cannot
        answer "what did the server actually say".
        """
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._snapshot_dir / f"fleet-{stamp}.txt"
        try:
            labels_blob = self._labels_path.read_text()
        except OSError as exc:
            labels_blob = f"(unreadable: {exc})"
        raw = json.dumps(self._last_raw, indent=2, default=str) if self._last_raw else "(none)"
        lines = [
            f"cao-fleet snapshot · session={self.session} · reason={reason} · {stamp}",
            self.badge_text(),
            self.alert_text() or "(endpoint reachable)",
            f"endpoint={self.url} fetch={self._last_fetch_ms} last_error={self.state.last_error}",
            f"sel={self.selected_id()} peek={self.peek_visible}/{self.peek_lines}",
            "",
            "== tui error ring ==",
            *self.error_ring_lines(ERROR_RING_SIZE),
            "",
            "== labels file ==",
            labels_blob,
            "== events tail ==",
            *read_events(self._events_path, 20),
            "",
            "== raw fleet json ==",
            raw,
        ]
        path.write_text("\n".join(lines) + "\n")
        return path


# ── one-shot rendering (`--once`) ────────────────────────────────────────────


def format_frame(
    session: str,
    state: FleetState,
    labels: Mapping[str, str],
    events: Sequence[str],
    *,
    latency_ms: int | None = None,
    clock: str = "",
    selected: str | None = None,
) -> str:
    """One plain-text frame — what ``--once`` prints (``fleet-tui.py:571-574``).

    Column widths follow the script's rule: the wider of the header and the
    widest cell, plus a two-space gutter (``:362-370``). No colour: the output
    is meant for a pipe, a snapshot or a bug report.

    ELAPSED reads ``-`` throughout: one fetch has no earlier fetch to time a
    transition against, and no server field carries the transition stamp (see
    :class:`StatusClock`). Printing a window-activity age there instead would
    put a number under the new column's name that does not mean what the column
    says.
    """
    rows = sort_terminals(state.terminals)
    workers = sum(1 for term in rows if term.parent_id)
    latency = "-" if latency_ms is None else f"{latency_ms}ms"
    out: List[str] = []
    if state.last_error:
        out.append(f"fleet endpoint unreachable: {state.last_error}")
    out.append(
        f"{SECTION_MARK} CAO fleet · {session}  {clock} · {workers} workers · fetch {latency}"
    )
    out.append("")
    values = [row_values(term, labels) for term in rows]
    widths = column_widths(PARITY_COLUMNS, values)
    header = header_line(PARITY_COLUMNS, widths)
    out.append(header)
    out.append(RULE_GLYPH * len(header.rstrip()))
    for term, row in zip(rows, values):
        marker = f"{MARKER_SELECTED} " if term.id == selected else "  "
        out.append((marker + "".join(cell.ljust(w) for cell, w in zip(row, widths))).rstrip())
    if not rows:
        out.append("  (no workers)")
    if events:
        out += ["", f"{SECTION_MARK} recent"] + [f"{EVENT_INDENT}{line}" for line in events]
    out += ["", hint_text()]
    return "\n".join(out) + "\n"


def render_once(
    session: str,
    endpoint: str,
    *,
    fetch: Callable[..., Any] = fetch_json,
    runner: Runner = run_tmux,
    labels_path: Path = LABELS_PATH,
    events_path: Path = EVENTS_PATH,
    now: Callable[[], float] = time.time,
) -> str:
    """Fetch once and return the frame; no Textual app, no terminal needed."""
    url = f"{endpoint.rstrip('/')}/sessions/{session}/fleet"
    started = now()
    latency: int | None = None
    try:
        raw = fetch(url, 5.0)
    except Exception as exc:
        state = FleetState.empty().with_failure(str(exc), now=now())
    else:
        latency = int((now() - started) * 1000)
        state = FleetState.from_dict(raw, fetched_at=now())
    rows = sort_terminals(state.terminals)
    return format_frame(
        session,
        state,
        read_labels(labels_path),
        read_events(events_path, 5),
        latency_ms=latency,
        clock=time.strftime("%H:%M:%S", time.localtime(now())),
        selected=rows[0].id if rows else None,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cao-fleet", description="Textual fleet view for a CAO session."
    )
    parser.add_argument(
        "--session",
        default=os.environ.get("CAO_SESSION"),
        help="CAO/tmux session name (default: $CAO_SESSION, else the enclosing tmux session).",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("CAO_ENDPOINT") or DEFAULT_ENDPOINT,
        help=f"cao-server base URL (default: $CAO_ENDPOINT or {DEFAULT_ENDPOINT}).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=FETCH_INTERVAL,
        help=f"seconds between fleet fetches (default: {FETCH_INTERVAL}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print one plain-text frame and exit, instead of running the TUI.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry for ``cao-fleet`` (D1)."""
    args = parse_args(argv)
    session = args.session or detect_tmux_session()
    if not session:
        sys.stderr.write(
            "cao-fleet: no session — pass --session, set CAO_SESSION, or run inside tmux\n"
        )
        return 2
    if args.interval <= 0:
        sys.stderr.write("cao-fleet: --interval must be greater than zero\n")
        return 2
    if args.once:
        sys.stdout.write(render_once(session, args.endpoint))
        return 0
    FleetApp(session, args.endpoint, poll_interval=args.interval).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
