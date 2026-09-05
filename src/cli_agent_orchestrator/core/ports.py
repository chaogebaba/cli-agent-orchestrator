"""The Protocols that let ``app`` stay ignorant of ``adapters`` (WP-ARCH phase 1).

Import-linter contract ``adapters-only-via-composition-root`` forbids ``app``,
``api``, ``mcp_server`` and ``cli`` from importing ``adapters`` at all.  The one
place adapters are named is ``cli_agent_orchestrator/bootstrap.py``, which builds
them and hands them to ``app`` as the Protocols below.  That is the whole
mechanism: application code depends on these signatures, never on SQLite.

Protocols, not ABCs, deliberately — an adapter satisfies one by shape, so a test
double is a plain class and no adapter needs to inherit from core.  They are
``runtime_checkable`` so a bootstrap assertion can state what it built.  Note
that ``isinstance`` against a runtime-checkable Protocol checks method PRESENCE
only, never signatures; mypy checks the signatures, and it runs strict here.

Phase boundaries are marked on each Protocol.  ``QueueStore`` was the phase-3
stub and is now filled in.  ``GateStore`` and ``ProviderAdapter`` remain
deliberately thin: they exist so the composition root and the contracts have the
right shape from the start, and they grow in phases 4 and 5 respectively.  The blueprint r9 is explicit that the 12-method
provider protocol is a long-run sketch and NOT a phase, so ``ProviderAdapter``
here stays at the capability-flag surface phase 5 actually needs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cli_agent_orchestrator.core.delivery import (
    DeadReason,
    DeliveryAttempt,
    EnqueueDraft,
    MsgState,
    QueueMessage,
    QueueMode,
    QueueOccupancy,
    SeatDigest,
)
from cli_agent_orchestrator.core.events import AnyKind, EventDraft, WorkerEvent
from cli_agent_orchestrator.core.findings import Finding, FindingCode
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState

__all__ = [
    "CheckRunner",
    "Clock",
    "EventSource",
    "EventStore",
    "FindingStore",
    "GateStore",
    "ProviderAdapter",
    "QueueStore",
    "StateProjection",
    "StateStore",
]


@runtime_checkable
class Clock(Protocol):
    """The single source of "now" for everything in the new tree.

    Injected rather than called directly so a test can drive retention horizons
    and staleness windows without sleeping, and so the fork's existing
    deterministic-simulation clock can be wired in behind it.  Implementations
    MUST return aware UTC; the event models reject anything else.
    """

    def now(self) -> datetime: ...


@runtime_checkable
class EventStore(Protocol):
    """Append-only worker event log with contiguous per-terminal sequences.

    The contract a consumer may rely on, and the reason ``append`` owns the
    sequence: within one ``terminal_id`` the sequence starts at 1 and has NO
    GAPS (B7), because the high-water bump and the insert share one
    ``BEGIN IMMEDIATE`` transaction.  A replay consumer may therefore ask for
    ``seq + 1`` and know that a missing row means "not yet", never "lost".
    """

    def append(self, draft: EventDraft) -> WorkerEvent:
        """Mint ``event_id``/``seq``/``ingested_at`` and store the row."""
        ...

    def read(
        self,
        terminal_id: str | None = None,
        *,
        since_seq: int = 0,
        since: datetime | None = None,
        kinds: frozenset[AnyKind] | None = None,
        limit: int | None = None,
    ) -> list[WorkerEvent]:
        """Read rows in ``(terminal_id, seq)`` order, oldest first.

        ``terminal_id=None`` reads the whole fleet, which is what the agreement
        report (AC10) and ``cao diag --session`` need.
        """
        ...

    def get(self, event_id: str) -> WorkerEvent | None:
        """One row by id — the evidence-chain lookup behind ``cao diag --why``."""
        ...

    def high_water(self, terminal_id: str) -> int:
        """Highest ``seq`` issued for ``terminal_id``; 0 when it has no rows."""
        ...

    def prune(self, older_than: datetime) -> int:
        """Delete events ingested before ``older_than``, keeping evidence.

        Rows named by an OPEN finding's ``sample_event_id`` are retained however
        old they are.  Returns the number of rows deleted.
        """
        ...


@runtime_checkable
class FindingStore(Protocol):
    """Typed invariant findings, deduplicated by ``(code, terminal_id, dedupe_key)``."""

    def record(
        self,
        code: FindingCode,
        *,
        terminal_id: str = "",
        dedupe_key: str = "",
        detail: str = "",
        sample_event_id: str | None = None,
    ) -> Finding:
        """Insert the finding, or increment the count of the matching open one.

        The FIRST sample is kept on a repeat: the earliest occurrence is the one
        whose surrounding timeline still explains anything.
        """
        ...

    def list_findings(
        self, *, state: str | None = None, code: FindingCode | None = None
    ) -> list[Finding]: ...

    def resolve(self, finding_id: str) -> bool:
        """Mark one finding resolved, releasing its sample event to retention."""
        ...


@runtime_checkable
class CheckRunner(Protocol):
    """Runs the AC8 invariant checks as rows are appended.

    Injected into the store rather than called by it directly, because the checks
    live in ``app`` and ``adapters`` may not import ``app``
    (``adapters-are-leaves``).  The registry skeleton is
    ``app/worker_truth/checks.py``; the checks themselves land with the projector.

    Implementations MUST NOT raise: a diagnostic check that can break an append
    would turn a diagnosability feature into an outage.
    """

    def on_append(self, event: WorkerEvent) -> None: ...


class StateProjection(Protocol):
    """The shadow projection row for one terminal (AC6).

    Structural, not a model, so the adapter that owns the table decides its own
    representation.  ``last_probe_at`` and ``last_source_probe_at`` are the two
    liveness COLUMNS — heartbeats update them, and neither is ever an event row.

    Members are READ-ONLY properties rather than plain annotations, and that is
    deliberate.  A plain annotation makes mypy treat the member as settable, so
    a frozen dataclass cannot satisfy the Protocol — which forced both the
    projector's row type and the store's to be mutable even though nothing ever
    assigns to them.  Nothing assigns THROUGH the port either: ``get`` produces
    a row and ``upsert`` consumes one.  A read-only property is still satisfied
    by a plain instance attribute, so this is strictly more permissive than the
    annotation form, not less.
    """

    @property
    def terminal_id(self) -> str: ...

    @property
    def state(self) -> WorkerState: ...

    @property
    def since(self) -> datetime: ...

    @property
    def last_event_seq(self) -> int: ...

    @property
    def degraded_reason(self) -> DegradedReason | None: ...

    @property
    def prior_state(self) -> WorkerState | None: ...

    @property
    def last_probe_at(self) -> datetime | None: ...

    @property
    def last_source_probe_at(self) -> datetime | None: ...

    @property
    def pane_pid(self) -> int | None: ...

    @property
    def pane_present(self) -> bool: ...

    @property
    def miss_count(self) -> int: ...


@runtime_checkable
class StateStore(Protocol):
    """Read/write access to ``worker_state_shadow``.

    Phase 1 keeps this projection SHADOW-ONLY: nothing in ``services/`` reads it,
    which is what makes AC11's "no behaviour change with the switch ON" true by
    construction rather than by assertion.  The projector (AC6) is its only
    writer.
    """

    def get(self, terminal_id: str) -> StateProjection | None: ...

    def upsert(self, projection: StateProjection) -> None: ...

    def touch_probe(
        self,
        terminal_id: str,
        *,
        probed_at: datetime,
        pane_present: bool,
        pane_pid: int | None,
        miss_count: int,
    ) -> None:
        """Update the liveness COLUMNS only — never a state change, never a row."""
        ...

    def touch_source_probe(self, terminal_id: str, *, probed_at: datetime) -> None:
        """Bump ``last_source_probe_at``; called by an authoritative tailer's poll."""
        ...

    def all_terminals(self) -> list[StateProjection]:
        """Every projected terminal — the sweep's input."""
        ...


