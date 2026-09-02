"""F702 (#557) J2: pilot tests for the `cao-fleet` Textual app (D3, D5, AC1–AC5).

Every test drives the real app through ``App.run_test()``. Nothing live is
behind it: the HTTP fetch, the sleep, the clock, tmux and the three scratch
paths are all constructor arguments.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest
from rich.text import Text
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static

from cli_agent_orchestrator.tui.columns import (
    ALL_COLUMNS,
    ALL_VIEW,
    MARKER_BLANK,
    MARKER_INDEX,
    MARKER_SELECTED,
    NEW_COLUMNS,
    PARITY_COLUMNS,
    PARITY_VIEW,
)
from cli_agent_orchestrator.tui.fleet_app import (
    ACCENT_SELECTION,
    GUTTER_WIDTH,
    KEY_HINTS,
    PEEK_RULE_GLYPH,
    RULE_GLYPH,
    SECTION_MARK,
    STYLE_HINT_KEY,
    STYLE_HINT_LABEL,
    FleetApp,
    column_widths,
    detect_tmux_session,
    find_events_sync_script,
    fmt_age,
    format_frame,
    hint_renderable,
    hint_text,
    idle_style,
    parse_args,
    read_events,
    read_labels,
    render_once,
    row_values,
    sort_terminals,
)
from cli_agent_orchestrator.tui.fleet_state import FleetState
from cli_agent_orchestrator.tui.status_cell import STYLE_QUIET_TAG, status_cell

FIXTURES = Path(__file__).parent / "fixtures"

# The tmux verbs that change something. AC4: exactly one of these may run, and
# only on jump.
SIDE_EFFECTING = {"select-window"}


def load_payload(name: str) -> Dict[str, Any]:
    """A `build_fleet()`-shaped fixture (not a failure marker)."""
    data: Dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    assert "__fixture__" not in data, f"{name} is a failure marker, not a payload"
    return data


def load_marker(name: str) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    assert data["payload"] is None
    return data


class FakeTmux:
    """Records every argv and answers the three read verbs from canned data."""

    def __init__(self, *, activity: Dict[str, int] | None = None, capture: str = "pane line\n"):
        self.calls: List[List[str]] = []
        self.activity = activity or {}
        self.capture = capture

    def __call__(self, args: Sequence[str]) -> str | None:
        argv = list(args)
        self.calls.append(argv)
        verb = argv[0]
        if verb == "list-windows":
            return "".join(f"{idx} {stamp}\n" for idx, stamp in self.activity.items())
        if verb == "list-panes":
            return "%7\n"
        if verb == "capture-pane":
            return self.capture
        return ""

    @property
    def verbs(self) -> List[str]:
        return [call[0] for call in self.calls]

    @property
    def side_effects(self) -> List[List[str]]:
        return [call for call in self.calls if call[0] in SIDE_EFFECTING]


class Feed:
    """An injected fetch/sleep pair with a frozen clock and a one-step gate.

    ``outcomes`` are returned (or raised) one per fetch. The injected sleep
    parks on a gate the test releases, so the fetch loop advances exactly one
    iteration per :func:`advance` call — no wall clock, no race.
    """

    def __init__(self, outcomes: Sequence[Any], *, tick: float = 1.0) -> None:
        self._outcomes = list(outcomes)
        self._tick = tick
        self._gate: asyncio.Event | None = None
        self.clock = 1000.0
        self.sleeps: List[float] = []
        self.fetches = 0

    def now(self) -> float:
        return self.clock

    def fetch(self, url: str, timeout: float = 5.0) -> Any:
        self.fetches += 1
        if not self._outcomes:
            raise _Exhausted("the test released more iterations than it queued")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += self._tick
        if self._gate is None:
            self._gate = asyncio.Event()
        self._gate.clear()
        await self._gate.wait()

    def release(self) -> None:
        if self._gate is not None:
            self._gate.set()


class _Exhausted(Exception):
    """Raised by the fetch when a test releases an unqueued iteration."""


def make_app(
    outcomes: Sequence[Any],
    tmp_path: Path,
    *,
    tmux: FakeTmux | None = None,
    labels: str | None = None,
    events: str | None = None,
    tick: float = 1.0,
    poll_interval: float = 2.0,
    sync_script: Path | None = None,
    spawn: Any = None,
) -> tuple[FleetApp, Feed, FakeTmux]:
    feed = Feed(outcomes, tick=tick)
    runner = tmux or FakeTmux(activity={"0": 990, "2": 999, "3": 999})
    labels_path = tmp_path / "fleet-labels.tsv"
    events_path = tmp_path / "fleet-events.log"
    if labels is not None:
        labels_path.write_text(labels)
    if events is not None:
        events_path.write_text(events)
    app = FleetApp(
        "f702-test",
        "http://127.0.0.1:9889",
        runner=runner,
        fetch=feed.fetch,
        sleep=feed.sleep,
        now=feed.now,
        labels_path=labels_path,
        events_path=events_path,
        snapshot_dir=tmp_path / "fleet-snapshots",
        poll_interval=poll_interval,
        # A set-but-absent path resolves to "no sync script", so no test ever
        # spawns a real child by accident (see the events-sync tests below).
        sync_script=sync_script if sync_script is not None else tmp_path / "absent-sync.sh",
        spawn=spawn if spawn is not None else Spawns(),
    )
    return app, feed, runner


class Spawns:
    """Records every events-sync launch; the child is never real."""

    def __init__(self, *, running: bool = False) -> None:
        self.calls: List[Path] = []
        self._running = running

    def __call__(self, script: Path) -> Any:
        self.calls.append(script)
        return FakeProc(running=self._running)


class FakeProc:
    def __init__(self, *, running: bool = False) -> None:
        self._running = running

    def poll(self) -> int | None:
        return None if self._running else 0


def cell(table: DataTable[Any], row: int, column: int) -> Any:
    return table.get_cell_at(Coordinate(row, column))


def plain(table: DataTable[Any], row: int, column: int) -> str:
    value = cell(table, row, column)
    return value.plain if isinstance(value, Text) else str(value)


async def wait_for_sleeps(pilot: Any, feed: Feed, count: int) -> None:
    """Pump the event loop until the fetch loop has parked ``count`` times."""
    for _ in range(1000):
        if len(feed.sleeps) >= count:
            await pilot.pause()
            await pilot.pause()
            return
        await pilot.pause()
    raise AssertionError(f"loop stalled at {len(feed.sleeps)} sleeps, wanted {count}")


async def settle(pilot: Any, feed: Feed) -> None:
    """Wait for the first fetch to be rendered and the loop to park."""
    await wait_for_sleeps(pilot, feed, 1)


async def advance(pilot: Any, feed: Feed, iterations: int = 1) -> None:
    """Release exactly ``iterations`` further fetch/post/sleep cycles."""
    for _ in range(iterations):
        target = len(feed.sleeps) + 1
        feed.release()
        await wait_for_sleeps(pilot, feed, target)


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_fmt_age_matches_the_script_thresholds() -> None:
    assert fmt_age(None) == "-"
    assert fmt_age(0) == "0s"
    assert fmt_age(119) == "119s"
    assert fmt_age(120) == "2m"
    assert fmt_age(7199) == "119m"
    assert fmt_age(7200) == "2h"


def test_read_labels_skips_lines_without_a_tab(tmp_path: Path) -> None:
    path = tmp_path / "labels.tsv"
    path.write_text("t1\tgate lane\nnot a row\nt2\tdev lane\textra\n")
    assert read_labels(path) == {"t1": "gate lane", "t2": "dev lane\textra"}


def test_read_labels_and_events_tolerate_a_missing_file(tmp_path: Path) -> None:
    assert read_labels(tmp_path / "nope.tsv") == {}
    assert read_events(tmp_path / "nope.log") == []


def test_sort_terminals_puts_supervisors_first(tmp_path: Path) -> None:
    state = FleetState.from_dict(load_payload("healthy"), fetched_at=1.0)
    ordered = sort_terminals(state.terminals)
    assert [t.id for t in ordered] == ["term-0001", "term-0002", "term-0003"]
    assert not ordered[0].parent_id


# ── AC5: columns ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_view_is_exactly_the_six_parity_columns(tmp_path: Path) -> None:
    """AC5: the default view is parity, in the order of fleet-tui.py:344."""
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        table = app.table
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == list(PARITY_VIEW)
        # AC5: the six parity headers, verbatim, after the header-less gutter.
        assert labels[1:] == ["WIN", "ID", "PROFILE", "TASK", "STATUS", "IDLE"]
        assert labels[MARKER_INDEX] == ""


@pytest.mark.asyncio
async def test_c_reveals_the_five_new_columns_and_toggles_back(tmp_path: Path) -> None:
    """AC5: `c` adds exactly the five new columns, after the six parity ones."""
    app, feed, _ = make_app([load_payload("delegating")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("c")
        await pilot.pause()
        labels = [str(col.label) for col in app.table.columns.values()]
        assert labels == list(ALL_VIEW)
        assert labels[len(PARITY_VIEW) :] == list(NEW_COLUMNS)

        rows = app.visible_terminals()
        supervisor = next(i for i, t in enumerate(rows) if t.id == "term-0021")
        assert plain(app.table, supervisor, ALL_VIEW.index("DELEG")) == "delegating (2)"
        assert plain(app.table, supervisor, ALL_VIEW.index("LIFE")) == "persistent"
        assert plain(app.table, supervisor, ALL_VIEW.index("MODEL")) == "claude-opus-5"

        await pilot.press("c")
        await pilot.pause()
        assert [str(c.label) for c in app.table.columns.values()] == list(PARITY_VIEW)


# ── AC2: the seven fixtures render ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture,expected_rows",
    [("healthy", 3), ("error_latched", 3), ("delegating", 3), ("wake_alarm", 2)],
)
async def test_payload_fixtures_render_every_row(
    fixture: str, expected_rows: int, tmp_path: Path
) -> None:
    app, feed, _ = make_app([load_payload(fixture)], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.table.row_count == expected_rows
        assert "not yet fetched" not in app.badge_text()
        assert app.state.last_error is None


@pytest.mark.asyncio
async def test_healthy_fixture_status_cells_carry_glyph_and_style(tmp_path: Path) -> None:
    """AC2/B12: STATUS cells are `Text`; assert `.plain` and `.style`."""
    app, feed, _ = make_app(
        [load_payload("healthy")], tmp_path, labels="term-0002\tbuild the fetcher\n"
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        table = app.table
        status = PARITY_VIEW.index("STATUS")

        assert plain(table, 0, status) == "◌ idle"
        assert cell(table, 0, status).style == "yellow"
        assert plain(table, 1, status) == "● working"
        assert cell(table, 1, status).style == "green"
        assert plain(table, 2, status) == "· completed"
        assert cell(table, 2, status).style == "dim"

        # Parity columns around it.
        assert plain(table, 0, PARITY_VIEW.index("WIN")) == "0"
        assert plain(table, 0, PARITY_VIEW.index("ID")) == "term-0001"
        assert plain(table, 0, PARITY_VIEW.index("PROFILE")) == "chao_supervisor"
        assert plain(table, 0, PARITY_VIEW.index("TASK")) == "(supervisor seat)"
        assert plain(table, 1, PARITY_VIEW.index("TASK")) == "build the fetcher"
        assert plain(table, 2, PARITY_VIEW.index("TASK")) == "(unlabeled)"
        # IDLE from tmux window_activity: window 3 was stamped at 999.
        assert plain(table, 2, PARITY_VIEW.index("IDLE")) == fmt_age(feed.clock - 999)


@pytest.mark.asyncio
async def test_error_latched_and_wake_alarm_fixtures_render_their_named_values(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app([load_payload("error_latched")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        status = PARITY_VIEW.index("STATUS")
        assert plain(app.table, 1, status) == "· error"
        assert plain(app.table, 2, status) == "· error [CAPPED]"
        assert cell(app.table, 2, status).style == "bold red"

    app2, feed2, _ = make_app([load_payload("wake_alarm")], tmp_path)
    async with app2.run_test() as pilot:
        await settle(pilot, feed2)
        status = PARITY_VIEW.index("STATUS")
        assert plain(app2.table, 1, status) == "x WEDGE? [BUSY]"
        assert cell(app2.table, 1, status).style == "bold red"
        assert len(app2.state.wake_exhaustion_alarms) >= 1


@pytest.mark.asyncio
async def test_empty_session_fixture_renders_no_rows_and_no_error(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("empty_session")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.table.row_count == 0
        assert app.state.last_error is None
        assert "0 workers" in app.badge_text()
        assert app.selected_id() is None


@pytest.mark.asyncio
async def test_never_fetched_marker_shows_the_not_yet_fetched_badge(tmp_path: Path) -> None:
    """The `never_fetched.json` case: nothing has arrived, badge says so (AC2)."""
    marker = load_marker("never_fetched")
    assert marker["__fixture__"] == "never_fetched"
    app, _, _ = make_app([], tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state.fetched_at is None
        assert app.badge_text().endswith("not yet fetched")
        assert app.table.row_count == 0


@pytest.mark.asyncio
async def test_fetch_timeout_marker_keeps_rows_and_grows_the_stale_badge(tmp_path: Path) -> None:
    """The `fetch_timeout.json` case: two consecutive failures after a good fetch."""
    marker = load_marker("fetch_timeout")
    assert marker["consecutive_failures"] == 2
    failure = TimeoutError(marker["error"])
    app, feed, _ = make_app([load_payload("healthy"), failure, failure], tmp_path, tick=5.0)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await advance(pilot, feed, 2)
        assert app.table.row_count == 3  # rows kept, not cleared
        badge = app.badge_text()
        assert badge.startswith("▌ CAO fleet · f702-test  ")
        assert " · 2 workers · fetch " in badge
        assert " · stale " in badge
        assert marker["error"] in str(app.state.last_error)


# ── AC1: failure posture ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failures_grow_the_badge_back_off_and_add_no_error_rows(tmp_path: Path) -> None:
    """AC1: stale badge grows, ladder is 2 -> 4 -> 8 -> 10, no error rows."""
    failures = [TimeoutError("timed out")] * 5
    app, feed, _ = make_app([load_payload("healthy"), *failures], tmp_path, tick=5.0)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await advance(pilot, feed)
        first_badge = app.badge_text()
        await advance(pilot, feed, 4)
        assert feed.sleeps == [2.0, 2.0, 4.0, 8.0, 10.0, 10.0]
        assert app.table.row_count == 3
        assert [t.id for t in app.state.terminals] == ["term-0001", "term-0002", "term-0003"]
        stale_first = int(first_badge.split("stale ")[1].rstrip("s"))
        stale_last = int(app.badge_text().split("stale ")[1].rstrip("s"))
        assert stale_last > stale_first


@pytest.mark.asyncio
async def test_a_non_timeout_exception_keeps_the_worker_looping(tmp_path: Path) -> None:
    """AC1: an injected non-timeout exception is recorded; the loop continues."""
    app, feed, _ = make_app([ValueError("bad json"), load_payload("healthy")], tmp_path, tick=1.0)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.state.last_error == "bad json"
        assert app.state.fetched_at is None
        await advance(pilot, feed)
        assert app.state.last_error is None
        assert app.table.row_count == 3


# ── AC4: side effects ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jump_issues_exactly_one_select_window_and_nothing_else_mutates(
    tmp_path: Path,
) -> None:
    """AC4: `enter` runs one `tmux select-window`; every other tmux call is a read."""
    tmux = FakeTmux(activity={"0": 990, "2": 999, "3": 999})
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, tmux=tmux)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert tmux.side_effects == []
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert tmux.side_effects == [["select-window", "-t", "f702-test:2"]]
    assert set(tmux.verbs) <= {"list-windows", "list-panes", "capture-pane", "select-window"}
    assert app.flash.startswith("jumped to codex_dev-term-0002")


@pytest.mark.asyncio
async def test_o_jumps_too_and_a_failed_jump_flashes(tmp_path: Path) -> None:
    class Failing(FakeTmux):
        def __call__(self, args: Sequence[str]) -> str | None:
            out = super().__call__(args)
            return None if args[0] == "select-window" else out

    tmux = Failing(activity={"0": 990, "2": 999, "3": 999})
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, tmux=tmux)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("o")
        await pilot.pause()
    assert tmux.side_effects == [["select-window", "-t", "f702-test:0"]]
    assert app.flash == "jump failed (f702-test:0)"


@pytest.mark.asyncio
async def test_jump_on_an_empty_table_runs_no_subprocess(tmp_path: Path) -> None:
    tmux = FakeTmux()
    app, feed, _ = make_app([load_payload("empty_session")], tmp_path, tmux=tmux)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("enter")
        await pilot.pause()
    assert tmux.side_effects == []
    assert app.flash == "nothing selected"


# ── keys ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_movement_keys_move_the_row_cursor(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.selected_id() == "term-0001"
        await pilot.press("j")
        assert app.selected_id() == "term-0002"
        await pilot.press("down")
        assert app.selected_id() == "term-0003"
        await pilot.press("k")
        assert app.selected_id() == "term-0002"
        await pilot.press("up")
        assert app.selected_id() == "term-0001"
        await pilot.press("G")
        assert app.selected_id() == "term-0003"
        await pilot.press("g")
        assert app.selected_id() == "term-0001"


@pytest.mark.asyncio
async def test_selection_survives_a_refresh(tmp_path: Path) -> None:
    """The row cursor tracks the terminal id, not the row index."""
    app, feed, _ = make_app([load_payload("healthy")] * 3, tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("G")
        assert app.selected_id() == "term-0003"
        await advance(pilot, feed, 2)
        assert app.selected_id() == "term-0003"


@pytest.mark.asyncio
async def test_peek_keys_toggle_the_pane_and_resize_it(tmp_path: Path) -> None:
    tmux = FakeTmux(activity={"0": 990}, capture="line one\nline two\n\n")
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, tmux=tmux)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        peek = app.query_one("#peek", Static)
        assert app.peek_visible is True and peek.display is True
        assert "line two" in str(peek.render())

        await pilot.press("p")
        assert app.peek_visible is False and peek.display is False

        await pilot.press("p")
        assert app.peek_visible is True

        assert app.peek_lines == 14
        await pilot.press("plus")
        assert app.peek_lines == 18
        await pilot.press("equals_sign")
        assert app.peek_lines == 22
        await pilot.press("minus")
        assert app.peek_lines == 18
        for _ in range(10):
            await pilot.press("minus")
        assert app.peek_lines == 6
        for _ in range(12):
            await pilot.press("plus")
        assert app.peek_lines == 40


@pytest.mark.asyncio
async def test_d_toggles_the_debug_pane_with_raw_json_and_latency(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        debug = app.query_one("#debug", Static)
        assert debug.display is False
        await pilot.press("d")
        assert debug.display is True
        text = app.debug_text()
        assert "f702-healthy" in text  # raw JSON
        assert "fetch=" in text and "ms" in text
        await pilot.press("d")
        assert app.query_one("#debug", Static).display is False


@pytest.mark.asyncio
async def test_s_writes_a_snapshot_under_the_snapshot_dir(tmp_path: Path) -> None:
    app, feed, _ = make_app(
        [load_payload("healthy")],
        tmp_path,
        labels="term-0002\tdev lane\n",
        events="event one\nevent two\n",
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("s")
        await pilot.pause()
    written = list((tmp_path / "fleet-snapshots").glob("fleet-*.txt"))
    assert len(written) == 1
    body = written[0].read_text()
    assert "cao-fleet snapshot" in body
    assert "term-0002\tdev lane" in body
    assert "event two" in body
    assert app.flash.startswith("snapshot written: ")


@pytest.mark.asyncio
async def test_events_log_tail_lands_in_the_recent_section(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, events="alpha\nbravo\ncharlie\n")
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        body = str(app.query_one("#events", Static).render())
        for line in ("alpha", "bravo", "charlie"):
            # each line is indented two spaces, as the script writes them
            assert f"  {line}" in body
        assert app.query_one("#events-title", Static).display is True
        assert read_events(app._events_path) == ["alpha", "bravo", "charlie"]


@pytest.mark.asyncio
async def test_an_empty_events_feed_hides_the_whole_recent_section(tmp_path: Path) -> None:
    """The script's ``if ev:`` guard: no feed, no title and no empty box."""
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, events="")
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.query_one("#events-title", Static).display is False
        assert app.query_one("#events", Static).display is False


