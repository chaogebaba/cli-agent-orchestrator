"""The one legacy module that knows both halves (WP-ARCH phase 3a).

Lane C did the same thing for ``cao diag`` in phase 1: rather than five legacy
files each importing the new tree, ONE module holds the contact surface and the
real hook sites call it.  Two properties follow, and both are worth the extra
file.

* **The AC11-style importer list stays at one entry.**  ``clients/database.py``,
  ``services/mailbox_service.py`` and ``services/inbox_service.py`` gain a
  legacy-to-legacy call each; only this module imports ``app``.  A reviewer
  reading the diff for "what did phase 3a attach to the running server" has one
  file to read rather than a grep across the two largest legacy packages.
* **Every legacy query lives on the legacy side of the line.**  ``app`` may not
  import ``clients``, so the mirror writer cannot read the inbox table.  The
  collection happens here and the facts cross as plain values.

**Nothing in this module may raise into its caller.**  Every entry point is
called from inside a message-delivery path, and §7a is explicit that the enqueue
sits behind the switch and does not raise into the caller.  So each one opens
with the installed-runtime check, wraps its own database reads, and returns
``None``.  The stake is not the diagnostic — it is that a shadow write which
could raise into ``_create_inbox_message_unfenced`` would turn an observational
feature into lost messages, which is the failure class the phase exists to
remove.

The reads here are also the reason the hooks fire POST-COMMIT.  The queue's store
holds its own connection to the same SQLite file; writing from inside an open
legacy transaction would contend for the write lock that transaction holds, and
would leave a shadow row behind for a legacy row that then rolled back.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from cli_agent_orchestrator.app.delivery import wiring as delivery_wiring
from cli_agent_orchestrator.app.delivery.facts import (
    LegacyAttempt,
    LegacyEnqueue,
    LegacyOutcome,
    LegacyVeto,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MIRRORED_ORCHESTRATION_TYPES",
    "record_inbox_row",
    "observe_messages",
    "observe_veto",
]

#: Sub-phase 3a mirrors ``send_message`` traffic and nothing else.
#:
#: §11 records why, and it is the one place the blueprint diverges from #584's
#: scope line, deliberately: read at the fork's own source, ``assign`` rides
#: terminal creation through the deferred-init path and ``handoff`` posts to a
#: blocking run-step, so NEITHER writes an inbox row.  For those two,
#: write-through would not be a migration of an existing row but a NEW path
#: through terminal creation, reaching into the deferred-init failure family
#: phase 2 owns and covered by no acceptance criterion in this blueprint.
#:
#: Filtering on the orchestration type rather than on the call site is what makes
#: the scope checkable: the other enqueue paths (the watchdog auto-resume, the
#: barrier escalation, the several notices) reach the same insert seam, and a
#: hook that mirrored everything arriving there would silently widen 3a's scope
#: past what AC-3a measures.
MIRRORED_ORCHESTRATION_TYPES = frozenset({"send_message"})


def _aware(value: Any) -> datetime:
    """Read a legacy ``DateTime(timezone=True)`` column as an aware UTC stamp.

    The fork's SQLite rows come back naive on some paths, and the queue's models
    reject a naive datetime outright — deliberately, since the store compares
    timestamps as fixed-width strings.  Reading a naive value as UTC is the only
    interpretation that can be right here: every stored stamp in this database is
    UTC by convention, and rejecting one would drop a message from the mirror for
    a driver detail.
    """
    if not isinstance(value, datetime):
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def record_inbox_row(row: Any, *, logical_receiver_id: str | None = None) -> None:
    """Hook points 1 and 2 — one committed ``send_message`` inbox insert.

    ``row`` is the ``InboxModel`` instance, read AFTER its transaction committed.

    The receiver is the DURABLE mailbox id when the row has one and the terminal
    id otherwise.  §5 item 2 makes that the queue's addressing rule, and it is
    what closes #33: queue rows are addressed to the mailbox and are not
    generation-gated, so a fresh incarnation inherits its predecessor's pending
    rows with no rewrite at all.  The legacy row's ``enqueue_generation`` is
    carried anyway, recorded for diagnosis rather than read as a delivery filter.
    """
    if not delivery_wiring.queue_enabled():
        return
    try:
        orchestration = str(getattr(row, "orchestration_type", "") or "")
        if orchestration not in MIRRORED_ORCHESTRATION_TYPES:
            return
        mailbox_id = logical_receiver_id or getattr(row, "logical_receiver_id", None)
        receiver_id = str(mailbox_id or getattr(row, "receiver_id", "") or "")
        if not receiver_id:
            return
        dedup_key = getattr(row, "callback_dedup_key", None)
        fact = LegacyEnqueue(
            legacy_message_id=int(row.id),
            receiver_id=receiver_id,
            sender_id=str(getattr(row, "sender_id", "") or ""),
            message=str(getattr(row, "message", "") or ""),
            status=str(getattr(row, "status", "") or ""),
            created_at=_aware(getattr(row, "created_at", None)),
            orchestration_type=orchestration,
            # Legacy sets ``callback_dedup_key`` only on the F475-eligible path,
            # whose own guard requires the receiver to be the sender's recorded
            # caller. So its presence is the fork's own statement that this row
            # is a callback, rather than a second inference about identity.
            is_callback=bool(dedup_key),
            expire_after_s=getattr(row, "expire_after_s", None),
            supersede_key=getattr(row, "supersede_key", None),
            content_hash=dedup_key,
            park_warm=bool(getattr(row, "park_warm", False)),
            barrier_id=getattr(row, "barrier_id", None),
            barrier_member_key=getattr(row, "barrier_member_key", None),
            enqueue_generation=getattr(row, "enqueue_generation", None),
        )
    except Exception:  # noqa: BLE001 — the mirror may never break an insert
        logger.debug("delivery mirror could not read an inbox row", exc_info=True)
        return
    delivery_wiring.record_enqueue(fact)


def observe_messages(message_ids: Iterable[int]) -> None:
    """Hook points 3 and 4 — re-read these rows and advance their shadow state.

    Re-reading the CURRENT status rather than trusting the edge that triggered
    the call is the design decision that makes the mirror self-correcting.
    Several legacy writers can end a row — the delivered compare-and-set, the
    expiry sweep, the F578 supersession, three separate cancel sites — and they
    do not fire in a guaranteed order or from a single seam.  Hooking each would
    be six more contact points, and a missed one would be permanent.  Reading the
    status means the NEXT observation of that row, from any edge, still arrives
    at the truth, and §7a already accepts that a row whose outcome was never
    observed stays ``ready`` and is swept at the flip.
    """
    if not delivery_wiring.queue_enabled():
        return
    ids = [int(mid) for mid in message_ids]
    if not ids:
        return
    try:
        outcomes = _collect(ids)
    except Exception:  # noqa: BLE001 — the mirror may never break a settle
        logger.debug("delivery mirror could not read legacy outcomes", exc_info=True)
        return
    for outcome in outcomes:
        delivery_wiring.record_outcome(outcome)


def observe_veto(
    message_ids: Iterable[int], *, reason: str | None, gate_episode: str | None = None
) -> None:
    """Hook point 5 — an injection the legacy path declined before any attempt.

    Recorded rather than dropped, because a dropped veto is the difference
    between "we tried and were refused" and "nothing happened", and the second is
    what made #604 unreadable from the stored rows.
    """
    if not delivery_wiring.queue_enabled() or reason is None:
        return
    ids = tuple(int(mid) for mid in message_ids)
    if not ids:
        return
    delivery_wiring.record_veto(
        LegacyVeto(
            legacy_message_ids=ids,
            reason=str(reason),
            at=datetime.now(UTC),
            gate_episode=gate_episode,
        )
    )


def _collect(message_ids: list[int]) -> list[LegacyOutcome]:
    """Read each row's current status and its whole attempt history.

    One session, two queries, whatever the batch size — the settle path can hand
    us fifty ids at once and a per-id query there would be fifty round trips on
    the single writer §10 already names as a contention risk.

    The attempt ordinal is assigned HERE, by sorting a message's attempts by
    start time, because ``inbox_delivery_attempt`` has no ordinal of its own.
    Sorting by ``(started_at, attempt_uuid)`` rather than by time alone keeps the
    numbering stable when two attempts share a timestamp, which is what makes
    re-observing the same message a no-op rather than a second set of rows under
    different claim ids.
    """
    from cli_agent_orchestrator.clients.database import (  # local: legacy import graph
        InboxDeliveryAttemptMemberModel,
        InboxDeliveryAttemptModel,
        InboxModel,
        SessionLocal,
    )

    with SessionLocal() as db:
        rows = db.query(InboxModel).filter(InboxModel.id.in_(message_ids)).all()
        if not rows:
            return []
        attempt_rows = (
            db.query(InboxDeliveryAttemptMemberModel, InboxDeliveryAttemptModel)
            .join(
                InboxDeliveryAttemptModel,
                InboxDeliveryAttemptModel.attempt_uuid
                == InboxDeliveryAttemptMemberModel.attempt_uuid,
            )
            .filter(InboxDeliveryAttemptMemberModel.message_id.in_(message_ids))
            .all()
        )

        by_message: dict[int, list[Any]] = {}
        for member, attempt in attempt_rows:
            by_message.setdefault(int(member.message_id), []).append(attempt)

        outcomes: list[LegacyOutcome] = []
        for row in rows:
            attempts = sorted(
                by_message.get(int(row.id), []),
                key=lambda a: (_aware(a.started_at), str(a.attempt_uuid)),
            )
            outcomes.append(
                LegacyOutcome(
                    legacy_message_id=int(row.id),
                    status=str(row.status or ""),
                    failure_reason=(
                        str(row.failure_reason) if getattr(row, "failure_reason", None) else None
                    ),
                    attempts=tuple(
                        LegacyAttempt(
                            ordinal=index,
                            outcome=str(attempt.outcome or "unsettled"),
                            started_at=_aware(attempt.started_at),
                            carrier=str(attempt.provider or "legacy"),
                            reason=str(attempt.reason) if attempt.reason else None,
                            error=str(attempt.error) if attempt.error else None,
                        )
                        for index, attempt in enumerate(attempts, start=1)
                    ),
                )
            )
        return outcomes
