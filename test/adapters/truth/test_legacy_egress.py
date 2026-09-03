"""AC4b (i) — the legacy status egress producer (WP-ARCH F725 #581, lane B).

The named runs from the blueprint appear verbatim below: a hundred identical
published observations must yield one row, and a live idle→busy flip on the
CONTINUOUS path must yield one row.  The second is what kills the mutant "hook
the on-demand probe path instead", because that path never fires for a
continuous flip.
"""

from __future__ import annotations

from enum import Enum

import pytest

from cli_agent_orchestrator.adapters.truth import legacy_egress
from cli_agent_orchestrator.core.events import DecisionKind, EventKind

from .conftest import FakeEventStore


class _Status(Enum):
    """Stands in for ``TerminalStatus`` — the producer must render ``.value``."""

    IDLE = "idle"
    PROCESSING = "processing"


class _Monitor:
    """The shape of ``StatusMonitor`` the hook actually touches."""

    def __init__(self, condition: str | None = None, fusion_reason: str | None = None) -> None:
        self._condition = condition
        self._status_fusion_reason = {"t1": fusion_reason} if fusion_reason else {}

    def get_condition(self, terminal_id: str) -> str | None:
        return self._condition


def _publish(
    monitor: _Monitor,
    *,
    status: _Status = _Status.IDLE,
    origin: str | None = "incremental",
    frame_source: str = "incremental",
    pass_outcome: str = "ok",
    raw: object = None,
) -> None:
    legacy_egress.record_legacy_publish(
        monitor, "t1", status, origin, frame_source, pass_outcome, raw
    )


def test_off_writes_nothing(store: FakeEventStore) -> None:
    monitor = _Monitor()
    for _ in range(10):
        _publish(monitor)
    assert store.rows == []


def test_hundred_identical_publishes_are_one_row(ingest_on: FakeEventStore) -> None:
    """B9: edge-triggered on the ``(latched_status, origin)`` pair."""
    monitor = _Monitor()
    for _ in range(100):
        _publish(monitor)
    rows = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)
    assert len(rows) == 1
    assert rows[0].payload["latched_status"] == "idle"
    assert rows[0].payload["origin"] == "incremental"
    assert rows[0].producer.value == "pane"
    assert rows[0].confidence.value == "derived"


def test_idle_to_busy_flip_on_the_continuous_path_is_one_row(
    ingest_on: FakeEventStore,
) -> None:
    """The mutant-killing run: the flip arrives via ``origin='incremental'``.

    A producer hooked at the on-demand probe path (``status_monitor.py:2772``)
    would see none of these calls and write nothing at all.
    """
    monitor = _Monitor()
    for _ in range(5):
        _publish(monitor, status=_Status.IDLE)
    for _ in range(5):
        _publish(monitor, status=_Status.PROCESSING)
    rows = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)
    assert [row.payload["latched_status"] for row in rows] == ["idle", "processing"]


def test_origin_none_and_incremental_are_the_same_edge(ingest_on: FakeEventStore) -> None:
    """``_publish_observation`` defaults ``None`` to ``incremental``; so must the hook."""
    monitor = _Monitor()
    _publish(monitor, origin=None)
    _publish(monitor, origin="incremental")
    assert len(ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)) == 1


def test_origin_none_with_forced_outcome_defaults_to_forced(
    ingest_on: FakeEventStore,
) -> None:
    monitor = _Monitor()
    _publish(monitor, origin=None, pass_outcome="forced")
    rows = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)
    assert rows[0].payload["origin"] == "forced"


def test_same_status_different_origin_is_a_new_edge(ingest_on: FakeEventStore) -> None:
    monitor = _Monitor()
    _publish(monitor, origin="incremental")
    _publish(monitor, origin="probe")
    assert len(ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)) == 2


def test_payload_carries_every_blueprint_field(ingest_on: FakeEventStore) -> None:
    monitor = _Monitor(condition="BUSY", fusion_reason="resync_after_drop")
    _publish(monitor, frame_source="native", pass_outcome="native", raw="classified")
    payload = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)[0].payload
    assert payload == {
        "latched_status": "idle",
        "origin": "incremental",
        "frame_source": "native",
        "pass_outcome": "native",
        "raw_classification": "classified",
        "fusion_reason": "resync_after_drop",
        "condition": "BUSY",
    }


def test_capped_condition_also_appends_usage_capped(ingest_on: FakeEventStore) -> None:
    """AC4b: the rollout can never say this, so the pane egress must."""
    monitor = _Monitor(condition=legacy_egress.CAPPED_CONDITION_LABEL)
    _publish(monitor)
    _publish(monitor)  # same pair: no second usage.capped either
    assert len(ingest_on.of_kind(EventKind.USAGE_CAPPED)) == 1


def test_capped_edge_fires_even_when_the_publish_pair_is_unchanged(
    ingest_on: FakeEventStore,
) -> None:
    """A cap can be detected while status and origin sit still."""
    monitor = _Monitor()
    _publish(monitor)
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED) == []
    monitor._condition = legacy_egress.CAPPED_CONDITION_LABEL
    _publish(monitor)
    assert len(ingest_on.of_kind(EventKind.USAGE_CAPPED)) == 1


def test_a_raising_monitor_never_breaks_the_publish_path(ingest_on: FakeEventStore) -> None:
    class _Hostile:
        _status_fusion_reason: dict[str, str] = {}

        def get_condition(self, terminal_id: str) -> str:
            raise RuntimeError("boom")

    legacy_egress.record_legacy_publish(
        _Hostile(), "t1", _Status.IDLE, "incremental", "incremental", "ok", None
    )
    rows = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)
    assert len(rows) == 1
    assert rows[0].payload["condition"] is None


def test_fleet_override_cites_the_observation_it_overrode(ingest_on: FakeEventStore) -> None:
    monitor = _Monitor()
    _publish(monitor)
    published = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)[0]
    legacy_egress.record_fleet_override("t1", "window_absent", "%42")
    override = ingest_on.of_kind(DecisionKind.FLEET_OVERRIDE)[0]
    assert override.evidence == published.event_id
    assert override.decision is DecisionKind.FLEET_OVERRIDE
    assert override.payload["reason"] == "window_absent"
    assert override.payload["overridden_to"] == "ERROR"


def test_fleet_override_with_no_prior_publish_cites_nothing(
    ingest_on: FakeEventStore,
) -> None:
    """Deliberate: a decision citing nothing is what DIAG-GHOST-TRANSITION looks for.

    Inventing an evidence id to make the row look complete is the mutant
    "evidence dropped from the decision row" wearing a disguise — the check would
    then never fire, and nothing would notice.
    """
    legacy_egress.record_fleet_override("t1", "init_health_failed")
    assert ingest_on.of_kind(DecisionKind.FLEET_OVERRIDE)[0].evidence is None


def test_fleet_override_off_writes_nothing(store: FakeEventStore) -> None:
    legacy_egress.record_fleet_override("t1", "window_absent")
    assert store.rows == []


def test_forget_drops_one_terminals_edge_state(ingest_on: FakeEventStore) -> None:
    monitor = _Monitor()
    _publish(monitor)
    legacy_egress.forget("t1")
    _publish(monitor)
    assert len(ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED)) == 2


@pytest.mark.parametrize("value,expected", [(None, None), (_Status.IDLE, "idle"), (7, "7")])
def test_value_rendering_prefers_enum_value(value: object, expected: str | None) -> None:
    assert legacy_egress._as_text(value) == expected
