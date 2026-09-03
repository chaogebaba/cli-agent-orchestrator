"""AC4b (ii) — the fleet liveness probe (WP-ARCH F725 #581, lane B).

Every named run from the blueprint is here: a killed pane produces
``pane.missing`` and then exactly one ``process.exited`` after
``PANE_MISS_TICKS``; an empty probe exits nothing and produces one
``probe.failed``; ``PROBE_FAIL_TICKS`` failures produce
``degraded(producer_error)`` and recover on the next success.

Durations and tick counts are imported from ``core/timing.py``, never restated.
A test that hard-coded ``2`` would pass for the wrong reason the day
``PANE_MISS_TICKS`` moved, and §4c forbids a literal duration anywhere but there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from cli_agent_orchestrator.adapters.truth import wiring
from cli_agent_orchestrator.adapters.truth.liveness_probe import LivenessProbe, PaneRecord
from cli_agent_orchestrator.core.events import (
    FLEET_TERMINAL_ID,
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
)
from cli_agent_orchestrator.core.states import DegradedReason
from cli_agent_orchestrator.core.timing import PANE_MISS_TICKS, PROBE_FAIL_TICKS

from .conftest import FakeClock, FakeEventStore, FakeStateStore


@dataclass(frozen=True)
class _Ref:
    terminal_id: str
    tmux_session: str
    tmux_window: str


ONE = _Ref("t1", "sess", "%1")
TWO = _Ref("t2", "sess", "%2")

PRESENT = [PaneRecord("sess", "%1", 4242), PaneRecord("sess", "%2", 4243)]
ONLY_TWO = [PaneRecord("sess", "%2", 4243)]
OTHER_SESSION = [PaneRecord("elsewhere", "%9", 1)]


def _probe(panes_by_tick: list[list[PaneRecord] | None], fleet: list[_Ref]) -> LivenessProbe:
    """A probe whose successive ticks return the given listings.

    ``None`` means the tmux call raised; an empty list means it answered with
    nothing.  B13 says both are "the probe failed" and neither is pane absence.
    """
    ticks = iter(panes_by_tick)

    def list_panes() -> list[PaneRecord]:
        value = next(ticks)
        if value is None:
            raise RuntimeError("tmux unreachable")
        return value

    return LivenessProbe(list_panes=list_panes, fleet=lambda: fleet)


def test_off_writes_nothing(store: FakeEventStore) -> None:
    probe = _probe([PRESENT], [ONE])
    probe.probe_once()
    assert store.rows == []


def test_a_healthy_tick_writes_columns_and_no_rows(
    ingest_on: FakeEventStore, state_store: FakeStateStore
) -> None:
    """r9 retired ``pane.alive``: a heartbeat is a COLUMN update, never a row.

    This is the mutant "the probe appends a row per tick".
    """
    probe = _probe([PRESENT] * 5, [ONE, TWO])
    for _ in range(5):
        probe.probe_once()
    assert ingest_on.rows == []
    assert len(state_store.probe_touches) == 10
    assert state_store.probe_touches[0]["pane_present"] is True
    assert state_store.probe_touches[0]["pane_pid"] == 4242


def test_a_killed_pane_gives_one_missing_then_exactly_one_exited(
    ingest_on: FakeEventStore,
) -> None:
    ticks = [PRESENT] + [ONLY_TWO] * (PANE_MISS_TICKS + 3)
    probe = _probe(ticks, [ONE, TWO])
    for _ in range(len(ticks)):
        probe.probe_once()

    missing = ingest_on.of_kind(EventKind.PANE_MISSING, "t1")
    exited = ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1")
    assert len(missing) == 1
    assert missing[0].payload["reason"] == DegradedReason.PANE_UNREADABLE.value
    assert len(exited) == 1
    assert exited[0].payload["miss_count"] == PANE_MISS_TICKS
    # The surviving terminal is untouched.
    assert ingest_on.read("t2") == []


def test_one_miss_never_exits(ingest_on: FakeEventStore) -> None:
    """``PANE_MISS_TICKS >= 2`` exists so a single scheduling blip cannot exit a worker."""
    probe = _probe([PRESENT, ONLY_TWO], [ONE, TWO])
    probe.probe_once()
    probe.probe_once()
    assert ingest_on.of_kind(EventKind.PANE_MISSING, "t1")
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1") == []


def test_an_absent_session_is_unreadable_never_exited(ingest_on: FakeEventStore) -> None:
    """B13: rows only for terminals whose tmux SESSION is listed."""
    probe = _probe([OTHER_SESSION] * (PANE_MISS_TICKS + 3), [ONE])
    for _ in range(PANE_MISS_TICKS + 3):
        probe.probe_once()
    missing = ingest_on.of_kind(EventKind.PANE_MISSING, "t1")
    assert len(missing) == 1
    assert missing[0].payload["reason"] == DegradedReason.PANE_UNREADABLE.value
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1") == []


def test_an_empty_probe_exits_nothing_and_files_one_probe_failed(
    ingest_on: FakeEventStore,
) -> None:
    probe = _probe([[]], [ONE])
    probe.probe_once()
    failed = ingest_on.of_kind(DecisionKind.PROBE_FAILED, FLEET_TERMINAL_ID)
    assert len(failed) == 1
    assert failed[0].producer is Producer.SERVER
    assert failed[0].decision is DecisionKind.PROBE_FAILED
    assert ingest_on.read("t1") == []


def test_a_raising_probe_is_the_same_verdict_as_an_empty_one(
    ingest_on: FakeEventStore,
) -> None:
    probe = _probe([None], [ONE])
    probe.probe_once()
    assert len(ingest_on.of_kind(DecisionKind.PROBE_FAILED, FLEET_TERMINAL_ID)) == 1
    assert ingest_on.read("t1") == []


def test_fleet_wide_producer_error_after_the_failure_threshold(
    ingest_on: FakeEventStore,
) -> None:
    probe = _probe([None] * PROBE_FAIL_TICKS, [ONE, TWO])
    for _ in range(PROBE_FAIL_TICKS):
        probe.probe_once()

    assert len(ingest_on.of_kind(DecisionKind.PROBE_FAILED, FLEET_TERMINAL_ID)) == PROBE_FAIL_TICKS
    for terminal in ("t1", "t2"):
        missing = ingest_on.of_kind(EventKind.PANE_MISSING, terminal)
        assert len(missing) == 1
        assert missing[0].payload["reason"] == DegradedReason.PRODUCER_ERROR.value


def test_the_producer_error_episode_opens_once_not_once_per_tick(
    ingest_on: FakeEventStore,
) -> None:
    ticks = PROBE_FAIL_TICKS + 4
    probe = _probe([None] * ticks, [ONE])
    for _ in range(ticks):
        probe.probe_once()
    assert len(ingest_on.of_kind(EventKind.PANE_MISSING, "t1")) == 1
    assert len(ingest_on.of_kind(DecisionKind.PROBE_FAILED, FLEET_TERMINAL_ID)) == ticks


def test_the_next_success_recovers_every_listed_terminal(
    ingest_on: FakeEventStore,
) -> None:
    """B16: recovery is an EDGE, which is what restores ``prior_state``."""
    probe = _probe([None] * PROBE_FAIL_TICKS + [PRESENT], [ONE, TWO])
    for _ in range(PROBE_FAIL_TICKS + 1):
        probe.probe_once()
    for terminal in ("t1", "t2"):
        recovered = ingest_on.of_kind(EventKind.PANE_RECOVERED, terminal)
        assert len(recovered) == 1
        assert recovered[0].payload["closed_producer_error_episode"] is True
        assert recovered[0].payload["recovered_from"] == DegradedReason.PRODUCER_ERROR.value


def test_a_first_sighting_is_not_a_recovery(ingest_on: FakeEventStore) -> None:
    """Nothing recovered: the terminal was never confirmed absent.

    A recovery row at startup would describe an event that did not happen, and
    AC10's content floor counts rows.
    """
    probe = _probe([PRESENT], [ONE, TWO])
    probe.probe_once()
    assert ingest_on.of_kind(EventKind.PANE_RECOVERED) == []


def test_a_returning_pane_recovers_and_can_go_missing_again(
    ingest_on: FakeEventStore,
) -> None:
    probe = _probe([PRESENT, ONLY_TWO, PRESENT, ONLY_TWO], [ONE, TWO])
    for _ in range(4):
        probe.probe_once()
    assert len(ingest_on.of_kind(EventKind.PANE_MISSING, "t1")) == 2
    assert len(ingest_on.of_kind(EventKind.PANE_RECOVERED, "t1")) == 1
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1") == []


def test_exit_reason_is_teardown_when_an_intent_is_live(
    ingest_on: FakeEventStore, clock: FakeClock
) -> None:
    """#571: a healthy teardown must be distinguishable from a crash, afterwards."""
    ingest_on.append(
        EventDraft(
            terminal_id="t1",
            kind=DecisionKind.TEARDOWN_INTENDED,
            producer=Producer.SERVER,
            confidence=Confidence.DERIVED,
            observed_at=clock.peek(),
            decision=DecisionKind.TEARDOWN_INTENDED,
            payload={"scope_kind": "terminal", "scope_key": "t1", "ttl_s": 300.0},
        )
    )
    ticks = [PRESENT] + [ONLY_TWO] * PANE_MISS_TICKS
    probe = _probe(ticks, [ONE, TWO])
    for _ in range(len(ticks)):
        probe.probe_once()
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1")[0].payload["reason"] == "teardown"