@pytest.mark.asyncio
async def test_q_quits(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("q")
        await pilot.pause()
        assert app._running is False or app.is_running is False


@pytest.mark.asyncio
async def test_every_binding_names_an_action_the_app_implements(tmp_path: Path) -> None:
    """No binding can point at a missing action (the key set is the contract)."""
    keys = set()
    for binding in FleetApp.BINDINGS:
        keys.add(binding.key)
        if binding.action != "quit":
            assert hasattr(FleetApp, f"action_{binding.action}"), binding
    assert keys == {
        "q",
        "up",
        "k",
        "down",
        "j",
        "g",
        "G",
        "enter",
        "o",
        "p",
        "plus",
        "equals_sign",
        "minus",
        "d",
        "s",
        "c",
    }


# ── parity: on-screen key hints (ranked gap 1) ───────────────────────────────

#: Which hint fragment covers each binding key. Every binding must map to a
#: fragment that actually appears on screen — that is what stops a binding
#: from landing without a hint, and what fails if the hint line is deleted.
HINT_COVERAGE = {
    "q": "q quit",
    "up": "↑↓/jk select",
    "down": "↑↓/jk select",
    "k": "↑↓/jk select",
    "j": "↑↓/jk select",
    "g": "g/G ends",
    "G": "g/G ends",
    "enter": "⏎/o jump",
    "o": "⏎/o jump",
    "p": "p peek",
    "plus": "+/- size",
    "equals_sign": "+/- size",
    "minus": "+/- size",
    "d": "d debug",
    "s": "s snapshot",
    "c": "c columns",
}


def test_every_binding_key_is_covered_by_an_on_screen_hint() -> None:
    """Gap 1: d/s/p/c (and the rest) are discoverable without the source."""
    text = hint_text()
    for binding in FleetApp.BINDINGS:
        assert binding.key in HINT_COVERAGE, f"binding {binding.key} has no hint"
        assert HINT_COVERAGE[binding.key] in text, binding.key
    for key, label in KEY_HINTS:
        assert f"{key} {label}" in text


@pytest.mark.asyncio
async def test_the_hint_line_is_rendered_in_the_app(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        rendered = str(app.query_one("#hints", Static).render())
        for fragment in ("d debug", "s snapshot", "p peek", "c columns", "q quit"):
            assert fragment in rendered


# ── parity: loud server-down state (ranked gap 2) ────────────────────────────


@pytest.mark.asyncio
async def test_a_fetch_failure_raises_a_loud_unreachable_line_with_the_last_good_time(
    tmp_path: Path,
) -> None:
    """Gap 2: red header line, not a subtle badge; last good fetch is named."""
    app, feed, _ = make_app(
        [load_payload("healthy"), TimeoutError("timed out")], tmp_path, tick=5.0
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        alert = app.query_one("#alert", Static)
        assert app.alert_text() == ""
        assert alert.display is False

        await advance(pilot, feed)
        text = app.alert_text()
        assert text.startswith("fleet endpoint unreachable: timed out")
        assert "last good " in text
        assert "never" not in text
        assert alert.display is True
        assert app.table.row_count == 3  # last good rows kept


@pytest.mark.asyncio
async def test_the_unreachable_line_says_never_when_no_fetch_ever_succeeded(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app([ConnectionRefusedError("refused")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.alert_text().endswith("· last good never")
        assert app.query_one("#alert", Static).display is True


# ── parity: the row colour language (ranked gap 3) ──────────────────────────


def test_idle_style_matches_the_scripts_thresholds() -> None:
    assert idle_style(None) == "dim"
    assert idle_style(0.0) == "dim"
    assert idle_style(4.9) == "dim"
    assert idle_style(5.0) == ""
    assert idle_style(299.0) == ""
    assert idle_style(300.0) == "yellow"
    assert idle_style(10_000.0) == "yellow"


@pytest.mark.asyncio
async def test_supervisor_and_worker_rows_carry_the_scripts_colours(tmp_path: Path) -> None:
    """Gap 3: supervisor magenta, worker id cyan, worker WIN dim."""
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        table = app.table
        win, tid, profile = (PARITY_VIEW.index(n) for n in ("WIN", "ID", "PROFILE"))

        assert cell(table, 0, win).style == "magenta"
        assert cell(table, 0, tid).style == "bold magenta"
        assert cell(table, 0, profile).style == "magenta"

        assert cell(table, 1, win).style == "dim"
        assert cell(table, 1, tid).style == "cyan"
        assert cell(table, 1, profile).style == ""


@pytest.mark.asyncio
async def test_unlabeled_tasks_are_dim_and_labeled_ones_are_not(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, labels="term-0002\tdev lane\n")
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        task = PARITY_VIEW.index("TASK")
        assert plain(app.table, 1, task) == "dev lane"
        assert cell(app.table, 1, task).style == ""
        assert plain(app.table, 2, task) == "(unlabeled)"
        assert cell(app.table, 2, task).style == "dim"


@pytest.mark.asyncio
async def test_idle_cells_are_coloured_by_age(tmp_path: Path) -> None:
    """A window last active 10 min ago is yellow; a fresh one is dim."""
    tmux = FakeTmux(activity={"0": 400, "2": 999, "3": 999})  # clock is 1000.0
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, tmux=tmux)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        idle = PARITY_VIEW.index("IDLE")
        assert plain(app.table, 0, idle) == "10m"
        assert cell(app.table, 0, idle).style == "yellow"
        fresh = plain(app.table, 1, idle)
        assert fresh.endswith("s") and int(fresh[:-1]) < 5
        assert cell(app.table, 1, idle).style == "dim"


# ── parity: the ▶ selection marker (ranked gap 4) ───────────────────────────


@pytest.mark.asyncio
async def test_the_selection_gutter_marks_exactly_the_cursor_row(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)

        def markers() -> list[str]:
            return [plain(app.table, i, MARKER_INDEX) for i in range(app.table.row_count)]

        assert markers() == [MARKER_SELECTED, MARKER_BLANK, MARKER_BLANK]
        await pilot.press("j")
        assert markers() == [MARKER_BLANK, MARKER_SELECTED, MARKER_BLANK]
        await pilot.press("G")
        assert markers() == [MARKER_BLANK, MARKER_BLANK, MARKER_SELECTED]
        await pilot.press("g")
        assert markers() == [MARKER_SELECTED, MARKER_BLANK, MARKER_BLANK]


@pytest.mark.asyncio
async def test_the_marker_survives_a_refresh_on_the_selected_row(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")] * 3, tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("G")
        await advance(pilot, feed, 2)
        assert app.selected_id() == "term-0003"
        assert plain(app.table, 2, MARKER_INDEX) == MARKER_SELECTED
        assert plain(app.table, 0, MARKER_INDEX) == MARKER_BLANK


# ── parity: events-feed sync cadence (ranked gap 5) ─────────────────────────


@pytest.mark.asyncio
async def test_the_events_sync_script_fires_once_and_is_then_throttled(
    tmp_path: Path,
) -> None:
    """Gap 5: fleet-events-sync.sh runs on the first frame, then ≥30 s apart."""
    script = tmp_path / "fleet-events-sync.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    spawns = Spawns()
    app, feed, _ = make_app(
        [load_payload("healthy")] * 4,
        tmp_path,
        sync_script=script,
        spawn=spawns,
        tick=5.0,
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert spawns.calls == [script]  # fired on the first render
        await advance(pilot, feed, 3)  # clock 1000 -> 1015: inside the throttle
        assert spawns.calls == [script]
        feed.clock += 31.0
        app.refresh_events()
        assert spawns.calls == [script, script]


@pytest.mark.asyncio
async def test_a_missing_sync_script_is_a_silent_no_op(tmp_path: Path) -> None:
    spawns = Spawns()
    app, feed, _ = make_app(
        [load_payload("healthy")], tmp_path, sync_script=tmp_path / "nope.sh", spawn=spawns
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert spawns.calls == []
        assert app.errors == []


@pytest.mark.asyncio
async def test_a_still_running_sync_child_is_not_relaunched(tmp_path: Path) -> None:
    script = tmp_path / "fleet-events-sync.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    spawns = Spawns(running=True)
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, sync_script=script, spawn=spawns)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert spawns.calls == [script]
        feed.clock += 10_000.0
        app.refresh_events()
        assert spawns.calls == [script]  # the previous run has not finished


def test_find_events_sync_script_prefers_the_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-sync.sh"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CAO_FLEET_EVENTS_SYNC", str(override))
    assert find_events_sync_script(tmp_path / "fleet-events.log") == override

    monkeypatch.setenv("CAO_FLEET_EVENTS_SYNC", str(tmp_path / "gone.sh"))
    assert find_events_sync_script(tmp_path / "fleet-events.log") is None


def test_find_events_sync_script_falls_back_to_the_events_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAO_FLEET_EVENTS_SYNC", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_events_sync_script(tmp_path / "fleet-events.log") is None
    sibling = tmp_path / "fleet-events-sync.sh"
    sibling.write_text("#!/bin/sh\n")
    assert find_events_sync_script(tmp_path / "fleet-events.log") == sibling


# ── parity: the WIN column (ranked gap 6) ───────────────────────────────────


@pytest.mark.asyncio
async def test_win_renders_the_real_window_index_from_a_stringified_payload(
    tmp_path: Path,
) -> None:
    """The live server sends `window_index` as a string (clients/tmux.py:1423).

    Before the fix every live row rendered `?`; the fixtures used ints and hid
    it. Window 0 must render as `0`, never as `?`.
    """
    payload = load_payload("healthy")
    for row in payload["terminals"]:
        row["window_index"] = str(row["window_index"])
    app, feed, _ = make_app([payload], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        win = PARITY_VIEW.index("WIN")
        assert [plain(app.table, i, win) for i in range(3)] == ["0", "2", "3"]
        assert "?" not in [plain(app.table, i, win) for i in range(3)]


@pytest.mark.asyncio
async def test_win_is_a_question_mark_only_when_the_server_sends_no_window(
    tmp_path: Path,
) -> None:
    payload = load_payload("healthy")
    payload["terminals"][1]["window_index"] = None
    payload["terminals"][2]["window_index"] = "not-a-number"
    app, feed, _ = make_app([payload], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        win = PARITY_VIEW.index("WIN")
        assert plain(app.table, 0, win) == "0"
        assert {plain(app.table, 1, win), plain(app.table, 2, win)} == {"?"}


# ── parity: header, error ring, snapshot, launch contract ───────────────────


@pytest.mark.asyncio
async def test_the_header_carries_session_clock_worker_count_latency_and_liveness(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        badge = app.badge_text()
        clock = time.strftime("%H:%M:%S", time.localtime(feed.clock))
        # The script's header line, verbatim: the ▌ section mark, the accented
        # title, two spaces, then the dim tail (fleet-tui.py:337-339).
        assert badge == f"▌ CAO fleet · f702-test  {clock} · 2 workers · fetch 0ms · live"


@pytest.mark.asyncio
async def test_the_error_ring_records_failures_and_the_debug_pane_shows_them(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app(
        [load_payload("healthy"), TimeoutError("timed out"), OSError("refused")],
        tmp_path,
        tick=1.0,
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.errors == []
        await advance(pilot, feed, 2)
        messages = [message for _, message in app.errors]
        assert messages == ["fetch: timed out", "fetch: refused"]
        assert "fetch: timed out" in app.debug_text()
        assert "fetch: refused" in app.debug_text()


@pytest.mark.asyncio
async def test_the_error_ring_is_capped_at_twenty_entries(tmp_path: Path) -> None:
    app, _, _ = make_app([], tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for index in range(30):
            app.record_error(f"boom {index}")
        assert len(app.errors) == 20
        assert app.errors[0][1] == "boom 10"
        assert app.errors[-1][1] == "boom 29"
        assert len(app.error_ring_lines()) == 8


@pytest.mark.asyncio
async def test_a_failing_tmux_read_lands_in_the_error_ring(tmp_path: Path) -> None:
    class Dead(FakeTmux):
        def __call__(self, args: Sequence[str]) -> str | None:
            super().__call__(args)
            return None

    app, feed, _ = make_app([load_payload("healthy")], tmp_path, tmux=Dead())
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert any(message.startswith("tmux list-windows") for _, message in app.errors)


@pytest.mark.asyncio
async def test_the_snapshot_carries_the_raw_json_and_the_error_ring(tmp_path: Path) -> None:
    app, feed, _ = make_app(
        [load_payload("healthy")],
        tmp_path,
        labels="term-0002\tdev lane\n",
        events="event one\nevent two\n",
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        app.record_error("synthetic ring entry")
        await pilot.press("s")
        await pilot.pause()
    body = next((tmp_path / "fleet-snapshots").glob("fleet-*.txt")).read_text()
    assert "== raw fleet json ==" in body
    assert "f702-healthy" in body  # the payload itself, not just a summary
    assert "== tui error ring ==" in body
    assert "synthetic ring entry" in body
    assert "(endpoint reachable)" in body


def test_parse_args_restores_interval_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_SESSION", raising=False)
    args = parse_args(["--session", "s", "--interval", "7.5", "--once"])
    assert (args.session, args.interval, args.once) == ("s", 7.5, True)
    default = parse_args([])
    assert default.interval == 2.0 and default.once is False


def test_detect_tmux_session_reads_the_enclosing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    assert detect_tmux_session(lambda args: "should-not-be-called\n") is None

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    seen: list[Sequence[str]] = []

    def runner(args: Sequence[str]) -> str | None:
        seen.append(args)
        return "cao-claude-orch5\n"

    assert detect_tmux_session(runner) == "cao-claude-orch5"
    assert seen == [["display-message", "-p", "#S"]]
    assert detect_tmux_session(lambda args: None) is None


@pytest.mark.asyncio
async def test_the_poll_interval_drives_the_loop_and_its_backoff(tmp_path: Path) -> None:
    failures = [TimeoutError("x")] * 3
    app, feed, _ = make_app(
        [load_payload("healthy"), *failures], tmp_path, poll_interval=5.0, tick=1.0
    )
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await advance(pilot, feed, 3)
        assert feed.sleeps == [5.0, 5.0, 10.0, 10.0]


# ── parity: the --once frame ────────────────────────────────────────────────


def test_render_once_prints_a_frame_with_rows_marker_and_hints(tmp_path: Path) -> None:
    payload = load_payload("healthy")
    labels_path = tmp_path / "labels.tsv"
    labels_path.write_text("term-0002\tdev lane\n")
    events_path = tmp_path / "events.log"
    events_path.write_text("first event\nsecond event\n")
    frame = render_once(
        "f702-test",
        "http://127.0.0.1:9889",
        fetch=lambda url, timeout=5.0: payload,
        runner=lambda args: "0 100\n2 100\n3 100\n",
        labels_path=labels_path,
        events_path=events_path,
    )
    assert "▌ CAO fleet · f702-test" in frame
    assert "WIN" in frame and "STATUS" in frame
    assert "▶ 0" in frame  # first row selected
    assert "dev lane" in frame
    assert "second event" in frame
    assert "d debug" in frame


def test_render_once_reports_an_unreachable_endpoint(tmp_path: Path) -> None:
    def boom(url: str, timeout: float = 5.0) -> Any:
        raise ConnectionRefusedError("connection refused")

    frame = render_once(
        "f702-test",
        "http://127.0.0.1:9889",
        fetch=boom,
        runner=lambda args: "",
        labels_path=tmp_path / "labels.tsv",
        events_path=tmp_path / "events.log",
    )
    assert frame.startswith("fleet endpoint unreachable: connection refused")
    assert "(no workers)" in frame


def test_format_frame_widths_follow_the_scripts_rule(tmp_path: Path) -> None:
    state = FleetState.from_dict(load_payload("healthy"), fetched_at=1.0)
    frame = format_frame("s", state, {}, {}, [], latency_ms=12, clock="00:00:00")
    header = next(line for line in frame.splitlines() if line.strip().startswith("WIN"))
    rows = [line for line in frame.splitlines() if line.startswith(("▶ ", "  term"))]
    assert header.startswith("  WIN")
    # every ID starts at the same column as the ID header
    id_column = header.index("ID")
    assert all(line[id_column:].startswith("term-") for line in rows)


def test_row_values_is_the_single_source_of_the_row_text() -> None:
    state = FleetState.from_dict(load_payload("healthy"), fetched_at=1.0)
    supervisor = next(t for t in state.terminals if not t.parent_id)
    assert row_values(supervisor, {}, {}) == [
        "0",
        "term-0001",
        "chao_supervisor",
        "(supervisor seat)",
        "◌ idle",
        "-",
    ]


# ── the section layout contract (F702 #557 "look" round) ─────────────────────
#
# The retiring script's section order, top to bottom (fleet-tui.py:336-450):
# header, blank, table, blank, recent, blank, status+hints, blank, peek. The
# peek is LAST. These tests are the layout's only guard — a widget reordered in
# `compose` (the peek back into the middle, most of all) fails here and only
# here.

#: Widget ids in the order `compose` yields them.
LAYOUT_ORDER = [
    "badge",
    "alert",
    "table-head",
    "table-rule",
    "fleet",
    "events-title",
    "events",
    "debug",
    "flash",
    "hints",
    "peek",
]


@pytest.mark.asyncio
async def test_the_sections_are_in_the_scripts_order_with_the_peek_last(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, events="alpha\n")
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        ids = [child.id for child in app.query_one(Vertical).children]
        assert ids == LAYOUT_ORDER
        assert ids[-1] == "peek", "the peek is the last section, never the middle"
        # and it is below the table and the recent feed on screen, not just in
        # the widget list
        peek = app.query_one("#peek", Static)
        for above in ("#fleet", "#events", "#hints"):
            assert peek.region.y > app.query_one(above).region.y


@pytest.mark.asyncio
async def test_every_section_title_carries_the_section_mark(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, events="alpha\n")
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.badge_text().startswith(f"{SECTION_MARK} CAO fleet")
        assert str(app.query_one("#events-title", Static).render()).startswith(
            f"{SECTION_MARK} recent"
        )
        term = app.selected_terminal()
        assert term is not None
        assert app.peek_title(term).startswith(f"{SECTION_MARK} peek")


@pytest.mark.asyncio
async def test_the_table_header_row_and_its_rule_sit_above_the_rows(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        head = str(app.query_one("#table-head", Static).render())
        rule = str(app.query_one("#table-rule", Static).render())
        assert head.startswith("  WIN")
        for name in PARITY_COLUMNS:
            assert name in head
        assert set(rule) == {RULE_GLYPH}
        assert len(rule) == app.frame_width()
        # header cells line up with the table's own columns
        widths = [int(column.width) for column in app.table.columns.values()]
        assert head.index("ID") == widths[0] + widths[1]


@pytest.mark.asyncio
async def test_the_peek_banner_is_a_title_over_a_full_width_double_rule(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        banner = app.peek_banner(app.selected_terminal()).plain.splitlines()
        assert banner[0].startswith(f"{SECTION_MARK} peek · ")
        assert set(banner[1]) == {PEEK_RULE_GLYPH}
        assert len(banner[1]) == app.frame_width()
        # the banner is the head of what the peek widget actually shows
        assert str(app.query_one("#peek", Static).render()).startswith(banner[0])


@pytest.mark.asyncio
async def test_the_table_columns_carry_the_scripts_two_space_gutter(tmp_path: Path) -> None:
    """No DataTable cell padding: the widths themselves hold the gutter."""
    app, feed, _ = make_app([load_payload("healthy")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.table.cell_padding == 0
        assert app.table.show_header is False
        assert app.table.zebra_stripes is False
        values = [row_values(t, {}, {}) for t in app.visible_terminals()]
        widths = app.frame_widths(values)
        assert widths[0] == GUTTER_WIDTH
        # every named column is its widest cell plus the two-space gutter,
        # except the last, which is stretched to the edge of the screen
        expected = column_widths(PARITY_COLUMNS, values)
        assert widths[1:-1] == expected[:-1]
        assert sum(widths) == app.frame_width()


def test_the_selection_bar_uses_the_scripts_accent_not_the_textual_theme() -> None:
    """DataTable's own DEFAULT_CSS would repaint the bar in the theme accent."""
    assert "#fleet > .datatable--cursor" in FleetApp.CSS
    assert ACCENT_SELECTION in FleetApp.CSS


def test_the_hint_line_is_bold_key_dim_label() -> None:
    line = hint_renderable()
    assert line.plain == hint_text()
    styles = {str(span.style) for span in line.spans}
    assert STYLE_HINT_KEY in styles and STYLE_HINT_LABEL in styles


def test_a_busy_tag_is_dimmed_but_the_condition_still_owns_the_cell_style() -> None:
    cell = status_cell({"status": "processing", "condition": "BUSY"})
    assert cell.plain == "● working [BUSY]"
    assert cell.style == "green"  # the D4/B12 contract is untouched
    dimmed = [span for span in cell.spans if str(span.style) == STYLE_QUIET_TAG]
    assert len(dimmed) == 1
    assert cell.plain[dimmed[0].start : dimmed[0].end] == " [BUSY]"


def test_a_loud_condition_tag_is_not_dimmed() -> None:
    cell = status_cell({"status": "idle", "condition": "CAPPED"})
    assert cell.style == "bold red"
    assert [span for span in cell.spans if str(span.style) == STYLE_QUIET_TAG] == []
