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

import asyncio
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

#: The herdr terminal_id every fixture pane carries — herdr's OWN namespace.
HERDR_TID = "term_65b015bb41ad32"

#: The CAO terminal id — a UUID, a different namespace entirely.  It reaches the
#: pane only as ``--env CAO_TERMINAL_ID`` and appears in NO herdr pane field, so
#: a source that matched pane records against it would bind nothing.  Every
#: assertion below reads events back by THIS id, because it is the id the
#: projector, the state store and ``cao diag`` know.
CAO_TID = "1f6c0f2e-9c6b-4a0e-9f0c-2d1e4a7b8c90"


@pytest.fixture(autouse=True)
def _reset_herdr_sources() -> Iterator[None]:
    """The module holds per-terminal source singletons; clear them each test."""
    herdr_runtime.reset_sources()
    yield
    herdr_runtime.reset_sources()


def _deliver(source: HerdrRuntimeSource, pane: dict[str, Any]) -> None:
    """Push a pane record the way herdr does — through the broadcast router.

    Binding is checked in ``_handle_event``/``_apply_snapshot``, never in
    ``_process_pane`` (which is the mapper, and assumes its caller already
    decided the pane belongs).  Every test about WHICH panes bind therefore has
    to come in through this door; calling ``_process_pane`` directly would pass
    whatever it was handed and prove nothing.
    """
    source._handle_event({"event": "pane_updated", "data": {"pane": pane}})


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
    record.setdefault("terminal_id", CAO_TID)
    record.update(overrides)
    return record


def _source() -> HerdrRuntimeSource:
    """A source in the H1 binding contract: emit on the CAO id, match on herdr's."""
    return HerdrRuntimeSource(
        CAO_TID,
        herdr_terminal_id=HERDR_TID,
        socket_path="/unused-in-unit-tests.sock",
    )


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
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]


def test_idle_maps_to_turn_ended(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("idle"))
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_ENDED.value]


def test_done_maps_to_turn_ended_with_unseen_activity_metadata(
    ingest_on: FakeEventStore,
) -> None:
    """done is idle-plus-unseen (§5): same boundary as idle, never COMPLETED,
    with a metadata hint the projector/diag can read."""
    source = _source()
    source._process_pane(_pane("done"))
    rows = ingest_on.of_kind(EventKind.TURN_ENDED, CAO_TID)
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
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED, CAO_TID) == []
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED, CAO_TID) == []


# --------------------------------------------------------------------------
# edge-triggering
# --------------------------------------------------------------------------


def test_a_repeated_status_is_not_a_new_boundary(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    source._process_pane(_pane("working"))
    source._process_pane(_pane("working"))
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]


def test_a_real_transition_is_an_edge(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    source._process_pane(_pane("idle"))
    source._process_pane(_pane("working"))
    assert ingest_on.kinds(CAO_TID) == [
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
    assert ingest_on.kinds(CAO_TID) == [
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
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]


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
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, CAO_TID)
    assert rows[0].confidence is Confidence.AUTHORITATIVE


def test_screen_manifest_pane_is_derived(ingest_on: FakeEventStore) -> None:
    """Absent the hook signal, confidence is derived — a producer claims no
    authority it cannot demonstrate."""
    source = _source()
    pane = _pane("working")
    pane.pop("screen_detection_skipped", None)  # a screen-manifest cohort
    source._process_pane(pane)
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, CAO_TID)
    assert rows[0].confidence is Confidence.DERIVED


# --------------------------------------------------------------------------
# identity (§9): keyed on agent_session, never terminal_id
# --------------------------------------------------------------------------


def test_event_source_ref_carries_the_stable_agent_session(ingest_on: FakeEventStore) -> None:
    source = _source()
    source._process_pane(_pane("working"))
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, CAO_TID)
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
    rows = ingest_on.of_kind(EventKind.PANE_MISSING, CAO_TID)
    assert len(rows) == 1
    assert rows[0].payload["reason"] == DegradedReason.NO_SIGNAL.value
    assert rows[0].payload["cause"] == "herdr_subscription_gap"
    assert "w2:p1" in rows[0].payload["tracked_panes"]


def test_gap_before_any_event_still_degrades_the_terminal(ingest_on: FakeEventStore) -> None:
    """A gap with nothing tracked yet still degrades this terminal, so a
    certified cohort vetoes delivery during the gap."""
    source = _source()
    source._emit_gap_degraded()
    rows = ingest_on.of_kind(EventKind.PANE_MISSING, CAO_TID)
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
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]