@runtime_checkable
class EventSource(Protocol):
    """A truth producer that runs as an asyncio task in the one process (U7).

    ``is_authoritative`` is the declaration behind source-level precedence (r9):
    a terminal has AT MOST ONE authoritative source.  While that source is
    healthy the projector applies derived events only for the kinds the source
    cannot know; while it is unhealthy, derived events apply fully as the
    legitimate fallback, with no finding — the pane is a first-class fallback,
    never a deprecated one.
    """

    @property
    def name(self) -> str: ...

    @property
    def is_authoritative(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class QueueStore(Protocol):
    """Delivery queue (audit §3.2, WP-ARCH phase 3).

    The four protocol operations are single SQL statements in the adapter, which
    is where the guarantees live: ``enqueue`` is replay-safe on the idempotency
    key, ``claim`` issues one owner and a fencing token, ``ack`` rejects a stale
    worker whose lease was stolen, and ``reclaim`` is the visibility-timeout
    redelivery that replaces a 2,311-line watchdog service.

    Sub-phase 3a calls only ``enqueue`` and the mirror-writer methods; ``claim``,
    ``ack`` and ``reclaim`` land here with their statements written and tested
    because two of the phase's three easiest-to-lose properties live inside them
    — the ``mode='live'`` filter that must sit in ``claim``'s own SQL rather than
    in its callers, and the ``dead_by`` column that no ``UPDATE`` may recompute.
    Both are cheaper to get right before there is a caller than after.
    """

    # -- the four statements ----------------------------------------------

    def enqueue(self, draft: EnqueueDraft) -> QueueMessage:
        """Insert a row, or return the existing one for a repeated key.

        Replay-safe: a second enqueue under the same ``idempotency_key`` returns
        the row already stored rather than a second row or an error, so a
        retried caller sees the same outcome it saw the first time.  A repeat
        carrying a DIFFERENT payload digest is a genuine conflict and raises,
        because that is a caller reusing one key for two messages.
        """
        ...

    def claim(
        self, *, lease_owner: str, now: datetime, limit: int = 1, receiver_id: str | None = None
    ) -> list[QueueMessage]:
        """Lease up to ``limit`` deliverable rows, incrementing the fencing token.

        The statement selects ``state='ready' AND available_at<=now AND
        mode='live'``.  **The mode filter lives inside this statement**, not in
        any caller: all three consumers of the queue inherit it that way — the
        boot occupancy test, the drain tick and the ordinary tick — and no future
        caller can forget it.  The occupancy predicate and the drain rule keep
        their own ``mode`` conditions as redundant defence rather than as the
        enforcement.
        """
        ...

    def ack(self, msg_id: str, claim_id: int, *, now: datetime) -> bool:
        """Settle one delivered row.  False when ``claim_id`` is stale.

        A slow worker whose lease was stolen and re-issued cannot ack a newer
        delivery: the statement matches on the fencing token, so zero rows
        changed means "rejected", not "not found".
        """
        ...

    def reclaim(self, *, now: datetime) -> tuple[int, int]:
        """Return expired leases to ``ready`` and dead-letter the exhausted.

        Returns ``(reclaimed, dead_lettered)``.  Increments ``attempts`` and adds
        ``DELIVERY_BACKOFF_S`` to ``available_at`` on each re-offer, and is the
        SOLE writer of that column.  It does NOT touch ``dead_by``: recomputing
        the deadline from the current ``available_at`` here would extend it on
        every re-offer and the row would never die (D12).
        """
        ...

    # -- reads --------------------------------------------------------------

    def get(self, msg_id: str) -> QueueMessage | None: ...

    def get_by_idempotency_key(self, key: str) -> QueueMessage | None: ...

    def attempts_for(self, msg_id: str) -> list[DeliveryAttempt]:
        """Every attempt for one id, oldest first — the ``cao diag`` body (I5)."""
        ...

    def occupancy(self) -> QueueOccupancy:
        """What D9's boot guard resolves the requested position against.

        Counts rows that are BOTH ``mode='live'`` and non-terminal.  Callers do
        not compose this from ``claim``; a guard that ran the claim statement
        would issue leases as a side effect of asking a question.
        """
        ...

    def count(self, *, mode: QueueMode | None = None) -> int:
        """Rows in ``delivery_msg``, optionally by mode.

        AC-3a's off-arm criterion is a count from here: with the switch off the
        count is nil, and rows in that arm are a failure rather than a curiosity.
        """
        ...

    # -- writes the mirror writer and the tick share -------------------------

    def record_attempt(self, attempt: DeliveryAttempt) -> None:
        """Write one attempt row, idempotently on its primary key."""
        ...

    def settle(
        self,
        msg_id: str,
        *,
        state: MsgState,
        now: datetime,
        reason: DeadReason | None = None,
        attempts: int | None = None,
    ) -> bool:
        """Move one row to a terminal state.  False when it was already terminal.

        ``reason`` is required for :attr:`MsgState.DEAD` and writes the
        ``delivery_dead`` row in the same transaction.  Terminal states are
        final: settling an already-terminal row is refused rather than
        overwritten, so a late edge cannot rewrite a recorded outcome.
        """
        ...

    def mark_dialog_hold(self, msg_id: str, *, held_since: datetime | None) -> None:
        """Set or clear the ``held_since`` clock (D12)."""
        ...

    # -- the digest ----------------------------------------------------------

    def open_digest(self, receiver_id: str) -> SeatDigest | None:
        """The receiver's open epoch, if one is open."""
        ...

    def reparent(
        self,
        msg_id: str,
        *,
        new_receiver_id: str,
        now: datetime,
        message_prefix: str = "",
    ) -> bool:
        """Move one undelivered row to another mailbox, digests and all.

        **One transaction, or none of it.**  The same transaction that rewrites
        ``receiver_id`` removes the id from the old receiver's open epoch and
        adds it to the new receiver's, opening one if none is open — so an id is
        listed in exactly one epoch and no fewer.  Splitting the two writes is
        one of the phase's three easiest-to-lose properties and it has no second
        line of defence: the moved id would stay listed in an epoch it no longer
        belongs to, never reach a terminal state there, and hold that epoch open
        forever while the tick re-woke it once per lease.

        An epoch left EMPTY by the move closes immediately as ``abandoned``,
        without waiting for a tick: the trigger is a reap, so the old receiver
        has no live incarnation, and an empty set satisfies the
        every-message-terminal test vacuously.
        """
        ...


@runtime_checkable
class GateStore(Protocol):
    """Gate run state machine — PHASE 4 stub (audit §3.3)."""

    def start_run(self, *args: object, **kwargs: object) -> object: ...


@runtime_checkable
class ProviderAdapter(Protocol):
    """Provider capability surface — PHASE 5 stub (audit §3.4).

    r9 is explicit: phase 5 adds capability FLAGS to the existing
    ``ProviderCapabilities`` and providers grow into them one at a time.  The
    12-method adapter protocol is a long-run sketch, not a phase, so this stub
    carries only what phase 5 actually consumes.
    """

    @property
    def name(self) -> str: ...

    @property
    def structured_events(self) -> bool: ...

    def event_source(self, terminal_id: str) -> EventSource | None: ...
