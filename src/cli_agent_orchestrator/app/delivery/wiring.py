"""The delivery switch, and the one seam legacy reaches the queue through.

This is phase 1's ``adapters/truth/wiring.py`` applied to delivery, and for the
same two reasons.

**The switch is structural, not a per-call environment read.**  ``bootstrap.py``
opens the queue store ONCE at boot and installs a runtime here; with nothing
installed every hook point costs one module-global lookup and returns.  Zero
``delivery_msg`` rows on an unarmed server is therefore true by construction
rather than by assertion: there is no code path from a hook to the queue that
does not pass the ``_runtime is None`` check below.  "The switch was ignored" is
not expressible as a missing ``if`` in a hook; it would have to be a deleted
install guard in the composition root, where the A/B suite sees it.

**There is one position, so the runtime carries none.**  ``CAO_DELIVERY_QUEUE``
had three while the queue and the legacy inbox were both carriers: ``off`` was
the pre-flip default under which legacy delivered, and ``drain`` was the way back
out of ``on`` — it served the rows already enqueued while new traffic returned to
legacy, so a rollback did not strand them in a table nothing read (#584).
WP-ARCH 3c deletes the legacy carriers, so there is nothing to roll back TO and
nothing for a second position to mean.  What used to be "is the resolved position
``on``?" is now "is a runtime installed?", which is the same question the null
check was already asking.

**A queue write never breaks the send it serves.**  §7a states it directly: the
enqueue call sits behind the switch and does not raise into its caller.  Every
function here swallows ``Exception`` and returns.  The stake is higher than
phase 1's, because what is at risk is message delivery: a write that could raise
into ``_create_inbox_message_unfenced`` would turn a queue fault into lost
messages, which is the failure class the whole phase exists to remove.

``BaseException`` is deliberately NOT swallowed: a ``KeyboardInterrupt`` or a
``CancelledError`` arriving inside a hook belongs to the caller's control flow.

No environment variable is named here, and there is no longer one to name.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue
from cli_agent_orchestrator.core.delivery import EnqueueDraft, MsgKind, QueueMode
from cli_agent_orchestrator.core.ports import Clock, QueueStore
from cli_agent_orchestrator.core.timing import DELIVERY_DEDUP_WINDOW_S

logger = logging.getLogger(__name__)

__all__ = [
    "DeliveryRuntime",
    "delivery_runtime",
    "install_delivery",
    "queue_owns_delivery",
    "queue_owns_new_traffic",
    "record_completion",
    "reset_delivery",
    "write_through",
]


@dataclass(frozen=True)
class DeliveryRuntime:
    """Everything a delivery hook needs, assembled by the composition root.

    Two fields, and until WP-ARCH 3c a third: the RESOLVED switch position, which
    the hooks obeyed so that no hook could act on a position D9's boot guard had
    already refused.  With the legacy carriers deleted the switch has one
    position, so the field could only ever hold one value; INSTALLED is now the
    whole of the state, and its presence or absence is carried by ``_runtime``
    itself rather than by a field inside it.
    """

    store: QueueStore
    clock: Clock


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


def queue_owns_delivery() -> bool:
    """True when a runtime is installed: the queue serves the seat.

    This was sub-phase 3b's mute, asked by K1 through K7 before each emitted, so
    the single-emitter property rested on the SWITCH.  In 3c it rests on the
    deletions — those emitters are gone — and this predicate is left as the one
    spelling of "the queue is the carrier" for the legacy sites that still ask.

    It was false at ``drain``, and that was the position's point: under ``drain``
    the tick finished delivering rows already enqueued while new traffic went
    back to the legacy inbox, so legacy had to keep emitting for those rows and
    muting it there would have left the new traffic with no carrier at all.  With
    the legacy carriers deleted there is no such arrangement to describe, so the
    predicate has nothing left to be false for except a queue that never started.
    """
    return _runtime is not None


def queue_owns_new_traffic() -> bool:
    """True when a runtime is installed: a new enqueue becomes a queue row.

    The same claim as :func:`queue_owns_delivery`, and the two are deliberately
    NOT merged into one name.  They were distinct questions while ``drain``
    existed — it accepted no NEW rows while still SERVING old ones, so exactly
    one of the two was true there — and the legacy call sites still ask the one
    they mean.  Keeping both spellings costs a line each and keeps each call site
    readable as the question it is actually asking.
    """
    return _runtime is not None


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
    if runtime is None:
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


def adopt_legacy_row(fact: LegacyEnqueue) -> str | None:
    """Enqueue one EXISTING legacy ``inbox`` row into the queue (WP-ARCH 3c).

    Returns the ``msg_id`` the row now lives under, or ``None`` when the queue
    does not own delivery.  The sibling of :func:`write_through`, and the
    differences from it are all forced by the row already existing:

    * ``legacy_message_id`` is the row's REAL id, not a fresh surrogate. That is
      what makes ``cao diag <msg_id>`` join back to the inbox row an operator is
      holding, and what lets the store recognise a re-adoption.
    * ``idempotency_key`` is derived from that id, so adopting the same row twice
      returns the SAME queue row instead of creating a second. The retire is the
      primary guard and this is the backstop for the crash window between them.
    * the F475 recent-duplicate window is NOT consulted. For new traffic that
      window suppresses a genuine double send; here the row IS the message, it
      has no counterpart yet, and "suppressing" it would drop the only copy —
      the exact loss adoption exists to prevent.

    Never raises into the caller: an adoption that cannot be written returns
    ``None``, the legacy row stays PENDING, and the next tick tries again.
    """
    runtime = _runtime
    if runtime is None:
        return None
    try:
        message = runtime.store.enqueue(
            EnqueueDraft(
                idempotency_key=f"adopted-inbox:{fact.legacy_message_id}",
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
                legacy_message_id=fact.legacy_message_id,
            )
        )
        return message.msg_id
    except Exception:  # noqa: BLE001 — the net may never break the tick
        logger.warning(
            "delivery adoption failed for legacy row %s; it stays pending for the next tick",
            fact.legacy_message_id,
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
    if runtime is None:
        return ()
    try:
        return runtime.store.cancel_on_complete(receiver_id, now=runtime.clock.now())
    except Exception:  # noqa: BLE001 — a cancel that cannot run must not break completion
        logger.warning("delivery: completion-cancel failed for %s", receiver_id, exc_info=True)
        return ()