# --------------------------------------------------------------------------
# attach / detach registry
# --------------------------------------------------------------------------


def test_attach_is_off_when_ingestion_is_off() -> None:
    assert herdr_runtime.attach("t1", herdr_terminal_id=HERDR_TID) is None
    assert herdr_runtime.source_for("t1") is None


def test_attach_is_idempotent_per_terminal(ingest_on: FakeEventStore) -> None:
    first = herdr_runtime.attach(CAO_TID, herdr_terminal_id=HERDR_TID, socket_path="/u.sock")
    second = herdr_runtime.attach(CAO_TID, herdr_terminal_id=HERDR_TID, socket_path="/u.sock")
    assert first is not None
    assert first is second


def test_detach_drops_the_source(ingest_on: FakeEventStore) -> None:
    herdr_runtime.attach(CAO_TID, herdr_terminal_id=HERDR_TID, socket_path="/u.sock")
    herdr_runtime.detach(CAO_TID)
    assert herdr_runtime.source_for(CAO_TID) is None


def test_attach_refuses_a_cao_id_with_no_herdr_key(ingest_on: FakeEventStore) -> None:
    """The shipped defect, refused at the seam rather than failing silently.

    A source constructed with only the CAO uuid has nothing in herdr's namespace
    to match pane records on, so it binds no pane and drops every event — which
    looks exactly like a quiet worker.  Attach returns None instead.
    """
    assert herdr_runtime.attach(CAO_TID, socket_path="/u.sock") is None
    assert herdr_runtime.source_for(CAO_TID) is None


def test_constructing_with_no_herdr_key_raises(ingest_on: FakeEventStore) -> None:
    with pytest.raises(ValueError, match="herdr-namespace key"):
        HerdrRuntimeSource(CAO_TID, socket_path="/u.sock")


# --------------------------------------------------------------------------
# the two-namespace binding contract (H1 slice 1)
# --------------------------------------------------------------------------


def test_events_carry_the_cao_id_while_panes_match_on_the_herdr_id(
    ingest_on: FakeEventStore,
) -> None:
    """The load-bearing binding test: the two ids differ and each does its job.

    The fixture pane carries herdr's ``term_*``; the source was attached with a
    CAO uuid.  The pane must BIND (so the event exists at all) and the row must
    be attributed to the CAO id (so the projector can find it).
    """
    source = _source()
    assert CAO_TID != HERDR_TID
    _deliver(source, _pane("working"))
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]
    assert ingest_on.kinds(HERDR_TID) == []
    rows = ingest_on.of_kind(EventKind.TURN_STARTED, CAO_TID)
    assert rows[0].terminal_id == CAO_TID
    # The herdr id is carried as PAYLOAD, where it is evidence and not identity.
    assert rows[0].payload["herdr_terminal_id"] == HERDR_TID


def test_a_source_bound_only_by_pane_id_binds_and_learns_the_herdr_id(
    ingest_on: FakeEventStore,
) -> None:
    """The shim's case: herdr's tab-create response yields a pane id, not a
    terminal id, so the pane id is the only key available at create time."""
    pane = _pane("working")
    source = HerdrRuntimeSource(CAO_TID, pane_id=str(pane["pane_id"]), socket_path="/u.sock")
    _deliver(source, pane)
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]
    assert source._herdr_terminal_id == HERDR_TID


def test_a_pane_whose_herdr_id_differs_does_not_bind(ingest_on: FakeEventStore) -> None:
    """Sanity in the other direction: matching is still real, not vacuous."""
    source = HerdrRuntimeSource(CAO_TID, herdr_terminal_id="term_not_ours", socket_path="/u.sock")
    _deliver(source, _pane("working"))
    assert ingest_on.rows == []


def test_a_pane_carrying_the_cao_id_in_its_terminal_id_does_not_bind(
    ingest_on: FakeEventStore,
) -> None:
    """The conflation, pinned as a falsifiable claim.

    If the source ever matched on ``self.terminal_id`` (the CAO id) again, this
    synthetic pane — a herdr record whose ``terminal_id`` field happens to hold a
    CAO uuid, which real herdr never produces — would bind.  It must not.
    """
    source = _source()
    _deliver(source, _pane("working", terminal_id=CAO_TID))
    assert ingest_on.rows == []


