"""AC5 — the ingestion switch and the emit seam (WP-ARCH F725 #581, lane B).

Two promises are under test, and both are structural rather than behavioural:
with the switch off nothing reaches the store, and a store that throws never
reaches the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.adapters.truth import wiring
from cli_agent_orchestrator.core.events import Confidence, EventDraft, EventKind, Producer

from .conftest import FakeClock, FakeEventStore


def _draft(terminal_id: str = "t1") -> EventDraft:
    return EventDraft(
        terminal_id=terminal_id,
        kind=EventKind.TURN_STARTED,
        producer=Producer.JSONL,
        confidence=Confidence.AUTHORITATIVE,
        observed_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )


def test_the_adapter_layer_does_not_spell_the_switch_itself() -> None:
    """One switch, one spelling, in the module that reads it.

    ``bootstrap.py`` owns ``CAO_WORKER_TRUTH_INGEST`` and its test
    (``test/adapters/test_bootstrap_switch.py``) owns the parsing rules.  A second
    definition down here is how a switch quietly starts meaning two things — the
    producers would read one name and the composition root another, and the ON/OFF
    guarantee would hold for neither.
    """
    assert not hasattr(wiring, "INGEST_ENV_VAR")
    assert not hasattr(wiring, "ingest_enabled")


def test_emit_is_a_noop_with_nothing_installed() -> None:
    """The OFF half of every ON/OFF pair in this suite bottoms out here."""
    assert wiring.producers_installed() is False
    assert wiring.emit(_draft()) is None


def test_emit_stores_and_returns_the_minted_row(store: FakeEventStore, clock: FakeClock) -> None:
    wiring.install_producers(wiring.ProducerRuntime(store=store, clock=clock))
    stored = wiring.emit(_draft())
    assert stored is not None
    assert stored.seq == 1
    assert stored.event_id
    assert len(store.rows) == 1


def test_emit_swallows_a_broken_store_and_logs_once(
    store: FakeEventStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A diagnostic that can raise into the status monitor is an outage, not a diagnostic."""
    wiring.install_producers(wiring.ProducerRuntime(store=store, clock=clock))
    store.fail_next = True
    with caplog.at_level(logging.WARNING):
        assert wiring.emit(_draft()) is None
    assert any("ingest failed" in record.message for record in caplog.records)
    # The next append still works: one failure does not disarm ingestion.
    assert wiring.emit(_draft()) is not None


def test_reset_disarms(store: FakeEventStore, clock: FakeClock) -> None:
    """``bootstrap`` calls this on the AC5 N6 migration-failure path."""
    wiring.install_producers(wiring.ProducerRuntime(store=store, clock=clock))
    wiring.reset_producers()
    assert wiring.emit(_draft()) is None
    assert store.rows == []
