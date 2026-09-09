"""The herdr runtime EventSource — unit tests (WP-HERDR H1, #702).

The §5 mapping is the load-bearing thing here, so each arrow is a test against a
REAL herdr 0.9.0 pane/agent record (``test/fixtures/herdr/pane-records.json`` —
see that dir's ``PROVENANCE.md``), not an invented shape:

* ``working`` -> ``turn.started`` (projector busy)
* ``idle``    -> ``turn.ended``   (projector idle)
* ``done``    -> ``turn.ended``   (projector idle) + unseen-activity metadata
* ``blocked`` -> nothing (§9 r2: herdr never reported it)
* ``unknown`` -> nothing
* subscription gap -> ``pane.missing`` + ``DegradedReason.NO_SIGNAL`` (§6)

The producer emits through ``wiring.emit`` exactly as the codex tailer does, so
these reuse the lane-B fakes in ``test/adapters/truth/conftest.py`` (``store``,
``ingest_on``, ``FakeClock``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from cli_agent_orchestrator.adapters.truth import herdr_runtime
from cli_agent_orchestrator.adapters.truth.herdr_runtime import HerdrRuntimeSource
from cli_agent_orchestrator.core.events import Confidence, EventKind
from cli_agent_orchestrator.core.states import DegradedReason

from .conftest import FakeEventStore

FIXTURES = Path(__file__).resolve().parents[3] / "test" / "fixtures" / "herdr"
PANE_RECORDS: dict[str, dict[str, Any]] = json.loads((FIXTURES / "pane-records.json").read_text())

#: The herdr terminal_id every fixture pane carries.
HERDR_TID = "term_65b015bb41ad32"


@pytest.fixture(autouse=True)
def _reset_herdr_sources() -> Iterator[None]:
    """The module holds per-terminal source singletons; clear them each test."""
    herdr_runtime.reset_sources()
    yield
    herdr_runtime.reset_sources()


def _pane(status: str, **overrides: Any) -> dict[str, Any]:
    """A real fixture pane record for ``status``, bound to :data:`HERDR_TID`.

    ``blocked`` has no fixture (herdr never reported it in H0), so it is built
    from the ``idle`` record with the status flipped — which is exactly the shape
    a hypothetical ``blocked`` pane would carry, and the point of the test is that
    even a well-formed ``blocked`` record produces no boundary.
    """
    if status == "blocked":
        record = dict(PANE_RECORDS["idle"])
        record["agent_status"] = "blocked"
    else:
        record = dict(PANE_RECORDS[status])
    record.setdefault("terminal_id", HERDR_TID)
    record.update(overrides)
    return record


def _source() -> HerdrRuntimeSource:
    return HerdrRuntimeSource(HERDR_TID, socket_path="/unused-in-unit-tests.sock")


# --------------------------------------------------------------------------
# EventSource surface
# --------------------------------------------------------------------------


def test_is_an_authoritative_event_source() -> None:
    source = _source()
    assert source.name == "herdr_runtime"
    assert source.is_authoritative is True


def test_off_emits_nothing(store: FakeEventStore) -> None:
    """With ingestion OFF, processing a pane record writes no row."""
    source = _source()
    source._process_pane(_pane("working"))
    assert store.rows == []


# --------------------------------------------------------------------------
# §5 state -> boundary mapping
# --------------------------------------------------------------------------


def test_working_maps_to_turn_started(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    assert ingest_on.kinds(HERDR_TID) == [EventKind.TURN_STARTED.value]


def test_idle_maps_to_turn_ended(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("idle"))
    assert ingest_on.kinds(HERDR_TID) == [EventKind.TURN_ENDED.value]


def test_done_maps_to_turn_ended_with_unseen_activity_metadata(
    ingest_on: FakeEventStore,
) -> None:
    """done is idle-plus-unseen (§5): same boundary as idle, never COMPLETED,
    with a metadata hint the projector/diag can read."""
    source = _source()
    source._process_pane(_pane("done"))
    rows = ingest_on.of_kind(EventKind.TURN_ENDED, HERDR_TID)
    assert len(rows) == 1
    assert rows[0].payload["herdr_status"] == "done"
    assert rows[0].payload["unseen_activity"] is True
    assert "done_hint" in rows[0].payload


def test_blocked_maps_to_nothing(ingest_on: FakeEventStore) -> None:
    """§9 r2: herdr reported blocked on neither provider, so it cannot be the
    source for awaiting_input; the question signal stays with question_state."""
    source = _source()
    source._process_pane(_pane("blocked"))
    assert ingest_on.rows == []


def test_unknown_maps_to_nothing(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("unknown"))
    assert ingest_on.rows == []


def test_only_the_three_mapped_statuses_produce_boundaries() -> None:
    """The mapping table is exactly working/idle/done — nothing else."""
    assert set(herdr_runtime.HERDR_STATUS_TO_EVENT) == {"working", "idle", "done"}
    assert herdr_runtime.HERDR_STATUS_TO_EVENT == {
        "working": EventKind.TURN_STARTED,
        "idle": EventKind.TURN_ENDED,
        "done": EventKind.TURN_ENDED,
    }


def test_never_emits_process_exited_or_usage_capped(ingest_on: FakeEventStore) -> None:
    """process.exited is the liveness probe's; usage.capped has no herdr signal."""
    source = _source()
    for status in ("working", "idle", "done", "blocked", "unknown"):
        source._process_pane(_pane(status))
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, HERDR_TID) == []
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED, HERDR_TID) == []


