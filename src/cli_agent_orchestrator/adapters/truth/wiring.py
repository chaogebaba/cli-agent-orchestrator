"""The ingestion switch and the one seam legacy code reaches truth through (AC5).

Phase 1 promises "no behaviour change", and AC5 says that promise must be
*enforced, not asserted*.  This module is how it is enforced.

The switch is structural, not a per-call environment read.  ``bootstrap.py``
owns ``CAO_WORKER_TRUTH_INGEST``, reads it ONCE at boot, and only when it is set
builds the store and calls :func:`install_producers`.  With nothing installed,
every producer and every one of the seven legacy hook points costs exactly one
module-global lookup and returns.  There is no code path from a hook to the
database that does not go through the ``_runtime is None`` check below, so "the
switch was ignored" — a phase-1 mutant — cannot be expressed as a missing ``if``
in a producer: it would have to be a deleted install guard in the composition
root, where the A/B suite sees it.

The env var is deliberately NOT named here.  One spelling of a switch, in the one
module that reads it; a second definition in the adapter layer is how a switch
starts meaning two different things.

The second promise this module keeps is that **ingestion never breaks the thing
it observes**.  A diagnostic that can raise into ``send_input`` or into the status
monitor's publish path would turn a diagnosability feature into an outage, and
the fork's own history (a nameless auto-answer rule silently skipped for weeks,
#559) says the failure will be discovered late.  So :func:`emit` swallows every
``Exception``, logs the first few occurrences with a traceback and then falls
silent, and returns ``None``.  Callers in legacy code are written to ignore the
return value entirely.

``BaseException`` is deliberately NOT swallowed: a ``KeyboardInterrupt`` or a
``CancelledError`` arriving inside an emit belongs to the caller's control flow,
and eating it would hang a shutdown.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from cli_agent_orchestrator.core.events import EventDraft, WorkerEvent
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ports import (
    Clock,
    EventStore,
    FindingStore,
    StateFolder,
    StateStore,
)

__all__ = [
    "ProducerRuntime",
    "emit",
    "record_finding",
    "install_producers",
    "producer_runtime",
    "producers_installed",
    "reset_producers",
]

logger = logging.getLogger(__name__)

#: How many emit failures are logged with a traceback before the logger falls
#: silent.  A store that is broken is broken for every subsequent append, and a
#: warning per event would drown the log the operator needs to read.
_MAX_LOGGED_FAILURES = 3


@dataclass(frozen=True)
class ProducerRuntime:
    """Everything a phase-1 producer needs, assembled by the composition root.

    Frozen: a producer may read it, never rebind it.  ``state_store`` and
    ``findings`` are optional so lane-by-lane bring-up works — the liveness probe
    degrades to appending only its edge EVENTS when no ``StateStore`` is wired,
    which is strictly less information but never wrong information.
    """

    store: EventStore
    clock: Clock
    state_store: StateStore | None = None
    findings: FindingStore | None = None
    #: WP-ARCH phase 2, A1 — the fold's driver.
    #:
    #: Typed as the ``StateFolder`` PROTOCOL and never as ``Projector``. That is
    #: the whole of the fix rather than a style note: this module is an adapter
    #: and the projector lives in ``app``, so a field annotated on the concrete
    #: class would put the forbidden import back while looking like a port, and
    #: ``test_adapters_never_import_app`` fails on it either way. The composition
    #: root fills it with the projector, which satisfies the Protocol
    #: structurally.
    #:
    #: Optional, the shape the store's own ``CheckRunner`` already has: a lane
    #: bringing producers up without a projector still appends, which is strictly
    #: less information and never wrong information.
    folder: StateFolder | None = None


_lock = threading.Lock()
_runtime: ProducerRuntime | None = None
_failure_count = 0


def install_producers(runtime: ProducerRuntime) -> None:
    """Arm ingestion.  Called by ``bootstrap.py`` only when the switch is on."""
    global _runtime, _failure_count
    with _lock:
        _runtime = runtime
        _failure_count = 0


def reset_producers() -> None:
    """Disarm ingestion.

    Used by ``shutdown_worker_truth``, by ``bootstrap.py`` on the AC5 ``N6`` path
    (the migrator failed: the server writes one ``DIAG-MIGRATION-FAILED``
    finding, disables ingestion for the process and boots normally), and by every
    test that installed a fake.
    """
    global _runtime, _failure_count
    with _lock:
        _runtime = None
        _failure_count = 0


def producer_runtime() -> ProducerRuntime | None:
    """The installed runtime, or ``None`` when ingestion is off."""
    return _runtime


def producers_installed() -> bool:
    """True when a runtime is installed, i.e. producers and hooks are live."""
    return _runtime is not None


def record_finding(
    code: FindingCode,
    *,
    terminal_id: str = "",
    dedupe_key: str = "",
    detail: str = "",
) -> None:
    """Record one DEDUPLICATED finding through the installed store.

    The finding half of :func:`emit`, and it exists for the same reason: a
    legacy module that meets a condition worth counting must have exactly one
    path to the phase-1 store, and that path must not be able to break the
    operation it observes.  ``FindingStore.record`` already folds repeats into a
    ``count`` on one row, so a hook on a hot path (a per-poll condition, say)
    writes one row that climbs rather than a row per occurrence — which is the
    property that makes counting a silent fallback affordable at all.

    Silent when ingestion is off (no runtime, or a runtime wired without a
    ``FindingStore`` during lane-by-lane bring-up), and never raises
    ``Exception``: the caller is an observation site, so a diagnostic that threw
    into it would convert a store fault into a failure of the thing observed.
    ``BaseException`` is deliberately not swallowed, exactly as in :func:`emit`.
    """
    global _failure_count
    runtime = _runtime
    if runtime is None or runtime.findings is None:
        return
    try:
        runtime.findings.record(code, terminal_id=terminal_id, dedupe_key=dedupe_key, detail=detail)
    except Exception:
        with _lock:
            _failure_count += 1
            should_log = _failure_count <= _MAX_LOGGED_FAILURES
        if should_log:
            logger.warning(
                "worker-truth finding record failed for code=%s dedupe_key=%s "
                "(the observed condition still stands; legacy behaviour is unaffected)",
                code.value,
                dedupe_key,
                exc_info=True,
            )


def emit(draft: EventDraft) -> WorkerEvent | None:
    """Append one draft through the installed store, then FOLD it.

    Returns the stored :class:`WorkerEvent` (callers that need the minted
    ``event_id`` as evidence for a later decision row use it), ``None`` when
    ingestion is off OR when the append failed.  Never raises ``Exception``.

    **The fold rides here because this is the one path from a hook to the store**
    (WP-ARCH phase 2, A1).  At phase 1's anchor ``Projector.project`` had no call
    site at all: the composition root built the projector and then handed the
    producer runtime everything except it, so the local was dropped and nothing
    ever folded an appended event.  ``status.transition`` rows are written by
    the fold and by nothing else, so without a driver the projection side of the
    log is simply empty — silently, which is what makes this seam worth a test.

    Three properties, each of which is a way this seam fails quietly:

    * **It runs after the append transaction commits**, which at this seam is
      automatic rather than a discipline: the fold sees what ``append`` has
      already returned, and ``append`` closes its ``immediate_transaction``
      before returning.  This matters because the projector's own
      ``_append_decision`` calls the store, so a fold placed inside an open
      transaction would nest one and fail every transition.
    * **Re-entry is bounded structurally.**  The fold is on EMITTED events, and
      the projector's own rows do not travel this way — ``_append_decision``
      writes through the event-store port directly, never through ``emit``.  A
      ``status.transition`` the fold produces therefore cannot re-enter it.  What
      the projector's ``decision_row`` branch guards is different and real: a
      decision row arriving from a PRODUCER, which ``server_decisions`` and
      ``legacy_egress`` both do.
    * **A fold failure never changes what this function returns.**  The append
      already succeeded and its ``event_id`` is a caller's evidence for a later
      decision row; swallowing the row because a diagnostic raised would turn a
      projector bug into a missing evidence chain.  So the fold has its own
      guard, outside the append's.
    """
    global _failure_count
    runtime = _runtime
    if runtime is None:
        return None
    try:
        stored = runtime.store.append(draft)
    except Exception:
        with _lock:
            _failure_count += 1
            should_log = _failure_count <= _MAX_LOGGED_FAILURES
        if should_log:
            logger.warning(
                "worker-truth ingest failed for terminal=%s kind=%s (ingestion "
                "continues; legacy behaviour is unaffected)",
                draft.terminal_id,
                draft.kind.value,
                exc_info=True,
            )
        return None

    _fold(runtime, stored)
    return stored


def _fold(runtime: ProducerRuntime, event: WorkerEvent) -> None:
    """Drive the projection for one appended event.  Never raises ``Exception``.

    Swallowing is the same promise :func:`emit` makes and the same one the
    store's ``CheckRunner`` is held to: this runs on the single path every
    producer takes, so a projector bug that escaped here would reach the status
    publish path and break AC11's no-behaviour-change claim.  The port's own
    contract says implementations must not raise; this is the belt to that
    suspenders.
    """
    folder = runtime.folder
    if folder is None:
        return
    try:
        folder.project(event)
    except Exception:  # noqa: BLE001 — a projection may never break an append
        logger.warning(
            "worker-truth fold failed for terminal=%s kind=%s (the row is stored; "
            "the projection is one event stale)",
            event.terminal_id,
            event.kind.value,
            exc_info=True,
        )
