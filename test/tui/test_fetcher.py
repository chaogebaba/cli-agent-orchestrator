"""F702 (#557) J1: the sequential fetch loop with bounded backoff (D2, AC1)."""

from typing import Any

import pytest

from cli_agent_orchestrator.tui.fetcher import FETCH_INTERVAL, MAX_BACKOFF, run_fetch_loop
from cli_agent_orchestrator.tui.fleet_state import FleetState

RAW: dict[str, Any] = {
    "session_name": "orch",
    "terminals": [{"id": "t1", "status": "idle"}],
    "wake_exhaustion_alarms": [],
}


class _StopLoop(Exception):
    """Sentinel raised from the injected sleep to end the infinite loop."""


class Harness:
    """Drives `run_fetch_loop` for a fixed number of iterations."""

    def __init__(self, outcomes: list[Any], *, tick: float = 1.0) -> None:
        self._outcomes = list(outcomes)
        self.sleeps: list[float] = []
        self.posted: list[FleetState] = []
        self.fetch_calls: list[tuple[str, float]] = []
        self.clock = 100.0
        self._tick = tick

    def fetch(self, url: str, timeout: float = 5.0) -> Any:
        self.fetch_calls.append((url, timeout))
        if not self._outcomes:
            raise _StopLoop
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def post(self, state: FleetState) -> None:
        self.posted.append(state)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += self._tick
        if not self._outcomes:
            raise _StopLoop

    def now(self) -> float:
        return self.clock

    async def run(self, url: str = "http://x/sessions/s/fleet") -> None:
        with pytest.raises(_StopLoop):
            await run_fetch_loop(url, self.post, sleep=self.sleep, fetch=self.fetch, now=self.now)


@pytest.mark.asyncio
async def test_backoff_ladder_caps_at_ten_and_resets_on_success() -> None:
    """2, 4, 8, 10, 10 while failing; back to 2 on the next success."""
    outcomes: list[Any] = [TimeoutError("timed out")] * 5 + [RAW]
    harness = Harness(outcomes)
    await harness.run()

    assert harness.sleeps == [2.0, 4.0, 8.0, 10.0, 10.0, 2.0]
    assert FETCH_INTERVAL == 2.0
    assert MAX_BACKOFF == 10.0


@pytest.mark.asyncio
async def test_first_interval_is_the_normal_interval_not_one_second() -> None:
    """BL2: the ladder starts at the poll interval, never below it."""
    harness = Harness([TimeoutError("timed out"), RAW])
    await harness.run()
    assert harness.sleeps[0] == 2.0


@pytest.mark.asyncio
async def test_ladder_restarts_at_two_after_a_success() -> None:
    """A success clears the failure run, so the next outage starts at 2 s."""
    harness = Harness([RAW, TimeoutError("a"), RAW, TimeoutError("b"), TimeoutError("c")])
    await harness.run()
    assert harness.sleeps == [2.0, 2.0, 2.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_timeout_and_non_timeout_exceptions_are_both_recorded() -> None:
    """AC1: a timeout and an injected ValueError are logged; the loop lives."""
    harness = Harness([TimeoutError("timed out"), ValueError("bad JSON"), RAW])
    await harness.run()

    assert len(harness.posted) == 3
    assert harness.posted[0].last_error == "timed out"
    assert harness.posted[1].last_error == "bad JSON"
    assert harness.posted[2].last_error is None
    assert [r.id for r in harness.posted[2].terminals] == ["t1"]


@pytest.mark.asyncio
async def test_a_state_is_posted_on_every_iteration_including_failures() -> None:
    """Mutant guard: moving `post` into the `else` drops the failure posts."""
    harness = Harness([TimeoutError("t1"), TimeoutError("t2"), RAW, TimeoutError("t3")])
    await harness.run()
    assert len(harness.posted) == 4
    assert [s.last_error for s in harness.posted] == ["t1", "t2", None, "t3"]


@pytest.mark.asyncio
async def test_first_iteration_failure_does_not_raise_name_error() -> None:
    """BLK-B: `state`/`interval` are bound before the loop."""
    harness = Harness([ConnectionRefusedError("refused"), RAW])
    await harness.run()
    assert harness.posted[0].last_error == "refused"
    assert harness.posted[0].terminals == ()
    assert harness.posted[0].fetched_at is None


@pytest.mark.asyncio
async def test_stale_for_grows_across_consecutive_failures() -> None:
    """A success anchors `fetched_at`; each later failure widens the gap."""
    harness = Harness([RAW, TimeoutError("a"), TimeoutError("b"), TimeoutError("c")])
    await harness.run()

    fetched_at = harness.posted[0].fetched_at
    assert fetched_at == 100.0
    assert harness.posted[0].stale_for == 0.0
    assert harness.posted[1].stale_for == 1.0
    assert harness.posted[2].stale_for == 2.0
    assert harness.posted[3].stale_for == 3.0
    # Rows are kept across the whole failure run (#441: staleness, not errors).
    assert all(len(s.terminals) == 1 for s in harness.posted)


@pytest.mark.asyncio
async def test_fetch_is_called_with_the_url_and_a_five_second_timeout() -> None:
    harness = Harness([RAW, RAW])
    await harness.run("http://localhost:9889/sessions/orch/fleet")
    assert harness.fetch_calls == [
        ("http://localhost:9889/sessions/orch/fleet", 5.0),
        ("http://localhost:9889/sessions/orch/fleet", 5.0),
    ]


@pytest.mark.asyncio
async def test_poll_interval_sets_the_cadence_and_the_backoff_floor() -> None:
    """F702 parity: `--interval` reaches the loop and starts the ladder."""
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            raise _Stop()

    calls = {"n": 0}

    def fetch(url: str, timeout: float = 5.0) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"session_name": "s", "terminals": []}
        raise TimeoutError("down")

    with pytest.raises(_Stop):
        await run_fetch_loop(
            "http://x/fleet",
            lambda state: None,
            sleep=sleep,
            fetch=fetch,
            now=lambda: 0.0,
            poll_interval=30.0,
        )
    # 30 healthy, 30 on the first failure, then doubling capped at the cadence.
    assert sleeps == [30.0, 30.0, 30.0, 30.0]


class _Stop(Exception):
    """Ends the endless loop from inside the injected sleep."""