# --------------------------------------------------------------------------
# edge-triggering
# --------------------------------------------------------------------------


def test_a_repeated_status_is_not_a_new_boundary(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    source._process_pane(_pane("working"))
    source._process_pane(_pane("working"))
    assert ingest_on.kinds(HERDR_TID) == [EventKind.TURN_STARTED.value]


def test_a_real_transition_is_an_edge(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    source._process_pane(_pane("idle"))
    source._process_pane(_pane("working"))
    assert ingest_on.kinds(HERDR_TID) == [
        EventKind.TURN_STARTED.value,
        EventKind.TURN_ENDED.value,
        EventKind.TURN_STARTED.value,
    ]


def test_blocked_between_two_states_still_lets_the_next_real_edge_fire(
    ingest_on: FakeEventStore,
) -> None:
    """A no-mapping status updates the edge baseline but emits nothing, so the
    transition AROUND it is still observed."""
    source = _source()
    source._process_pane(_pane("working"))
    source._process_pane(_pane("blocked"))  # no row, but baseline is now 'blocked'
    source._process_pane(_pane("idle"))
    assert ingest_on.kinds(HERDR_TID) == [
        EventKind.TURN_STARTED.value,
        EventKind.TURN_ENDED.value,
    ]


# --------------------------------------------------------------------------
# terminal binding
# --------------------------------------------------------------------------


def test_a_pane_for_another_terminal_is_ignored(ingest_on: FakeEventStore) -> None:
    source = _source()
    other = _pane("working", terminal_id="term_someone_else")
    event = {"event": "pane_updated", "data": {"pane": other}}
    source._handle_event(event)
    assert ingest_on.rows == []


def test_handle_event_routes_a_broadcast_pane_updated(ingest_on: FakeEventStore) -> None:
    source = _source()
    event = {"event": "pane_updated", "data": {"pane": _pane("working")}}
    source._handle_event(event)
    assert ingest_on.kinds(HERDR_TID) == [EventKind.TURN_STARTED.value]


def test_handle_event_ignores_non_pane_updated(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._handle_event({"event": "pane_agent_detected", "data": {"agent": "pi"}})
    source._handle_event({"event": "workspace_closed", "data": {"workspace_id": "w2"}})
    assert ingest_on.rows == []


# --------------------------------------------------------------------------
# confidence: hook-backed vs screen-manifest
# --------------------------------------------------------------------------


def test_hook_backed_pane_is_authoritative(ingest_on: FakeEventStore) -> None:
    """A pane herdr resolved via a lifecycle hook (screen_detection_skipped=true)
    is authoritative — the pi case in H0."""
    source = _source()
    source._process_pane(_pane("working"))  # fixture working record has sds=true
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, HERDR_TID)
    assert rows[0].confidence is Confidence.AUTHORITATIVE


def test_screen_manifest_pane_is_derived(ingest_on: FakeEventStore) -> None:
    """Absent the hook signal, confidence is derived — a producer claims no
    authority it cannot demonstrate."""
    source = _source()
    pane = _pane("working")
    pane.pop("screen_detection_skipped", None)  # a screen-manifest cohort
    source._process_pane(pane)
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, HERDR_TID)
    assert rows[0].confidence is Confidence.DERIVED


# --------------------------------------------------------------------------
# identity (§9): keyed on agent_session, never terminal_id
# --------------------------------------------------------------------------


def test_event_source_ref_carries_the_stable_agent_session(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, HERDR_TID)
    ref = rows[0].source_ref
    # The fixture's agent_session is source=herdr:pi, value=<jsonl path>.
    assert ref is not None
    assert ref.startswith("herdr:pi:")
    assert ".jsonl" in ref


# --------------------------------------------------------------------------
# subscription gap (§6)
# --------------------------------------------------------------------------


def test_gap_emits_pane_missing_with_no_signal(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))  # track a pane first
    ingest_on.rows.clear()
    source._emit_gap_degraded()
    rows = ingest_on.of_kind(EventKind.PANE_MISSING, HERDR_TID)
    assert len(rows) == 1
    assert rows[0].payload["reason"] == DegradedReason.NO_SIGNAL.value
    assert rows[0].payload["cause"] == "herdr_subscription_gap"
    assert "w2:p1" in rows[0].payload["tracked_panes"]


