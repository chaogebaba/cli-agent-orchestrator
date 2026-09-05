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
from cli_agent_orchestrator.core.delivery import QueueMode, SwitchPosition
from cli_agent_orchestrator.core.ports import Clock, QueueStore

logger = logging.getLogger(__name__)

__all__ = [
    "DeliveryRuntime",
    "delivery_runtime",
    "install_delivery",
    "queue_enabled",
    "queue_owns_delivery",
    "queue_owns_new_traffic",
    "queue_position",
    "record_completion",
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


def queue_position() -> SwitchPosition:
    """The RESOLVED position, or ``off`` when nothing is installed.

    One reader for the whole legacy tree, so "is the queue serving this?" has one
    answer and not one per call site.  Legacy modules ask this rather than the
    environment: the boot guard can demote a requested position, and a hook that
    read the variable itself could act on a position the guard already refused.
    """
    runtime = _runtime
    return SwitchPosition.OFF if runtime is None else runtime.position


def queue_owns_delivery() -> bool:
    """True at ``on``: the queue serves the seat and D6's surfaces are MUTED.

    This is the whole of sub-phase 3b's muting, in one predicate.  K1 through K7
    are still present — they are deleted in 3c — and each asks this before it
    emits, so the single-emitter property in 3b rests on the SWITCH while in 3c
    it rests on the deletions.  Case 17 therefore tests the muting, and a second
    emitter in its ``on`` arm is a leaky mute rather than a missing deletion.

    ``drain`` is deliberately false.  Under ``drain`` the tick finishes
    delivering rows already enqueued while new traffic goes back to the legacy
    inbox (§6), so legacy must keep emitting for those rows; muting there would
    leave the new traffic with no carrier at all.
    """
    return queue_position() is SwitchPosition.ON


def queue_owns_new_traffic() -> bool:
    """True at ``on``: a new enqueue becomes a ``mode='live'`` queue row.

    False at ``drain``, which is the position's whole point — it accepts no new
    queue rows, so it empties on its own budget while new enqueues go to the
    legacy inbox (§6, D9).
    """
    return queue_position() is SwitchPosition.ON


def record_enqueue(fact: LegacyEnqueue) -> None:
    """Write the queue row for one committed legacy insert.

    ``mode`` follows the position: ``shadow`` observes, ``live`` is served by the
    tick.  At ``drain`` nothing is written at all, because ``drain`` accepts no
    new queue rows.

    Returns ``None`` always, and callers in legacy code are written to ignore it.
    Handing back the minted ``msg_id`` was considered and rejected: a legacy
    caller with a queue id in its hand is a caller that can come to depend on
    one.
    """
    runtime = _runtime
    if runtime is None:
        return
    position = runtime.position
    if position is SwitchPosition.DRAIN:
        return
    mode = QueueMode.LIVE if position is SwitchPosition.ON else QueueMode.SHADOW
    _guarded(
        lambda: runtime.mirror.enqueue(fact, mode=mode), "enqueue", str(fact.legacy_message_id)
    )


def record_outcome(fact: LegacyOutcome) -> None:
    """Advance one SHADOW row from the legacy row's current status.

    Inert once the queue owns delivery: at ``on`` the queue's own attempt rows
    and states are the authority (I5), and letting a legacy edge settle a live
    row would give one id two authorities — which is the defect D13 scopes the
    legacy ledger out for.
    """
    runtime = _runtime
    if runtime is None or runtime.position is SwitchPosition.ON:
        return
    _guarded(lambda: runtime.mirror.observe(fact), "outcome", str(fact.legacy_message_id))


def record_veto(fact: LegacyVeto) -> None:
    """Record an injection the legacy path declined.  Inert at ``on``, as above."""
    runtime = _runtime
    if runtime is None or runtime.position is SwitchPosition.ON:
        return
    _guarded(
        lambda: runtime.mirror.observe_veto(fact),
        "veto",
        ",".join(str(mid) for mid in fact.legacy_message_ids),
    )


def record_completion(receiver_id: str) -> tuple[str, ...]:
    """D8's completion-cancel, driven by the RECEIVER'S OWN completion.

    ``supersede_key`` handles the same-mailbox case at enqueue and does NOT reach
    #435, where the aged steer is addressed to the worker and the completion
    callback to the supervisor, so no newer row ever lands in the worker's
    mailbox.  This is the mechanism that does reach it.

    Evaluated once per completion EVENT rather than as a standing predicate, and
    over ``ready`` rows only: a steer already leased at completion still lands,
    and a steer reclaimed to ``ready`` after the completion is not retroactively
    cancelled.  Both limits are stated rather than hidden, and both stay
    diagnosable through ``cao diag <msg_id>``.
    """
    runtime = _runtime
    if runtime is None or runtime.position is not SwitchPosition.ON:
        return ()
    try:
        return runtime.store.cancel_on_complete(receiver_id, now=runtime.clock.now())
    except Exception:  # noqa: BLE001 — a cancel that cannot run must not break completion
        logger.warning("delivery: completion-cancel failed for %s", receiver_id, exc_info=True)
        return ()


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
