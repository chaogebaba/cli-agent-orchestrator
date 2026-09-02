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
the script does (``fleet-tui.py:115-118``).

**Side effects (AC4).** Every subprocess goes through one injectable runner.
Exactly one of them mutates anything: ``tmux select-window`` on jump
(``fleet-tui.py:484``). ``list-windows``, ``list-panes`` and ``capture-pane``
are reads.

**Failure posture (AC1).** A fetch failure is *staleness*, never an error row
(#441): the last good rows stay on screen and the header badge grows a
``stale Ns`` suffix. Before the first successful fetch the badge reads
``not yet fetched`` (``FleetState.fetched_at is None``).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Sequence

# The Textual dependency is the optional `fleet` extra (D1/B10): a server-only
# install carries no TUI library. Importing this module without it is a clean
# one-line exit, not a traceback -- the console script's only job is to say
# which extra is missing.
try:  # pragma: no cover - exercised by the install path, not the suite
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.message import Message
    from textual.widgets import DataTable, RichLog, Static
except ModuleNotFoundError as exc:  # pragma: no cover - same
    raise SystemExit(
        "cao-fleet needs the 'fleet' extra (Textual is not installed).\n"
        "    uv tool install --force 'cli-agent-orchestrator[fleet]'\n"
        f"(import failed: {exc})"
    ) from exc

from rich.text import Text

from cli_agent_orchestrator.tui.columns import ALL_COLUMNS, PARITY_COLUMNS
from cli_agent_orchestrator.tui.fetcher import FETCH_INTERVAL, fetch_json, run_fetch_loop
from cli_agent_orchestrator.tui.fleet_state import FleetState, TerminalState
from cli_agent_orchestrator.tui.status_cell import status_cell

__all__ = ["FleetApp", "FleetUpdated", "main"]

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

#: The tmux verbs this app is allowed to run. Only ``select-window`` mutates
#: anything; the rest are reads (AC4).
TMUX_READS = frozenset({"list-windows", "list-panes", "capture-pane"})
TMUX_WRITES = frozenset({"select-window"})

#: ``(session, args) -> stdout``; ``None`` means the call failed.
Runner = Callable[[Sequence[str]], "str | None"]


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


class FleetUpdated(Message):
    """One snapshot from the fetcher worker."""

    def __init__(self, state: FleetState) -> None:
        self.state = state
        super().__init__()


class FleetApp(App[None]):
    """The fleet table.

    Every collaborator that touches the outside world — the HTTP fetch, the
    sleep, the clock, tmux, and the three scratch paths — is a constructor
    argument, so the pilot tests in ``test/tui/test_fleet_app.py`` drive the
    real widget code with nothing live behind it.
    """

    CSS = """
    Screen { layout: vertical; }
    #badge { height: 1; }
    #flash { height: 1; color: $text-muted; }
    #fleet { height: 1fr; }
    #peek { height: 14; border-top: solid $panel; }
    #events { height: 6; border-top: solid $panel; }
    #debug { height: 10; border-top: solid $panel; }
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

        self.state: FleetState = FleetState.empty()
        self.show_new_columns = False
        self.peek_visible = True
        self.peek_lines = PEEK_LINES_DEFAULT
        self.debug_visible = False
        self.flash = ""
        #: Every tmux argv this app has run, in order — the AC4 audit trail.
        self.tmux_calls: List[List[str]] = []
        self._last_raw: Any = None
        self._last_fetch_ms: int | None = None
        self._events_shown: List[str] = []

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"{self.endpoint}/sessions/{self.session}/fleet"

    def tmux(self, args: Sequence[str]) -> str | None:
        """Run one tmux command through the injected runner, recording the argv."""
        argv = list(args)
        verb = argv[0] if argv else ""
        if verb not in TMUX_READS and verb not in TMUX_WRITES:
            raise AssertionError(f"tmux verb not permitted by this app: {verb!r}")
        self.tmux_calls.append(argv)
        return self._runner(argv)

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
        kwargs: Dict[str, Any] = {"fetch": self._timed_fetch, "now": self._now}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        await run_fetch_loop(
            self.url,
            lambda state: self.post_message(FleetUpdated(state)),
            **kwargs,
        )

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="badge")
            yield DataTable(id="fleet")
            yield Static(id="peek")
            yield RichLog(id="events", markup=False)
            yield Static(id="debug")
            yield Static(id="flash")

    def on_mount(self) -> None:
        table = self.table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.can_focus = False  # every key is an app binding (AC4: one jump path)
        self._install_columns()
        self.query_one("#peek", Static).display = self.peek_visible
        self.query_one("#debug", Static).display = self.debug_visible
        self.refresh_view()
        self.fetch_worker()

    @property
    def table(self) -> DataTable[Any]:
        return self.query_one("#fleet", DataTable)

    @property
    def columns(self) -> Sequence[str]:
        """The visible column names, in order (AC5)."""
        return ALL_COLUMNS if self.show_new_columns else PARITY_COLUMNS

    def _install_columns(self) -> None:
        table = self.table
        table.clear(columns=True)
        table.add_columns(*self.columns)

    # ── rendering ────────────────────────────────────────────────────────────

    def on_fleet_updated(self, message: FleetUpdated) -> None:
        self.state = message.state
        self.refresh_view()

    def refresh_view(self) -> None:
        """Redraw every widget from the current snapshot. Never raises."""
        self.refresh_table()
        self.refresh_badge()
        self.refresh_events()
        self.refresh_peek()
        self.refresh_debug()
        self.query_one("#flash", Static).update(self.flash)

    def window_ages(self) -> Dict[str, float]:
        """``{window_index: seconds since last output}`` (``fleet-tui.py:115-118``)."""
        out = self.tmux(
            ["list-windows", "-t", self.session, "-F", "#{window_index} #{window_activity}"]
        )
        ages: Dict[str, float] = {}
        if not out:
            return ages
        wall = self._now()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                ages[parts[0]] = max(0.0, wall - int(parts[1]))
        return ages

    def visible_terminals(self) -> List[TerminalState]:
        return sort_terminals(self.state.terminals)

    def row_cells(
        self, term: TerminalState, labels: Mapping[str, str], ages: Mapping[str, float]
    ) -> List[Any]:
        """One row, in the order of :attr:`columns`.

        STATUS is a :class:`rich.text.Text` (D4/B12) so ``get_cell_at`` hands a
        test back the glyph and the style; every other cell is a plain string.
        """
        widx = str(term.window_index) if term.window_index is not None else "?"
        default_label = "(supervisor seat)" if not term.parent_id else "(unlabeled)"
        task = labels.get(term.id, default_label)[:40]
        cells: List[Any] = [
            widx,
            term.id,
            term.profile or "?",
            task,
            status_cell(status_row(term)),
            fmt_age(ages.get(widx)),
        ]
        if self.show_new_columns:
            cells += [
                term.condition or "-",
                f"delegating ({term.children_count})" if term.delegating else "-",
                "*" if term.fusion_changed else "",
                term.lifecycle,
                term.resolved_model or "-",
            ]
        return cells

    def refresh_table(self) -> None:
        table = self.table
        selected = self.selected_id()
        labels = read_labels(self._labels_path)
        ages = self.window_ages()
        table.clear()
        rows = self.visible_terminals()
        for term in rows:
            table.add_row(*self.row_cells(term, labels, ages), key=term.id)
        if not rows:
            return
        ids = [t.id for t in rows]
        index = ids.index(selected) if selected in ids else 0
        table.move_cursor(row=index)

    def badge_text(self) -> str:
        """Header line: session, worker count, and the staleness badge (AC1)."""
        workers = sum(1 for t in self.state.terminals if t.parent_id)
        if self.state.fetched_at is None:
            badge = "not yet fetched"
        elif self.state.stale_for > FETCH_INTERVAL:
            badge = f"stale {int(self.state.stale_for)}s"
        else:
            badge = "live"
        return f"CAO fleet · {self.session} · {workers} workers · {badge}"

    def refresh_badge(self) -> None:
        self.query_one("#badge", Static).update(self.badge_text())

    def refresh_events(self) -> None:
        log = self.query_one("#events", RichLog)
        lines = read_events(self._events_path)
        if lines == self._events_shown:
            return
        log.clear()
        for line in lines:
            log.write(line)
        self._events_shown = lines

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

    def capture_window(self, window_index: int) -> List[str]:
        """Tail of the selected window's CAO pane (``fleet-tui.py:128-148``).

        F544: a window target resolves to the *active* pane, so the first pane
        id is resolved first and captured by id.
        """
        panes = self.tmux(
            ["list-panes", "-t", f"{self.session}:{window_index}", "-F", "#{pane_id}"]
        )
        target = f"{self.session}:{window_index}"
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
        return lines[-self.peek_lines :] if lines else ["(blank)"]

    def refresh_peek(self) -> None:
        peek = self.query_one("#peek", Static)
        peek.display = self.peek_visible
        peek.styles.height = self.peek_lines
        if not self.peek_visible:
            return
        term = self.selected_terminal()
        if term is None or term.window_index is None:
            peek.update("(nothing selected)")
            return
        peek.update(Text.from_ansi("\n".join(self.capture_window(term.window_index))))

    def debug_text(self) -> str:
        latency = "-" if self._last_fetch_ms is None else f"{self._last_fetch_ms}ms"
        raw = json.dumps(self._last_raw, indent=2, default=str) if self._last_raw else "(none)"
        return (
            f"endpoint={self.url} fetch={latency} "
            f"stale_for={self.state.stale_for:.1f}s last_error={self.state.last_error}\n{raw}"
        )

    def refresh_debug(self) -> None:
        debug = self.query_one("#debug", Static)
        debug.display = self.debug_visible
        if self.debug_visible:
            debug.update(self.debug_text())

    # ── actions (one per binding) ────────────────────────────────────────────

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()
        self.refresh_peek()

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()
        self.refresh_peek()

    def action_cursor_top(self) -> None:
        self.table.move_cursor(row=0)
        self.refresh_peek()

    def action_cursor_bottom(self) -> None:
        if self.table.row_count:
            self.table.move_cursor(row=self.table.row_count - 1)
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
        """Write a debugging snapshot (``fleet-tui.py:273-296``). Not a tmux call."""
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._snapshot_dir / f"fleet-{stamp}.txt"
        try:
            labels_blob = self._labels_path.read_text()
        except OSError as exc:
            labels_blob = f"(unreadable: {exc})"
        lines = [
            f"cao-fleet snapshot · session={self.session} · reason={reason} · {stamp}",
            self.badge_text(),
            self.debug_text(),
            f"sel={self.selected_id()} peek={self.peek_visible}/{self.peek_lines}",
            "",
            "== labels file ==",
            labels_blob,
            "== events tail ==",
            *read_events(self._events_path, 20),
        ]
        path.write_text("\n".join(lines) + "\n")
        return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cao-fleet", description="Textual fleet view for a CAO session."
    )
    parser.add_argument(
        "--session",
        default=os.environ.get("CAO_SESSION"),
        help="CAO/tmux session name (default: $CAO_SESSION).",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("CAO_ENDPOINT") or DEFAULT_ENDPOINT,
        help=f"cao-server base URL (default: $CAO_ENDPOINT or {DEFAULT_ENDPOINT}).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry for ``cao-fleet`` (D1)."""
    args = parse_args(argv)
    if not args.session:
        sys.stderr.write("cao-fleet: no session — pass --session or set CAO_SESSION\n")
        return 2
    FleetApp(args.session, args.endpoint).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
