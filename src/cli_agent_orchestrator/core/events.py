"""The worker event vocabulary (WP-ARCH phase 1, AC2).

One append-only log carries two kinds of row, distinguished by the ``decision``
column of the audit §3.1 DDL:

* **Worker truth** — what a producer OBSERVED about a terminal.  ``kind`` comes
  from :class:`EventKind`, ``decision`` and ``evidence`` are ``NULL``.
* **Server decisions** — what the server DID, recorded beside the observation
  that justified it.  ``kind`` comes from :class:`DecisionKind`, ``decision``
  repeats it, and ``evidence`` names the ``event_id`` that justified the call.

Keeping decisions in the same table with the same per-terminal ``seq`` is what
makes ``cao diag <id>`` a single ordered read instead of a join across logs, and
what lets ``DIAG-GHOST-TRANSITION`` notice a decision that cites nothing.

Only BOUNDARY events are durable.  Streaming deltas — token chunks, partial tool
output, a redraw — are never written; that is the OpenCode lesson in the audit
§6, and r9 additionally retired the periodic ``pane.alive`` kind: a heartbeat is
a COLUMN update on the projection, never a row here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "Confidence",
    "DecisionKind",
    "EventDraft",
    "EventKind",
    "Producer",
    "WorkerEvent",
    "AnyKind",
    "parse_kind",
]


class Producer(StrEnum):
    """Who wrote the row.  Audit §3.1: ``'hook' | 'jsonl' | 'pane' | 'server'``."""

    HOOK = "hook"
    JSONL = "jsonl"
    PANE = "pane"
    SERVER = "server"


class Confidence(StrEnum):
    """How much the projector may lean on the row.

    Source-level precedence (r9) means confidence is a property of the PRODUCER,
    not of an individual observation: a terminal has at most one authoritative
    source, declared by its adapter, and everything else is derived.
    """

    AUTHORITATIVE = "authoritative"
    DERIVED = "derived"


class EventKind(StrEnum):
    """Boundary events, exactly the audit §3.1 kinds line.

    Ownership notes that are load-bearing and easy to lose:

    * ``USAGE_CAPPED`` has no rollout record — it can only come from the pane or
      the legacy egress, never from the codex JSONL.
    * ``PROCESS_EXITED`` has exactly ONE owner, the liveness probe (AC4b); its
      payload ``reason`` is ``teardown`` iff a live ``teardown.intended``
      decision row exists for the terminal, else ``crash``.
    * ``STATUS_LEGACY_PUBLISHED`` is EDGE-TRIGGERED on the
      ``(latched_status, origin)`` pair, so a hundred identical publishes are one
      row.
    * ``PANE_MISSING``/``PANE_RECOVERED`` are probe EDGES.  Heartbeats are
      projection columns.
    """

    SESSION_STARTED = "session.started"
    SESSION_RESUMED = "session.resumed"
    TURN_STARTED = "turn.started"
    TURN_ENDED = "turn.ended"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    PROMPT_AWAITING = "prompt.awaiting"
    PROMPT_ANSWERED = "prompt.answered"
    SUBMISSION_CONFIRMED = "submission.confirmed"
    USAGE_CAPPED = "usage.capped"
    PROCESS_EXITED = "process.exited"
    STATUS_LEGACY_PUBLISHED = "status.legacy_published"
    PANE_MISSING = "pane.missing"
    PANE_RECOVERED = "pane.recovered"


class DecisionKind(StrEnum):
    """Server decision rows — the ``decision`` column's vocabulary.

    Every member is named in the blueprint §4: the projector writes the three
    ``status.*`` rows (AC6), the dispatch hooks write ``delivery.attempt``,
    ``teardown.decided`` and ``teardown.intended`` (AC4c), the fleet ERROR
    overrides write ``fleet.override`` and the liveness probe writes
    ``probe.failed`` (AC4b).
    """

    STATUS_TRANSITION = "status.transition"
    STATUS_REASON_CHANGED = "status.reason_changed"
    STATUS_RECOVERED = "status.recovered"
    DELIVERY_ATTEMPT = "delivery.attempt"
    TEARDOWN_DECIDED = "teardown.decided"
    TEARDOWN_INTENDED = "teardown.intended"
    FLEET_OVERRIDE = "fleet.override"
    PROBE_FAILED = "probe.failed"


AnyKind = EventKind | DecisionKind


def parse_kind(value: str) -> AnyKind:
    """Resolve a stored ``kind`` string back to its enum member.

    The two vocabularies are disjoint by construction (a test asserts it), so a
    single lookup is unambiguous.
    """
    try:
        return EventKind(value)
    except ValueError:
        return DecisionKind(value)


class EventDraft(BaseModel):
    """What a PRODUCER hands to the store.

    ``event_id``, ``seq`` and ``ingested_at`` are deliberately absent: all three
    are minted by the store inside the one ``BEGIN IMMEDIATE`` transaction that
    also bumps the high-water mark (AC3).  A producer that could choose its own
    ``seq`` is a producer that can leave a gap, and B7 makes gaps illegal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    terminal_id: str = Field(min_length=1)
    kind: AnyKind
    producer: Producer
    confidence: Confidence
    observed_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    source_ref: str | None = None
    run_id: str | None = None
    msg_id: str | None = None
    decision: DecisionKind | None = None
    evidence: str | None = None

    @field_validator("observed_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        """Timestamps are aware UTC, fork-wide convention.

        A naive timestamp here would sort correctly against other naive ones and
        wrongly against everything else, which is exactly the class of bug the
        agreement report (AC10) must not have to explain away.
        """
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _check_decision_shape(self) -> "EventDraft":
        """Enforce the DDL's implicit contract between ``kind``/``decision``/``evidence``.

        Three rules, each of which a mutant could otherwise slip past:

        1. A :class:`DecisionKind` row MUST set ``decision`` to that same kind —
           the column exists so ``WHERE decision IS NOT NULL`` selects exactly
           the server's own actions.
        2. An :class:`EventKind` row MUST leave ``decision`` ``NULL``; worker
           truth is not a decision.
        3. ``evidence`` is meaningful only on a decision row.  Dropping evidence
           from a decision is one of the phase-1 mutants
           (``DIAG-GHOST-TRANSITION`` must fire), so the shape is checked here
           and the emptiness is checked by that runtime check, not by this model.
        """
        if isinstance(self.kind, DecisionKind):
            if self.decision is None:
                raise ValueError(f"decision row {self.kind.value} must set decision")
            if self.decision is not self.kind:
                raise ValueError(
                    f"decision {self.decision.value} does not match kind {self.kind.value}"
                )
        else:
            if self.decision is not None:
                raise ValueError(f"worker-truth row {self.kind.value} must not set decision")
            if self.evidence is not None:
                raise ValueError(f"worker-truth row {self.kind.value} must not set evidence")
        return self


class WorkerEvent(EventDraft):
    """A stored row: the draft plus the three fields the store mints.

    Subclassing keeps one field list.  Readers get a fully typed row; writers
    cannot fabricate ``seq``.
    """

    event_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    ingested_at: datetime

    @field_validator("ingested_at")
    @classmethod
    def _require_aware_ingested(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("ingested_at must be timezone-aware (UTC)")
        return value
