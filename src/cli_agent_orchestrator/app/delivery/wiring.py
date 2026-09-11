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

**A queue write never breaks the send it serves.**  §7a states it directly: the
enqueue call sits behind the switch and does not raise into its caller.  Every
function here swallows ``Exception`` and returns.  The stake is higher than
phase 1's, because what is at risk is message delivery: a write that could raise
into ``_create_inbox_message_unfenced`` would turn a queue fault into lost
messages, which is the failure class the whole phase exists to remove.

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

from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue
from cli_agent_orchestrator.core.delivery import (
    EnqueueDraft,
    MsgKind,
    QueueMode,
    SwitchPosition,
)
from cli_agent_orchestrator.core.ports import Clock, QueueStore
from cli_agent_orchestrator.core.timing import DELIVERY_DEDUP_WINDOW_S

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
    "reset_delivery",
    "write_through",
]


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


_lock = threading.Lock()
_runtime: DeliveryRuntime | None = None


def install_delivery(runtime: DeliveryRuntime) -> None:
    """Arm the delivery hooks.  Called by ``bootstrap.py`` only."""
    global _runtime
    with _lock:
        _runtime = runtime


def reset_delivery() -> None:
    """Disarm the delivery hooks.

    Used at shutdown, on the migrator-failed path, and by every test that
    installed a fake.
    """
    global _runtime
    with _lock:
        _runtime = None


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


def write_through(fact: LegacyEnqueue) -> tuple[int, str] | None:
    """Enqueue new traffic into the QUEUE instead of the legacy inbox (§6).

    Returns ``(surrogate_id, msg_id)``, or ``None`` when the queue does not own
    new traffic — in which case the caller writes its legacy row exactly as it
    does today.

    This is the flip §6 describes: "the legacy inbox goes read-only: it stops
    accepting inserts, existing rows drain through the old path, and new rows go
    to ``delivery_msg``".  It is the ONLY path from a legacy send into the queue.
    Sub-phase 3a had a second, observational one; it is retired (#738).

    Dual-write is excluded, and the reason is in the same paragraph: a
    dual-written row is a fifth carrier and would reproduce #506 inside the fix.

    **D13's carried effects that are NOT here happen in the caller**, because
    they operate on the barrier tables and the legacy predicate has to see them
    in its own transaction: the dispatch-barrier attach, the open-barrier
    association, the late-callback rewrite and the F578 supersession. What IS
    here is the F475 window check, because at ``on`` there is no legacy row for
    the legacy predicate to find.

    Never raises into the caller: a queue that cannot be written returns
    ``None``, and the caller writes its legacy row. That degrades to the
    pre-flip behaviour rather than losing the message.
    """
    runtime = _runtime
    if runtime is None or runtime.position is not SwitchPosition.ON:
        return None
    try:
        now = runtime.clock.now()
        duplicate = runtime.store.find_recent_duplicate(
            sender_id=fact.sender_id,
            receiver_id=fact.receiver_id,
            content_hash=fact.content_hash or "",
            window_s=DELIVERY_DEDUP_WINDOW_S,
            now=now,
            park_warm=fact.park_warm,
            barrier_id=fact.barrier_id,
        )
        if duplicate is not None and duplicate.legacy_message_id is not None:
            # A suppressed duplicate returns the EXISTING row, which is what the
            # legacy path returns today — never a fabricated id.
            return int(duplicate.legacy_message_id), duplicate.msg_id

        surrogate = runtime.store.next_surrogate_id()
        message = runtime.store.enqueue(
            EnqueueDraft(
                idempotency_key=f"live-inbox:{surrogate}",
                receiver_id=fact.receiver_id,
                sender_id=fact.sender_id,
                kind=MsgKind.CALLBACK if fact.is_callback else MsgKind.NOTE,
                payload=fact.message,
                mode=QueueMode.LIVE,
                expire_after_s=fact.expire_after_s,
                supersede_key=fact.supersede_key,
                content_hash=fact.content_hash,
                park_warm=fact.park_warm,
                barrier_id=fact.barrier_id,
                barrier_member_key=fact.barrier_member_key,
                enqueue_generation=fact.enqueue_generation,
                legacy_message_id=surrogate,
            )
        )
        return surrogate, message.msg_id
    except Exception:  # noqa: BLE001 — a queue write may never break a send
        logger.warning(
            "delivery write-through failed; the caller falls back to the legacy insert",
            exc_info=True,
        )
        return None


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