def test_exit_reason_is_crash_when_the_intent_has_expired(
    ingest_on: FakeEventStore, clock: FakeClock
) -> None:
    ingest_on.append(
        EventDraft(
            terminal_id="t1",
            kind=DecisionKind.TEARDOWN_INTENDED,
            producer=Producer.SERVER,
            confidence=Confidence.DERIVED,
            observed_at=clock.peek() - timedelta(seconds=3600),
            decision=DecisionKind.TEARDOWN_INTENDED,
            payload={"scope_kind": "terminal", "scope_key": "t1", "ttl_s": 300.0},
        )
    )
    ticks = [PRESENT] + [ONLY_TWO] * PANE_MISS_TICKS
    probe = _probe(ticks, [ONE, TWO])
    for _ in range(len(ticks)):
        probe.probe_once()
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1")[0].payload["reason"] == "crash"


def test_exit_reason_is_crash_with_no_intent_at_all(ingest_on: FakeEventStore) -> None:
    ticks = [PRESENT] + [ONLY_TWO] * PANE_MISS_TICKS
    probe = _probe(ticks, [ONE, TWO])
    for _ in range(len(ticks)):
        probe.probe_once()
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, "t1")[0].payload["reason"] == "crash"


def test_a_broken_fleet_roster_never_breaks_the_tick(ingest_on: FakeEventStore) -> None:
    def boom() -> list[_Ref]:
        raise RuntimeError("database unavailable")

    probe = LivenessProbe(list_panes=lambda: PRESENT, fleet=boom)
    probe.probe_once()  # must not raise
    assert ingest_on.rows == []


def test_the_probe_is_not_authoritative() -> None:
    """It owns ``process.exited``, but a pane listing cannot know a turn."""
    probe = LivenessProbe(list_panes=lambda: PRESENT, fleet=lambda: [ONE])
    assert probe.is_authoritative is False
    assert probe.name == "liveness_probe"


def test_it_works_without_a_state_store(store: FakeEventStore, clock: FakeClock) -> None:
    """Degrades to edges only — less information, never wrong information."""
    wiring.install_producers(wiring.ProducerRuntime(store=store, clock=clock, state_store=None))
    ticks = [PRESENT] + [ONLY_TWO] * PANE_MISS_TICKS
    probe = _probe(ticks, [ONE, TWO])
    for _ in range(len(ticks)):
        probe.probe_once()
    assert len(store.of_kind(EventKind.PROCESS_EXITED, "t1")) == 1
