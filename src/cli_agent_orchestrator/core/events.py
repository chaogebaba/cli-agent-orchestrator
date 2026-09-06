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
    "FLEET_TERMINAL_ID",
    "PROJECTION_ORIGIN",
    "SOURCE_REF_PREFIXES",
    "Confidence",
    "DecisionKind",
    "EventDraft",
    "EventKind",
    "Producer",
    "SourceRefScheme",
    "WorkerEvent",
    "AnyKind",
    "parse_kind",
    "source_ref",
]

#: The ``terminal_id`` for a row that is about the FLEET, not about one
#: terminal — ``probe.failed`` is the phase-1 case, since a probe that fails
#: names no pane (B13: a failed call is not pane absence).
#:
#: A sentinel rather than a nullable column: ``worker_event.terminal_id`` is
#: ``NOT NULL`` in the audit DDL and ``EventDraft`` requires a non-empty string,
#: so fleet-wide rows need a value, and a reserved one keeps them filterable and
#: greppable instead of hiding behind a NULL.
#:
#: It lives in ``core`` because the LAYERING leaves nowhere else. The producer
#: that writes it is an adapter and the ``cao diag`` consumer that filters it
#: sits in ``app``/``cli``, which the ``adapters-only-via-composition-root``
#: contract forbids from importing ``adapters`` at all. A constant both sides
#: must agree on, with no import path between them, is a core vocabulary
#: constant — exactly like the enums below.
#:
#: The double-underscore prefix is deliberate: CAO terminal ids are hex session
#: identifiers, so this can never collide with a real one.
FLEET_TERMINAL_ID = "__fleet__"

#: The ``origin`` WP-ARCH phase 2's D1 publisher stamps on a status the PROJECTION
#: caused, and the value its ``fed_by`` payload field then carries (D5).
#:
#: It lives here for the same reason ``FLEET_TERMINAL_ID`` does: the producer that
#: writes it is an adapter and the agreement classifier that must drop rows
#: carrying it sits in ``app``, which the ``adapters-only-via-composition-root``
#: contract forbids from importing ``adapters`` at all.  A constant both sides
#: must agree on, with no import path between them, is a core vocabulary constant.
#: A second spelling would be exactly the defect D5 exists to prevent, arriving by
#: a different door: the classifier would stop recognising the echoes and the
#: agreement report would go back to comparing the projection with itself.
PROJECTION_ORIGIN = "worker_truth"


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
    * ``STATUS_PANE_CLASSIFIED`` is phase 2's fifteenth kind (D1c).  Phase 1
      fixed the set at the audit's fourteen and asserted it by strict equality
      (``test_event_kinds_are_exactly_the_audit_list``), so growing it is a
      deliberate amendment in TWO places — here and that test's hardcoded list
      — carrying the weight phase 3 carried when it added five finding codes.

      It exists because phase 2's D1 suppresses the pane path's PUBLISH for a
      source-healthy terminal, and phase 1's ``status.legacy_published`` producer
      is hooked at the EGRESS: suppressing the publish would suppress the record
      of the reading too, leaving ``DIAG-PANE-DISAGREE`` with one side of its
      comparison.  The classifier keeps running (I7), so the reading exists; only
      its record was lost.  This kind is that record, appended at the
      classification site and EDGE-TRIGGERED on the same ``(latched_status,
      origin)`` pair the legacy producer uses, so a sourced terminal costs one
      row per classification edge rather than one per output chunk.
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
    STATUS_PANE_CLASSIFIED = "status.pane_classified"
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


class SourceRefScheme(StrEnum):
    """The closed set of ``source_ref`` schemes phase 2's producers may build.

    D4's correction, stated as what the code does rather than as what an earlier
    draft hoped: ``source_ref`` is a **provenance** field, not a join key.  There
    are three schemes, one per producer, and no two are alike —

    ============  ============================================  ==============
    scheme        shape                                         producer
    ============  ============================================  ==============
    ``TRANSCRIPT``  ``transcript:<resolved path>#<record uuid>``   the tailer
    ``HOOK``        ``hook:<hook_event_name>#<idempotency_key>``   the hook route
    ``PANE``        ``pane:<terminal_id>#<seq>``                   the classifier
    ============  ============================================  ==============

    A key of ``(terminal_id, kind, source_ref)`` therefore does NOT collapse a
    pair of producers observing one fact, and it is not meant to: the fold's
    idempotency is per-producer, and two observations of one fact land on the
    transition table's diagonal, which is a ``NO_OP``.  Both rows are retained,
    which is what the ``producer`` column is for.

    Built in ONE place so a fourth producer cannot invent a fourth shape
    unnoticed — the prefix set is asserted by a test (AC-2a).

    The pre-existing ``rollout:`` prefix of phase 1's codex tailer
    (``adapters/truth/codex_rollout.py``) is deliberately NOT a member: this
    constructor is the closed set for the schemes phase 2 introduces, and
    rewriting a shipped producer's provenance format is not in this sub-phase's
    build line.
    """

    TRANSCRIPT = "transcript"
    HOOK = "hook"
    PANE = "pane"


#: Exactly the prefixes :func:`source_ref` can produce, with their separators.
#: AC-2a asserts this set is exactly these three; a fourth fails it.
SOURCE_REF_PREFIXES: frozenset[str] = frozenset(f"{scheme.value}:" for scheme in SourceRefScheme)


def source_ref(scheme: SourceRefScheme, subject: str, discriminator: str | object) -> str:
    """Build one ``source_ref``.  The only sanctioned constructor.

    ``subject`` is the scheme's first field — a resolved absolute path for
    ``TRANSCRIPT`` (the form the tailer already keys its offset by, parent §4
    AC4/B5), a ``hook_event_name`` for ``HOOK``, a ``terminal_id`` for ``PANE``.
    ``discriminator`` is the second: a record uuid, an idempotency key, a
    sequence number.

    Both halves are required to be non-empty.  A ``source_ref`` missing its
    discriminator would look like provenance and identify nothing, which is
    worse than a null: a reader would believe the row could be traced.
    """
    subject_text = str(subject).strip()
    discriminator_text = str(discriminator).strip()
    if not subject_text:
        raise ValueError(f"source_ref {scheme.value} requires a non-empty subject")
    if not discriminator_text:
        raise ValueError(f"source_ref {scheme.value} requires a non-empty discriminator")
    return f"{scheme.value}:{subject_text}#{discriminator_text}"


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
    #: WP-ARCH phase 2, D4.  Caller-supplied, and null for every producer that
    #: cannot be retried by a transport this server does not control — which is
    #: all of them but the claude_code hook route.  Supplying it makes the append
    #: replay-safe: a second append under the same key returns the row already
    #: stored, consuming no sequence number.
    idempotency_key: str | None = None

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
