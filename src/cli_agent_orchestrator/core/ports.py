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

Phase boundaries are marked on each Protocol.  ``QueueStore``, ``GateStore`` and
``ProviderAdapter`` are deliberately thin stubs: they exist so the composition
root and the contracts have the right shape from the start, and they grow in
phases 3, 4 and 5 respectively.  The blueprint r9 is explicit that the 12-method
provider protocol is a long-run sketch and NOT a phase, so ``ProviderAdapter``
here stays at the capability-flag surface phase 5 actually needs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

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
    """Delivery queue — PHASE 3 stub (audit §3.2).

    Present now so the composition root and the layering contracts have their
    final shape.  Phase 3 adds the idempotency key, ``claim_id`` fencing, lease
    expiry reclaim, the separate dead-letter table and the single ``seat_digest``
    row per epoch.  Implementing it before then would be building against a
    design that has not been gated.
    """

    def enqueue(self, *args: object, **kwargs: object) -> object: ...


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
