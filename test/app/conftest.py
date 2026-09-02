"""Shared wiring for the lane-C tests (WP-ARCH phase 1, F725 #581)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from test.app.fakes import (
    FakeClock,
    InMemoryEventStore,
    InMemoryFindingStore,
    InMemoryStateStore,
)

import pytest

from cli_agent_orchestrator.app.worker_truth.checks import (
    CheckRegistry,
    LegacyDisagreementCheck,
    register_phase1_checks,
)
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.core.events import (
    AnyKind,
    Confidence,
    DecisionKind,
    EventDraft,
    Producer,
    WorkerEvent,
)


@dataclass
class Rig:
    """Everything a projector test needs, wired the way ``bootstrap`` will wire it."""

    clock: FakeClock
    events: InMemoryEventStore
    states: InMemoryStateStore
    findings: InMemoryFindingStore
    registry: CheckRegistry
    checks: LegacyDisagreementCheck
    sources: StaticSourceRegistry
    projector: Projector

    def emit(
        self,
        terminal_id: str,
        kind: AnyKind,
        *,
        producer: Producer = Producer.JSONL,
        confidence: Confidence = Confidence.AUTHORITATIVE,
        payload: dict[str, object] | None = None,
        source_ref: str | None = None,
        msg_id: str | None = None,
        run_id: str | None = None,
        decision: DecisionKind | None = None,
        evidence: str | None = None,
        observed_at: datetime | None = None,
    ) -> WorkerEvent:
        """Append one hand-built draft and fold it, as a producer would.

        Lane B owns the real producers; every lane-C test drives the projector
        from drafts so a change in a producer can never quietly change what these
        tests assert about the FOLD.
        """
        stored = self.events.append(
            EventDraft(
                terminal_id=terminal_id,
                kind=kind,
                producer=producer,
                confidence=confidence,
                observed_at=observed_at if observed_at is not None else self.clock.now(),
                payload=dict(payload or {}),
                source_ref=source_ref,
                msg_id=msg_id,
                run_id=run_id,
                decision=decision,
                evidence=evidence,
            )
        )
        self.projector.project(stored)
        return stored

    def pane(
        self,
        terminal_id: str,
        kind: AnyKind,
        *,
        payload: dict[str, object] | None = None,
    ) -> WorkerEvent:
        """Shorthand for a derived, pane-produced event."""
        return self.emit(
            terminal_id,
            kind,
            producer=Producer.PANE,
            confidence=Confidence.DERIVED,
            payload=payload,
        )

    def legacy(self, terminal_id: str, latched_status: str, origin: str = "incremental"):
        """Shorthand for one ``status.legacy_published`` row."""
        from cli_agent_orchestrator.core.events import EventKind

        return self.pane(
            terminal_id,
            EventKind.STATUS_LEGACY_PUBLISHED,
            payload={"latched_status": latched_status, "origin": origin},
        )

    def state_of(self, terminal_id: str):
        row = self.states.get(terminal_id)
        return None if row is None else row.state


@pytest.fixture
def rig() -> Rig:
    clock = FakeClock()
    events = InMemoryEventStore(clock)
    states = InMemoryStateStore()
    findings = InMemoryFindingStore(clock)
    registry = register_phase1_checks(CheckRegistry(findings))
    checks = LegacyDisagreementCheck(findings, events, states, clock)
    events.set_checks(registry)
    events.bind_findings(findings)
    sources = StaticSourceRegistry()
    projector = Projector(events, states, clock, sources, legacy_check=checks)
    return Rig(clock, events, states, findings, registry, checks, sources, projector)
