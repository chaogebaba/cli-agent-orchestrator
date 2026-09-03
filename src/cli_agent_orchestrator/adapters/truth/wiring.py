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
from cli_agent_orchestrator.core.ports import Clock, EventStore, FindingStore, StateStore

__all__ = [
    "ProducerRuntime",
    "emit",
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


def emit(draft: EventDraft) -> WorkerEvent | None:
    """Append one draft through the installed store.

    Returns the stored :class:`WorkerEvent` (callers that need the minted
    ``event_id`` as evidence for a later decision row use it), ``None`` when
    ingestion is off OR when the append failed.  Never raises ``Exception``.
    """
    global _failure_count
    runtime = _runtime
    if runtime is None:
        return None
    try:
        return runtime.store.append(draft)
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