def test_gap_before_any_event_still_degrades_the_terminal(ingest_on: FakeEventStore) -> None:
    """A gap with nothing tracked yet still degrades this terminal, so a
    certified cohort vetoes delivery during the gap."""
    source = _source()
    source._emit_gap_degraded()
    rows = ingest_on.of_kind(EventKind.PANE_MISSING, HERDR_TID)
    assert len(rows) == 1
    assert rows[0].payload["reason"] == DegradedReason.NO_SIGNAL.value
    assert rows[0].payload["tracked_panes"] == []


def test_gap_clears_edge_baseline_so_resnapshot_re_emits(ingest_on: FakeEventStore) -> None:
    """After a gap the remembered statuses are stale; the resnapshot's current
    status must re-fire as an edge rather than be suppressed as a repeat."""
    source = _source()
    source._process_pane(_pane("working"))
    source._emit_gap_degraded()
    ingest_on.rows.clear()
    # Reconnect resnapshot shows 'working' again — a NEW edge after the gap.
    source._process_pane(_pane("working"))
    assert ingest_on.kinds(HERDR_TID) == [EventKind.TURN_STARTED.value]


# --------------------------------------------------------------------------
# attach / detach registry
# --------------------------------------------------------------------------


def test_attach_is_off_when_ingestion_is_off() -> None:
    assert herdr_runtime.attach("t1") is None
    assert herdr_runtime.source_for("t1") is None


def test_attach_is_idempotent_per_terminal(ingest_on: FakeEventStore) -> None:
    first = herdr_runtime.attach(HERDR_TID, socket_path="/unused.sock")
    second = herdr_runtime.attach(HERDR_TID, socket_path="/unused.sock")
    assert first is not None
    assert first is second


def test_detach_drops_the_source(ingest_on: FakeEventStore) -> None:
    herdr_runtime.attach(HERDR_TID, socket_path="/unused.sock")
    herdr_runtime.detach(HERDR_TID)
    assert herdr_runtime.source_for(HERDR_TID) is None


# --------------------------------------------------------------------------
# end-to-end over an injected fake client: snapshot -> stream -> gap
# --------------------------------------------------------------------------


class _FakeClient:
    """A minimal HerdrClient stand-in that replays a scripted snapshot + events.

    Drives ``_connect_and_stream`` without a socket: ``snapshot`` returns the
    seeded panes, ``events`` yields the seeded events and then raises
    :class:`HerdrTransportError` to simulate the socket dropping — the §6 gap.
    """

    def __init__(self, panes: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        self._panes = panes
        self._events = events
        self.subscribed = False

    async def connect(self) -> None:  # noqa: D401 - stub
        return None

    async def check_protocol(self) -> dict[str, Any]:
        return {"protocol": 22, "schema_version": 1}

    async def subscribe(self, subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        self.subscribed = True
        return {"type": "subscription_started"}

    async def snapshot(self) -> dict[str, Any]:
        return {"panes": self._panes}

    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event
        from cli_agent_orchestrator.adapters.herdr.client import HerdrTransportError

        raise HerdrTransportError("socket closed")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_connect_and_stream_applies_snapshot_then_events_then_gap(
    ingest_on: FakeEventStore,
) -> None:
    """One connection's life: snapshot seeds working, an event moves it to idle,
    then the socket drops and the source emits the degraded gap."""
    fake = _FakeClient(
        panes=[_pane("working")],
        events=[{"event": "pane_updated", "data": {"pane": _pane("idle")}}],
    )
    source = HerdrRuntimeSource(HERDR_TID, socket_path="/unused.sock", client=fake)
    with pytest.raises(Exception):
        await source._connect_and_stream()
    assert fake.subscribed is True
    # snapshot(working) -> turn.started, event(idle) -> turn.ended.
    kinds = ingest_on.kinds(HERDR_TID)
    assert kinds == [EventKind.TURN_STARTED.value, EventKind.TURN_ENDED.value]


@pytest.mark.asyncio
async def test_run_loop_emits_gap_degraded_on_stream_drop(ingest_on: FakeEventStore) -> None:
    """The run loop turns a dropped stream into the §6 degraded signal, then
    would back off; we stop it after the first cycle."""
    fake = _FakeClient(panes=[_pane("working")], events=[])
    source = HerdrRuntimeSource(
        HERDR_TID,
        socket_path="/unused.sock",
        client=fake,
        reconnect_backoff_base_s=0.01,
        reconnect_backoff_max_s=0.01,
    )

    # Run one cycle by hand: connect_and_stream raises (gap), the loop's handler
    # emits the degraded event.
    try:
        await source._connect_and_stream()
    except Exception:
        source._emit_gap_degraded()

    missing = ingest_on.of_kind(EventKind.PANE_MISSING, HERDR_TID)
    assert missing, "a dropped stream degrades the source"
    assert missing[0].payload["reason"] == DegradedReason.NO_SIGNAL.value
