"""F702 (#557) J2: pilot tests for the `cao-fleet` Textual app (D3, D5, AC1–AC5).

Every test drives the real app through ``App.run_test()``. Nothing live is
behind it: the HTTP fetch, the sleep, the clock, tmux and the three scratch
paths are all constructor arguments.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest
from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable, RichLog, Static

from cli_agent_orchestrator.tui.columns import ALL_COLUMNS, NEW_COLUMNS, PARITY_COLUMNS
from cli_agent_orchestrator.tui.fleet_app import (
    FleetApp,
    fmt_age,
    read_events,
    read_labels,
    sort_terminals,
)
from cli_agent_orchestrator.tui.fleet_state import FleetState

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
    )
    return app, feed, runner


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
        assert labels == list(PARITY_COLUMNS)
        assert labels == ["WIN", "ID", "PROFILE", "TASK", "STATUS", "IDLE"]


@pytest.mark.asyncio
async def test_c_reveals_the_five_new_columns_and_toggles_back(tmp_path: Path) -> None:
    """AC5: `c` adds exactly the five new columns, after the six parity ones."""
    app, feed, _ = make_app([load_payload("delegating")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        await pilot.press("c")
        await pilot.pause()
        labels = [str(col.label) for col in app.table.columns.values()]
        assert labels == list(ALL_COLUMNS)
        assert labels[len(PARITY_COLUMNS) :] == list(NEW_COLUMNS)

        rows = app.visible_terminals()
        supervisor = next(i for i, t in enumerate(rows) if t.id == "term-0021")
        assert plain(app.table, supervisor, ALL_COLUMNS.index("DELEG")) == "delegating (2)"
        assert plain(app.table, supervisor, ALL_COLUMNS.index("LIFE")) == "persistent"
        assert plain(app.table, supervisor, ALL_COLUMNS.index("MODEL")) == "claude-opus-5"

        await pilot.press("c")
        await pilot.pause()
        assert [str(c.label) for c in app.table.columns.values()] == list(PARITY_COLUMNS)


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
        status = PARITY_COLUMNS.index("STATUS")

        assert plain(table, 0, status) == "◌ idle"
        assert cell(table, 0, status).style == "yellow"
        assert plain(table, 1, status) == "● working"
        assert cell(table, 1, status).style == "green"
        assert plain(table, 2, status) == "· completed"
        assert cell(table, 2, status).style == "dim"

        # Parity columns around it.
        assert plain(table, 0, PARITY_COLUMNS.index("WIN")) == "0"
        assert plain(table, 0, PARITY_COLUMNS.index("ID")) == "term-0001"
        assert plain(table, 0, PARITY_COLUMNS.index("PROFILE")) == "chao_supervisor"
        assert plain(table, 0, PARITY_COLUMNS.index("TASK")) == "(supervisor seat)"
        assert plain(table, 1, PARITY_COLUMNS.index("TASK")) == "build the fetcher"
        assert plain(table, 2, PARITY_COLUMNS.index("TASK")) == "(unlabeled)"
        # IDLE from tmux window_activity: window 3 was stamped at 999.
        assert plain(table, 2, PARITY_COLUMNS.index("IDLE")) == fmt_age(feed.clock - 999)


@pytest.mark.asyncio
async def test_error_latched_and_wake_alarm_fixtures_render_their_named_values(
    tmp_path: Path,
) -> None:
    app, feed, _ = make_app([load_payload("error_latched")], tmp_path)
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        status = PARITY_COLUMNS.index("STATUS")
        assert plain(app.table, 1, status) == "· error"
        assert plain(app.table, 2, status) == "· error [CAPPED]"
        assert cell(app.table, 2, status).style == "bold red"

    app2, feed2, _ = make_app([load_payload("wake_alarm")], tmp_path)
    async with app2.run_test() as pilot:
        await settle(pilot, feed2)
        status = PARITY_COLUMNS.index("STATUS")
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
        assert badge.startswith("CAO fleet · f702-test · 2 workers · stale ")
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
async def test_events_log_tail_lands_in_the_rich_log(tmp_path: Path) -> None:
    app, feed, _ = make_app([load_payload("healthy")], tmp_path, events="alpha\nbravo\ncharlie\n")
    async with app.run_test() as pilot:
        await settle(pilot, feed)
        assert app.query_one("#events", RichLog).lines
        assert read_events(app._events_path) == ["alpha", "bravo", "charlie"]


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