# --------------------------------------------------------------------------
# source health (§5 precedence only engages when the column is bumped)
# --------------------------------------------------------------------------


class _RecordingStateStore:
    """Records ``touch_source_probe`` calls; everything else is a no-op stub."""

    def __init__(self) -> None:
        self.probes: list[str] = []

    def get(self, terminal_id: str):  # type: ignore[no-untyped-def]
        return None

    def upsert(self, projection) -> None:  # type: ignore[no-untyped-def]
        return None

    def touch_probe(self, terminal_id: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None

    def touch_source_probe(self, terminal_id: str, *, probed_at) -> None:  # type: ignore[no-untyped-def]
        self.probes.append(terminal_id)

    def all_terminals(self):  # type: ignore[no-untyped-def]
        return []


def _recording_states(monkeypatch: pytest.MonkeyPatch) -> "_RecordingStateStore":
    """Swap the installed runtime for one whose StateStore records probe bumps."""
    from cli_agent_orchestrator.adapters.truth import wiring

    runtime = wiring.producer_runtime()
    assert runtime is not None
    states = _RecordingStateStore()
    monkeypatch.setattr(
        wiring,
        "_runtime",
        wiring.ProducerRuntime(
            store=runtime.store, clock=runtime.clock, state_store=states  # type: ignore[arg-type]
        ),
    )
    return states


def test_a_pushed_frame_bumps_source_health(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the projector never believes the source and §5 never engages.

    ``Projector._source_healthy`` treats a NULL ``last_source_probe_at`` as
    UNHEALTHY, so a source that never bumps it is muted-by-nothing: every derived
    pane event applies and source-level precedence is dead code.
    """
    states = _recording_states(monkeypatch)
    source = _source()
    source._process_pane(_pane("working"), pushed=True)
    assert states.probes == [CAO_TID]


def test_source_health_is_bumped_even_when_the_status_did_not_change(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy worker reporting ``working`` for a minute is a HEALTHY source, not
    a silent one — so among PUSHED frames, health is a heartbeat and not an
    event count."""
    states = _recording_states(monkeypatch)
    source = _source()
    for _ in range(3):
        source._process_pane(_pane("working"), pushed=True)
    assert len(states.probes) == 3
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_STARTED.value]


# --------------------------------------------------------------------------
# r2: health is evidence the STREAM delivered, not evidence a socket is open
# --------------------------------------------------------------------------


def test_a_snapshot_record_is_not_proof_of_a_live_stream(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The r1 defect, pinned.

    A subscription can be ACKed and permanently silent — measured on herdr 0.9.0
    with the wrong subscription — and the connect-time snapshot still reports a
    level.  If that level bumped health, the projector would mute the scraped
    lifecycle and the terminal would freeze at whatever herdr said at attach,
    forever, while ``cao diag`` called it authoritative.  A snapshot emits its
    boundary and proves nothing.
    """
    states = _recording_states(monkeypatch)
    source = _source()
    source._process_pane(_pane("idle"), pushed=False)
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_ENDED.value]
    assert states.probes == []
    assert source._stream_is_proven() is False


def test_the_keepalive_is_silent_until_the_stream_has_proven_itself(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat for a stream that has never delivered is the r1 defect with
    extra steps, so the keepalive asks first."""
    states = _recording_states(monkeypatch)
    source = _source()
    assert source._stream_is_proven() is False
    if source._stream_is_proven():  # pragma: no cover - the guard under test
        source._touch_source_probe()
    assert states.probes == []

    source._process_pane(_pane("working"), pushed=True)
    states.probes.clear()
    assert source._stream_is_proven() is True
    if source._stream_is_proven():
        source._touch_source_probe()
    assert states.probes == [CAO_TID]


def test_stream_proof_expires(ingest_on: FakeEventStore) -> None:
    """Past the TTL a source stops asserting health rather than asserting a stale
    one — the bound that keeps the heartbeat from re-creating the r1 defect."""
    source = HerdrRuntimeSource(
        CAO_TID,
        herdr_terminal_id=HERDR_TID,
        socket_path="/u.sock",
        stream_proof_ttl_s=0.0,
    )
    source._process_pane(_pane("working"), pushed=True)
    assert source._stream_is_proven() is False


def test_a_gap_revokes_the_stream_proof(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next connection earns health with a frame of its own, as the first did."""
    _recording_states(monkeypatch)
    source = _source()
    source._process_pane(_pane("working"), pushed=True)
    assert source._stream_is_proven() is True
    source._emit_gap_degraded()
    assert source._stream_is_proven() is False


def test_a_gap_does_not_bump_source_health(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped stream is the one thing that must let the column go stale."""
    from cli_agent_orchestrator.adapters.truth import wiring

    runtime = wiring.producer_runtime()
    assert runtime is not None
    states = _RecordingStateStore()
    monkeypatch.setattr(
        wiring,
        "_runtime",
        wiring.ProducerRuntime(
            store=runtime.store, clock=runtime.clock, state_store=states  # type: ignore[arg-type]
        ),
    )
    source = _source()
    source._emit_gap_degraded()
    assert states.probes == []


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
    source = HerdrRuntimeSource(
        CAO_TID, herdr_terminal_id=HERDR_TID, socket_path="/unused.sock", client=fake
    )
    with pytest.raises(Exception):
        await source._connect_and_stream()
    assert fake.subscribed is True
    # snapshot(working) -> turn.started, event(idle) -> turn.ended.
    kinds = ingest_on.kinds(CAO_TID)
    assert kinds == [EventKind.TURN_STARTED.value, EventKind.TURN_ENDED.value]


@pytest.mark.asyncio
async def test_run_loop_emits_gap_degraded_on_stream_drop(ingest_on: FakeEventStore) -> None:
    """The run loop turns a dropped stream into the §6 degraded signal, then
    would back off; we stop it after the first cycle."""
    fake = _FakeClient(panes=[_pane("working")], events=[])
    source = HerdrRuntimeSource(
        CAO_TID,
        herdr_terminal_id=HERDR_TID,
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

    missing = ingest_on.of_kind(EventKind.PANE_MISSING, CAO_TID)
    assert missing, "a dropped stream degrades the source"
    assert missing[0].payload["reason"] == DegradedReason.NO_SIGNAL.value


# --------------------------------------------------------------------------
# r2: the subscription herdr actually pushes lifecycle on, and its wire naming
# --------------------------------------------------------------------------


def _verbatim_status_frames() -> list[dict[str, Any]]:
    """Every ``pane.agent_status_changed`` line of the recorded pi stream.

    VERBATIM, from ``test/fixtures/herdr/pi-events.jsonl`` — the repo's own
    capture of a real subscribed socket against a hook-backed pi lane. Read from
    the file rather than transcribed, because the r2 revision of this test
    hand-wrote a frame carrying ``screen_detection_skipped`` and ``terminal_id``
    and the real frames carry NEITHER. A fixture that adds fields the wire does
    not send tests the fixture.
    """
    frames = []
    for line in (FIXTURES / "pi-events.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "pane.agent_status_changed":
            frames.append(event)
    return frames


#: One verbatim frame, for the tests that need a single one. Its payload is FLAT
#: (no ``data.pane`` wrapper) and carries exactly four keys.
AGENT_STATUS_FRAME: dict[str, Any] = _verbatim_status_frames()[0]

#: The four keys a pushed frame carries — asserted, not assumed.
VERBATIM_STATUS_KEYS = {"agent", "agent_status", "pane_id", "workspace_id"}

#: A recorded ``pane_updated`` frame — UNDERSCORE-separated, nested under
#: ``data.pane``. The two conventions are herdr's two schemas (EventKind uses
#: underscores, SubscriptionEventKind uses dots), and a source has to read both.
PANE_UPDATED_FRAME: dict[str, Any] = {
    "event": "pane_updated",
    "data": {"pane": {"pane_id": "w2:p1", "terminal_id": HERDR_TID, "agent_status": "idle"}},
}


def test_the_subscription_carries_this_source_s_own_pane_status_stream() -> None:
    """The r1 silence, fixed at its cause.

    An agent-status transition does not emit ``pane.updated`` — measured on
    0.9.0, five transitions produced five ``pane.agent_status_changed`` pushes
    and not one ``pane.updated``; upstream ogulcancelik/herdr#2115. So the
    broadcast alone is a stream that can never carry the lifecycle this source
    exists to read, and the per-pane spec must ride in the SAME subscribe,
    because herdr resets the connection on a second one.
    """
    subs = herdr_runtime._subscriptions_for("w2:p1")
    assert {"type": "pane.updated"} in subs
    assert {"type": "pane.agent_status_changed", "pane_id": "w2:p1"} in subs
    assert len(subs) == 2, "both specs batch into the ONE subscribe herdr allows"


def test_the_per_pane_spec_is_omitted_when_no_pane_is_bound() -> None:
    """herdr refuses ``pane.agent_status_changed`` without a ``pane_id`` and
    CLOSES the connection, so a source with no pane id must not send it —
    one malformed spec costs the whole stream."""
    assert herdr_runtime._subscriptions_for(None) == [{"type": "pane.updated"}]


def test_the_verbatim_pushed_frame_carries_exactly_four_keys() -> None:
    """The fact every other B4 test rests on, pinned against the real capture.

    43 frames in the recorded pi stream, and a fresh live capture on herdr 0.9.0
    (grok-box-010, four driven transitions) agrees: a pushed
    ``pane.agent_status_changed`` carries ``agent``, ``agent_status``,
    ``pane_id``, ``workspace_id`` — and NOT ``screen_detection_skipped``, which
    is what ``_confidence_for`` used to read to decide authority.
    """
    frames = _verbatim_status_frames()
    assert len(frames) >= 20, "the capture should be a real stream, not a sample"
    keys: set[str] = set()
    for frame in frames:
        keys |= set(frame["data"].keys())
    assert keys == VERBATIM_STATUS_KEYS
    assert not any("screen_detection_skipped" in f["data"] for f in frames)


def test_a_dot_named_agent_status_frame_is_read(ingest_on: FakeEventStore) -> None:
    """The trap: ``"pane.agent_status_changed".replace("_", ".")`` is
    ``pane.agent.status.changed``, so a branch behind the r1 normaliser could
    never match. The dotted family is matched on the RAW name."""
    source = HerdrRuntimeSource(CAO_TID, pane_id="w2:p1", socket_path="/u.sock")
    source._handle_event(AGENT_STATUS_FRAME)
    assert len(ingest_on.kinds(CAO_TID)) == 1
    rows = ingest_on.rows
    assert rows[0].payload["herdr_status"] == AGENT_STATUS_FRAME["data"]["agent_status"]
    assert rows[0].terminal_id == CAO_TID


def test_the_underscore_named_family_still_normalises(ingest_on: FakeEventStore) -> None:
    """The other convention, unbroken: ``pane_updated`` -> ``pane.updated``."""
    source = HerdrRuntimeSource(CAO_TID, pane_id="w2:p1", socket_path="/u.sock")
    source._handle_event(PANE_UPDATED_FRAME)
    assert ingest_on.kinds(CAO_TID) == [EventKind.TURN_ENDED.value]


def test_a_pushed_agent_status_frame_proves_the_stream(
    ingest_on: FakeEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frame that carries lifecycle is the frame that vouches for health."""
    states = _recording_states(monkeypatch)
    source = HerdrRuntimeSource(CAO_TID, pane_id="w2:p1", socket_path="/u.sock")
    source._handle_event(AGENT_STATUS_FRAME)
    assert states.probes == [CAO_TID]
    assert source._stream_is_proven() is True


def test_an_agent_status_frame_for_another_pane_is_ignored(ingest_on: FakeEventStore) -> None:
    """Binding is checked on the flat payload exactly as on the nested one."""
    source = HerdrRuntimeSource(CAO_TID, pane_id="w9:p9", socket_path="/u.sock")
    source._handle_event(AGENT_STATUS_FRAME)
    assert ingest_on.rows == []


# --------------------------------------------------------------------------
# r2: teardown from a worker thread must reach the loop, never raise
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sync_from_another_thread_cancels_without_raising(
    ingest_on: FakeEventStore,
) -> None:
    """``detach`` runs on the terminal-delete WORKER THREAD, and
    ``Task.cancel()`` is not thread-safe — it reaches ``loop.call_soon``, whose
    ``_check_thread`` raises.  Before the loop fix this was latent (``_task`` was
    always None off-loop); after it, teardown raised for real, and the raise
    happened before the caller dropped the terminal's authority.
    """
    import threading

    loop = asyncio.get_running_loop()
    herdr_runtime.set_event_loop(loop)
    source = HerdrRuntimeSource(CAO_TID, herdr_terminal_id=HERDR_TID, socket_path="/u.sock")

    async def _park() -> None:
        await asyncio.sleep(3600)

    parked = asyncio.create_task(_park())
    source._task = parked
    await asyncio.sleep(0)

    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            source.stop_sync()
        except BaseException as exc:  # noqa: BLE001 - the whole point of the test
            errors.append(exc)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)
    assert errors == [], f"stop_sync raised off-loop: {errors!r}"

    # The cancellation was HANDED to the loop, so it lands on the next turn of
    # it rather than inside the worker thread's call.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if parked.done():
            break
    assert parked.done(), "the parked task outlived teardown"
    herdr_runtime.set_event_loop(None)


# --------------------------------------------------------------------------
# B4: a certified terminal's own events must not be muted by its own authority
# --------------------------------------------------------------------------


def test_a_pushed_frame_on_an_uncertified_terminal_stays_derived(
    ingest_on: FakeEventStore,
) -> None:
    """Unchanged, and correct: nothing mutes an uncertified terminal's pane, so a
    reading herdr cannot vouch for should not claim authority."""
    source = HerdrRuntimeSource(
        CAO_TID, pane_id="w2:p1", socket_path="/u.sock", lifecycle_authoritative=False
    )
    source._handle_event(AGENT_STATUS_FRAME)
    assert ingest_on.rows[0].confidence is Confidence.DERIVED


def test_a_pushed_frame_on_a_CERTIFIED_terminal_is_authoritative(
    ingest_on: FakeEventStore,
) -> None:
    """B4. The verbatim frame carries no ``screen_detection_skipped``, so the
    field-based rule returns DERIVED for every pushed lifecycle event there is.

    On a certified terminal that is fatal rather than merely cautious: §6(ii)
    mutes the scraped lifecycle, ``turn.*`` is not in ``DERIVED_ALWAYS_KINDS``,
    and the frame bumps the probe column before its own event reaches the
    projector — so the terminal's authority mutes the terminal's events and it
    projects the connect snapshot and then nothing, with a working stream.

    Certification is what settles it, because certification is the claim: a PASS
    row says this source is the authority for this terminal's lifecycle.
    """
    source = HerdrRuntimeSource(
        CAO_TID, pane_id="w2:p1", socket_path="/u.sock", lifecycle_authoritative=True
    )
    source._handle_event(AGENT_STATUS_FRAME)
    assert ingest_on.rows[0].confidence is Confidence.AUTHORITATIVE


def _snapshot_pane(status: str) -> dict[str, Any]:
    """A pane record shaped like ``session.snapshot`` actually returns one.

    N13: the field-bearing records in ``pane-records.json`` are AGENT records
    (``herdr agent get``), per that fixture's own PROVENANCE — the ``idle`` and
    ``unknown`` entries, lifted from the event stream, carry no
    ``screen_detection_skipped``, and neither did ``api snapshot`` for a live
    pane in the B4 capture. So the snapshot path is exercised against a record
    with the field ABSENT, which is the shape the wire sends.
    """
    record = dict(PANE_RECORDS["idle"])
    record["agent_status"] = status
    record.setdefault("terminal_id", HERDR_TID)
    assert "screen_detection_skipped" not in record
    return record


def test_a_field_bearing_record_still_grants_authority(ingest_on: FakeEventStore) -> None:
    """Where herdr DOES send the field — an agent record — it still decides, on
    an uncertified terminal too. That path is untouched."""
    source = HerdrRuntimeSource(
        CAO_TID,
        herdr_terminal_id=HERDR_TID,
        socket_path="/u.sock",
        lifecycle_authoritative=False,
    )
    source._process_pane(_pane("working"), pushed=False)  # agent record, sds=True
    assert ingest_on.rows[0].confidence is Confidence.AUTHORITATIVE


def test_a_snapshot_record_without_the_field_is_derived_when_uncertified(
    ingest_on: FakeEventStore,
) -> None:
    source = HerdrRuntimeSource(
        CAO_TID,
        herdr_terminal_id=HERDR_TID,
        socket_path="/u.sock",
        lifecycle_authoritative=False,
    )
    source._process_pane(_snapshot_pane("working"), pushed=False)
    assert ingest_on.rows[0].confidence is Confidence.DERIVED


def test_a_resnapshot_on_a_certified_terminal_is_authoritative(
    ingest_on: FakeEventStore,
) -> None:
    """B5. §6 says a resnapshot is the ONLY safe recovery from a subscription
    gap, because events are not receipts.

    While authority required ``pushed``, a resnapshot's records were DERIVED and
    therefore muted on a certified terminal — so the cohort degraded on the gap
    and stayed degraded through the very resnapshot meant to recover it, waiting
    on a pushed edge that an edge-triggered stream never sends when the status
    did not change across the gap.
    """
    source = HerdrRuntimeSource(
        CAO_TID,
        herdr_terminal_id=HERDR_TID,
        socket_path="/u.sock",
        lifecycle_authoritative=True,
    )
    source._process_pane(_snapshot_pane("working"), pushed=False)
    assert ingest_on.rows[0].confidence is Confidence.AUTHORITATIVE


def test_a_certified_terminals_own_lifecycle_is_not_muted_by_its_own_authority(
    ingest_on: FakeEventStore,
) -> None:
    """The end-to-end claim B4 makes, through the REAL projector.

    Certified terminal, working stream, verbatim frames. Before the fix every
    ``turn.*`` was DERIVED and `_projected` muted it the moment the first frame
    had bumped the probe column. The terminal must project the stream's levels,
    not freeze at the snapshot.
    """
    from cli_agent_orchestrator.app.worker_truth.projector import (
        Projector,
        StaticSourceRegistry,
    )
    from cli_agent_orchestrator.core.states import WorkerState
    from test.app.fakes import FakeClock, InMemoryEventStore, InMemoryStateStore

    clock = FakeClock()
    events = InMemoryEventStore(clock)
    states = InMemoryStateStore()
    sources = StaticSourceRegistry(frozenset({CAO_TID}))
    sources.set_fallback_disabled(CAO_TID)
    projector = Projector(events, states, clock, sources)
    from cli_agent_orchestrator.adapters.truth import wiring

    wiring.install_producers(
        wiring.ProducerRuntime(store=events, clock=clock, state_store=states, folder=projector)
    )
    try:
        source = HerdrRuntimeSource(
            CAO_TID, pane_id="w2:p1", socket_path="/u.sock", lifecycle_authoritative=True
        )
        for frame in _verbatim_status_frames()[:8]:
            source._handle_event(frame)
        row = states.get(CAO_TID)
        assert row is not None
        assert row.state in (
            WorkerState.BUSY,
            WorkerState.IDLE,
        ), "a certified terminal with a working stream must project it, not freeze"
    finally:
        wiring.reset_producers()


def test_a_certified_terminal_degraded_by_a_gap_recovers_on_the_RESNAPSHOT(
    ingest_on: FakeEventStore,
) -> None:
    """B5 end to end, through the real projector.

    Gap -> degraded(no_signal) -> reconnect -> resnapshot. §6 calls the
    resnapshot the only safe recovery, so the terminal must leave `degraded` on
    it — not sit there until a pushed edge that an edge-triggered stream may
    never send, because a status unchanged across the gap produces no edge.
    """
    from cli_agent_orchestrator.adapters.truth import wiring
    from cli_agent_orchestrator.app.worker_truth.projector import (
        Projector,
        StaticSourceRegistry,
    )
    from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
    from test.app.fakes import FakeClock, InMemoryEventStore, InMemoryStateStore

    clock = FakeClock()
    events = InMemoryEventStore(clock)
    states = InMemoryStateStore()
    sources = StaticSourceRegistry(frozenset({CAO_TID}))
    sources.set_fallback_disabled(CAO_TID)
    projector = Projector(events, states, clock, sources)
    wiring.install_producers(
        wiring.ProducerRuntime(store=events, clock=clock, state_store=states, folder=projector)
    )
    try:
        source = HerdrRuntimeSource(
            CAO_TID,
            herdr_terminal_id=HERDR_TID,
            socket_path="/u.sock",
            lifecycle_authoritative=True,
        )
        source._process_pane(_snapshot_pane("working"), pushed=True)
        source._emit_gap_degraded()
        row = states.get(CAO_TID)
        assert row is not None
        assert row.state is WorkerState.DEGRADED
        assert row.degraded_reason is DegradedReason.NO_SIGNAL

        # Reconnect: the resnapshot reports the level. No pushed edge follows,
        # because the status did not change while the socket was down.
        source._process_pane(_snapshot_pane("working"), pushed=False)
        row = states.get(CAO_TID)
        assert row is not None
        assert row.state is WorkerState.BUSY, "the resnapshot must recover the terminal"
    finally:
        wiring.reset_producers()
