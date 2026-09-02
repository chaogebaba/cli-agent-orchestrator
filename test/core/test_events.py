"""AC2 — the event vocabulary and row shape (WP-ARCH phase 1, F725 #581)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
    WorkerEvent,
    parse_kind,
)

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

# The audit §3.1 kinds line, transcribed independently of the enum.
_AUDIT_KINDS = {
    "session.started",
    "session.resumed",
    "turn.started",
    "turn.ended",
    "tool.called",
    "tool.result",
    "prompt.awaiting",
    "prompt.answered",
    "submission.confirmed",
    "usage.capped",
    "process.exited",
    "status.legacy_published",
    "pane.missing",
    "pane.recovered",
}


def _draft(**overrides: object) -> EventDraft:
    fields: dict[str, object] = {
        "terminal_id": "t-1",
        "kind": EventKind.TURN_STARTED,
        "producer": Producer.JSONL,
        "confidence": Confidence.AUTHORITATIVE,
        "observed_at": _NOW,
    }
    fields.update(overrides)
    return EventDraft(**fields)  # type: ignore[arg-type]


def test_event_kinds_are_exactly_the_audit_list() -> None:
    """No kind added, none missing — and no periodic kind (r9 retired ``pane.alive``)."""
    assert {kind.value for kind in EventKind} == _AUDIT_KINDS
    assert "pane.alive" not in _AUDIT_KINDS
    assert "frame.classified" not in _AUDIT_KINDS


def test_producer_and_confidence_vocabularies() -> None:
    assert {producer.value for producer in Producer} == {"hook", "jsonl", "pane", "server"}
    assert {value.value for value in Confidence} == {"authoritative", "derived"}


def test_kind_vocabularies_are_disjoint() -> None:
    """``parse_kind`` is unambiguous only while the two enums share no string."""
    assert not {kind.value for kind in EventKind} & {kind.value for kind in DecisionKind}


def test_parse_kind_round_trips_both_vocabularies() -> None:
    assert parse_kind("turn.started") is EventKind.TURN_STARTED
    assert parse_kind("delivery.attempt") is DecisionKind.DELIVERY_ATTEMPT
    with pytest.raises(ValueError):
        parse_kind("nonsense.kind")


def test_decision_kinds_cover_every_blueprint_writer() -> None:
    """Each decision the blueprint names has a member here."""
    assert {kind.value for kind in DecisionKind} == {
        "status.transition",
        "status.reason_changed",
        "status.recovered",
        "delivery.attempt",
        "teardown.decided",
        "teardown.intended",
        "fleet.override",
        "probe.failed",
    }


def test_draft_defaults() -> None:
    draft = _draft()
    assert draft.payload == {}
    assert draft.source_ref is None
    assert draft.decision is None
    assert draft.evidence is None


def test_draft_is_frozen_and_rejects_unknown_fields() -> None:
    """Immutable rows and no silent extras: a typo becomes an error, not a lost column."""
    draft = _draft()
    with pytest.raises(ValidationError):
        draft.terminal_id = "other"
    with pytest.raises(ValidationError):
        _draft(sequence=3)


def test_naive_observed_at_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _draft(observed_at=datetime(2026, 9, 2, 12, 0))


def test_decision_row_must_set_matching_decision() -> None:
    ok = _draft(
        kind=DecisionKind.DELIVERY_ATTEMPT,
        decision=DecisionKind.DELIVERY_ATTEMPT,
        producer=Producer.SERVER,
        confidence=Confidence.AUTHORITATIVE,
        evidence="01J000000000000000000000EV",
    )
    assert ok.decision is DecisionKind.DELIVERY_ATTEMPT

    with pytest.raises(ValidationError, match="must set decision"):
        _draft(kind=DecisionKind.DELIVERY_ATTEMPT, producer=Producer.SERVER)

    with pytest.raises(ValidationError, match="does not match kind"):
        _draft(
            kind=DecisionKind.DELIVERY_ATTEMPT,
            decision=DecisionKind.FLEET_OVERRIDE,
            producer=Producer.SERVER,
        )


def test_worker_truth_row_may_not_carry_decision_or_evidence() -> None:
    """``WHERE decision IS NOT NULL`` must select exactly the server's own actions."""
    with pytest.raises(ValidationError, match="must not set decision"):
        _draft(decision=DecisionKind.STATUS_TRANSITION)
    with pytest.raises(ValidationError, match="must not set evidence"):
        _draft(evidence="01J000000000000000000000EV")


def test_worker_event_adds_the_store_minted_fields() -> None:
    event = WorkerEvent(
        terminal_id="t-1",
        kind=EventKind.TURN_ENDED,
        producer=Producer.JSONL,
        confidence=Confidence.AUTHORITATIVE,
        observed_at=_NOW,
        event_id="01J000000000000000000000EV",
        seq=1,
        ingested_at=_NOW,
    )
    assert event.seq == 1
    assert isinstance(event, EventDraft)


def test_worker_event_rejects_zero_seq_and_naive_ingested_at() -> None:
    """Sequences start at 1, so 0 is a bug rather than an empty terminal."""
    base: dict[str, object] = {
        "terminal_id": "t-1",
        "kind": EventKind.TURN_ENDED,
        "producer": Producer.JSONL,
        "confidence": Confidence.AUTHORITATIVE,
        "observed_at": _NOW,
        "event_id": "01J000000000000000000000EV",
        "ingested_at": _NOW,
    }
    with pytest.raises(ValidationError):
        WorkerEvent(seq=0, **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="timezone-aware"):
        WorkerEvent(
            **{**base, "ingested_at": datetime(2026, 9, 2, 12, 0)},  # type: ignore[arg-type]
            seq=1,
        )
