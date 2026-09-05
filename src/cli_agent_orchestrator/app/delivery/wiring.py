"""The delivery switch, and the one seam legacy reaches the queue through.

This is phase 1's ``adapters/truth/wiring.py`` applied to delivery, and for the
same two reasons.

**The switch is structural, not a per-call environment read.**  ``bootstrap.py``
owns ``CAO_DELIVERY_QUEUE``, reads it ONCE at boot, resolves it through D9's
guard, and installs a runtime here only for a position that writes rows.  With
nothing installed every hook point costs one module-global lookup and returns,
so AC-3a's off-arm criterion — zero ``delivery_msg`` rows — is true by
construction rather than by assertion: there is no code path from a hook to the
queue that does not pass the ``_runtime is None`` check below.  "The switch was
ignored" is therefore not expressible as a missing ``if`` in a hook; it would
have to be a deleted install guard in the composition root, where the A/B suite
sees it.

**Ingestion never breaks the thing it observes.**  §7a states it for this
sub-phase directly: the enqueue call sits behind the switch and does not raise
into its caller.  Every function here swallows ``Exception`` and returns.  The
stake is higher than phase 1's, because the thing being observed is message
delivery: a shadow write that could raise into ``_create_inbox_message_unfenced``
would turn a diagnostic into lost messages, which is the failure class the whole
phase exists to remove.

``BaseException`` is deliberately NOT swallowed: a ``KeyboardInterrupt`` or a
``CancelledError`` arriving inside a hook belongs to the caller's control flow.

The env var is deliberately NOT named here.  One spelling of a switch, in the one
module that reads it; a second definition in this layer is how a switch starts
meaning two different things — and ``bootstrap.py:18`` already warns about
exactly that.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue, LegacyOutcome, LegacyVeto
from cli_agent_orchestrator.app.delivery.mirror import MirrorWriter
from cli_agent_orchestrator.core.delivery import SwitchPosition
from cli_agent_orchestrator.core.ports import Clock, QueueStore

logger = logging.getLogger(__name__)

__all__ = [
    "DeliveryRuntime",
    "delivery_runtime",
    "install_delivery",
    "queue_enabled",
    "record_enqueue",
    "record_outcome",
    "record_veto",
    "reset_delivery",
]

#: How many hook failures are logged with a traceback before the logger falls
#: silent.  A queue that is broken is broken for every subsequent write, and a
#: warning per message would drown the log an operator needs to read.
_MAX_LOGGED_FAILURES = 3


@dataclass(frozen=True)
class DeliveryRuntime:
    """Everything a delivery hook needs, assembled by the composition root.

    ``position`` is the RESOLVED position, after D9's boot guard — not what the
    operator asked for.  Carrying it here rather than re-reading the environment
    is what makes the guard's decision the one the hooks obey; a hook that read
    the variable itself could act on a position the guard had already refused.
    """

    store: QueueStore
    clock: Clock
    position: SwitchPosition
    mirror: MirrorWriter


_lock = threading.Lock()
_runtime: DeliveryRuntime | None = None
_failure_count = 0


def install_delivery(runtime: DeliveryRuntime) -> None:
    """Arm the delivery hooks.  Called by ``bootstrap.py`` only."""
    global _runtime, _failure_count
    with _lock:
        _runtime = runtime
        _failure_count = 0


def reset_delivery() -> None:
    """Disarm the delivery hooks.

    Used at shutdown, on the migrator-failed path, and by every test that
    installed a fake.
    """
    global _runtime, _failure_count
    with _lock:
        _runtime = None
        _failure_count = 0


def delivery_runtime() -> DeliveryRuntime | None:
    """The installed runtime, or ``None`` when the queue is off."""
    return _runtime


def queue_enabled() -> bool:
    """True when a runtime is installed, i.e. the hooks write rows."""
    return _runtime is not None


def record_enqueue(fact: LegacyEnqueue) -> None:
    """Write the shadow row for one committed legacy insert.

    Returns ``None`` always, and callers in legacy code are written to ignore
    it.  Handing back the minted ``msg_id`` was considered and rejected: a legacy
    caller with a queue id in its hand is a caller that can come to depend on
    one, and sub-phase 3a's whole claim is that removing it changes nothing.
    """
    runtime = _runtime
    if runtime is None:
        return
    _guarded(lambda: runtime.mirror.enqueue(fact), "enqueue", str(fact.legacy_message_id))


def record_outcome(fact: LegacyOutcome) -> None:
    """Advance one shadow row from the legacy row's current status."""
    runtime = _runtime
    if runtime is None:
        return
    _guarded(lambda: runtime.mirror.observe(fact), "outcome", str(fact.legacy_message_id))


def record_veto(fact: LegacyVeto) -> None:
    """Record an injection the legacy path declined."""
    runtime = _runtime
    if runtime is None:
        return
    _guarded(
        lambda: runtime.mirror.observe_veto(fact),
        "veto",
        ",".join(str(mid) for mid in fact.legacy_message_ids),
    )


def _guarded(call: object, hook: str, subject: str) -> None:
    global _failure_count
    try:
        call()  # type: ignore[operator]
    except Exception:  # noqa: BLE001 — a shadow write may never break delivery
        with _lock:
            _failure_count += 1
            should_log = _failure_count <= _MAX_LOGGED_FAILURES
        if should_log:
            logger.warning(
                "delivery shadow %s failed for %s (the queue is observational in "
                "this sub-phase; legacy delivery is unaffected)",
                hook,
                subject,
                exc_info=True,
            )
