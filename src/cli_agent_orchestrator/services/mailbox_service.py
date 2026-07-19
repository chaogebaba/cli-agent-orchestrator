"""Durable supervisor mailbox authority, publication, replay, and lifecycle."""

from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast, overload

from sqlalchemy import and_, exists, func, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients.database import (
    CallbackBarrierModel,
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
    _inbox_message_from_row,
    _insert_routed_inbox_row,
    _stamp_enqueue_generation,
    resolve_inbox_receiver,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType

logger = logging.getLogger(__name__)

MAILBOX_AUTHORITY_TIMEOUT_SECONDS = 30.0
_authority_locks: dict[tuple[str, str], threading.Lock] = {}
_authority_locks_guard = threading.Lock()


class MailboxDomainError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code.replace("_", " ")
        super().__init__(self.code)


class PublicationCleanupFailed(MailboxDomainError):
    def __init__(self, cause: Exception) -> None:
        self.cause_code = getattr(cause, "code", type(cause).__name__)
        self.cause_message = getattr(cause, "message", str(cause))
        super().__init__("publication_cleanup_failed", "mailbox publication cleanup failed")


@dataclass(frozen=True)
class MailboxClaim:
    session_name: str
    role: str
    mailbox_id: str | None
    observed_generation: int | None


def get_mailbox_authority_lock(session_name: str, role: str) -> threading.Lock:
    """Guarded, process-lifetime logical-key registry."""
    key = (session_name, role)
    with _authority_locks_guard:
        lock = _authority_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _authority_locks[key] = lock
        return lock


def claim_mailbox(session_name: str, role: str = "supervisor") -> MailboxClaim:
    with SessionLocal() as db:
        row: Any = (
            db.query(MailboxModel).filter_by(session_name=session_name, role=role).one_or_none()
        )
        return MailboxClaim(
            session_name, role, row.id if row else None, row.generation if row else None
        )


def _acquire(lock: threading.Lock) -> None:
    if not lock.acquire(timeout=MAILBOX_AUTHORITY_TIMEOUT_SECONDS):
        raise MailboxDomainError("mailbox_authority_timeout", "mailbox authority lock timed out")


def _address_ids(db: Any, mailbox_id: str) -> list[str]:
    return [
        row[0]
        for row in db.query(MailboxIncarnationModel.terminal_id)
        .filter_by(mailbox_id=mailbox_id)
        .all()
    ]


def _park_owner_generation(
    db: Any,
    row: Any,
    *,
    logical_default_generation: int | None = None,
) -> int:
    if type(row.enqueue_generation) is int:
        return int(row.enqueue_generation)
    if row.logical_receiver_id:
        incarnation = (
            db.query(MailboxIncarnationModel.generation)
            .filter(
                MailboxIncarnationModel.mailbox_id == row.logical_receiver_id,
                MailboxIncarnationModel.terminal_id == row.receiver_id,
            )
            .scalar()
        )
        if type(incarnation) is int:
            return int(incarnation)
        if type(logical_default_generation) is int:
            return logical_default_generation
        mailbox_generation = (
            db.query(MailboxModel.generation)
            .filter(MailboxModel.id == row.logical_receiver_id)
            .scalar()
        )
        if type(mailbox_generation) is int:
            return int(mailbox_generation)
    else:
        lifecycle_generation = (
            db.query(TerminalModel.lifecycle_generation)
            .filter(TerminalModel.id == row.receiver_id)
            .scalar()
        )
        if type(lifecycle_generation) is int:
            return int(lifecycle_generation)
    raise RuntimeError("parked_owner_generation_unavailable")


def _park_inbox_row(
    db: Any,
    row: Any,
    *,
    logical_default_generation: int | None = None,
) -> None:
    with db.no_autoflush:
        owner_receiver_id = row.receiver_id
        owner_generation = _park_owner_generation(
            db, row, logical_default_generation=logical_default_generation
        )
    row.owner_receiver_id = owner_receiver_id
    row.owner_generation = owner_generation
    row.status = MessageStatus.PARKED.value
    row.digested_into = None


def _bounded_utf8(value: str, limit: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value
    return data[:limit].decode("utf-8", errors="ignore")


def _digest_summary_lines(db: Any, rows: list[Any]) -> list[str]:
    """Build deterministic, sanitized summaries without interpreting old bodies."""
    candidates: list[str] = []
    for row in sorted(rows, key=lambda item: item.id):
        if row.orchestration_type == OrchestrationType.MAILBOX_DIGEST.value:
            count, first_id, last_id = (
                db.query(
                    func.count(InboxModel.id), func.min(InboxModel.id), func.max(InboxModel.id)
                )
                .filter(
                    InboxModel.digested_into == row.id,
                    InboxModel.orchestration_type != OrchestrationType.MAILBOX_DIGEST.value,
                )
                .one()
            )
            span = "none" if first_id is None else f"{first_id}-{last_id}"
            line = (
                f"superseded digest {row.id} (gen {row.enqueue_generation}, "
                f"{count} items, ids {span})"
            )
        else:
            clean = "".join(
                (
                    " "
                    if char in "\r\n\t"
                    else ("" if unicodedata.category(char) in {"Cc", "Cf"} else char)
                )
                for char in row.message
            )
            body = " ".join(clean.split())
            line = f"message {row.id} from {row.sender_id}: {body}"
        candidates.append(_bounded_utf8(line, 120))

    selected: list[str] = []
    used = 0
    for index, line in enumerate(candidates):
        cost = len(line.encode("utf-8")) + (1 if selected else 0)
        if used + cost > 2000:
            omitted = len(candidates) - index
            suffix = f"…(+{omitted} more)"
            while selected and used + 1 + len(suffix.encode("utf-8")) > 2000:
                removed = selected.pop()
                used -= len(removed.encode("utf-8")) + (1 if selected else 0)
                omitted += 1
                suffix = f"…(+{omitted} more)"
            selected.append(suffix)
            break
        selected.append(line)
        used += cost
    return selected


def publish_supervisor_incarnation(claim: MailboxClaim, terminal_id: str) -> dict[str, Any]:
    """CAS-publish one supervisor incarnation and atomically adopt its backlog."""
    from cli_agent_orchestrator.services.inbox_service import get_delivery_lock

    delivery_lock = get_delivery_lock(terminal_id)
    _acquire(delivery_lock)
    lock = get_mailbox_authority_lock(claim.session_name, claim.role)
    authority_acquired = False
    try:
        _acquire(lock)
        authority_acquired = True
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            existing = (
                db.query(MailboxIncarnationModel).filter_by(terminal_id=terminal_id).one_or_none()
            )
            if existing is not None:
                mailbox: Any = db.query(MailboxModel).filter_by(id=existing.mailbox_id).one()
                if (
                    mailbox.session_name == claim.session_name
                    and mailbox.role == claim.role
                    and mailbox.current_terminal_id == terminal_id
                    and claim.mailbox_id in {None, mailbox.id}
                ):
                    result = {
                        "mailbox_id": mailbox.id,
                        "generation": existing.generation,
                        "digest_message_id": None,
                        "adopted_receiver_ids": [terminal_id],
                    }
                    db.commit()
                    return result
                raise MailboxDomainError("mailbox_conflict", "mailbox publication conflict")

            now = datetime.now()
            if claim.mailbox_id is None:
                mailbox = MailboxModel(
                    id=f"mb_{uuid.uuid4().hex[:8]}",
                    session_name=claim.session_name,
                    role=claim.role,
                    current_terminal_id=terminal_id,
                    generation=1,
                    consumed_through_id=0,
                    created_at=now,
                    updated_at=now,
                )
                db.add(mailbox)
                try:
                    db.flush()
                except IntegrityError as exc:
                    raise MailboxDomainError(
                        "mailbox_conflict", "mailbox publication conflict"
                    ) from exc
                generation = 1
            else:
                generation = int(claim.observed_generation or 0) + 1
                changed = (
                    db.query(MailboxModel)
                    .filter(
                        MailboxModel.id == claim.mailbox_id,
                        MailboxModel.session_name == claim.session_name,
                        MailboxModel.role == claim.role,
                        MailboxModel.generation == claim.observed_generation,
                    )
                    .update(
                        {
                            MailboxModel.current_terminal_id: terminal_id,
                            MailboxModel.generation: generation,
                            MailboxModel.updated_at: now,
                        },
                        synchronize_session=False,
                    )
                )
                if changed != 1:
                    raise MailboxDomainError("mailbox_conflict", "mailbox publication conflict")
                mailbox = cast(Any, db.query(MailboxModel).filter_by(id=claim.mailbox_id).one())

            bumped = (
                db.query(TerminalModel)
                .filter(TerminalModel.id == terminal_id)
                .update(
                    {TerminalModel.lifecycle_generation: (TerminalModel.lifecycle_generation + 1)},
                    synchronize_session=False,
                )
            )
            # Direct mailbox-domain callers may publish without a terminal row;
            # no CAO pane coordinates exist in that seam, so there is no proof
            # generation to advance. Runtime publication always updates one row.
            if bumped not in {0, 1}:
                raise MailboxDomainError("mailbox_conflict", "terminal publication conflict")

            incarnation = MailboxIncarnationModel(
                mailbox_id=mailbox.id,
                generation=generation,
                terminal_id=terminal_id,
                published_at=now,
            )
            db.add(incarnation)
            db.flush()
            address_ids = _address_ids(db, mailbox.id)
            pending: list[Any] = (
                db.query(InboxModel)
                .filter(
                    InboxModel.status == MessageStatus.PENDING.value,
                    or_(
                        InboxModel.logical_receiver_id == mailbox.id,
                        and_(
                            InboxModel.logical_receiver_id.is_(None),
                            InboxModel.receiver_id.in_(address_ids),
                        ),
                    ),
                )
                .all()
            )
            wake_ids = [terminal_id]

            delivered = (
                db.query(InboxModel)
                .filter(
                    InboxModel.id > mailbox.consumed_through_id,
                    InboxModel.status == MessageStatus.DELIVERED.value,
                    InboxModel.orchestration_type != OrchestrationType.MAILBOX_DIGEST.value,
                    or_(
                        InboxModel.logical_receiver_id == mailbox.id,
                        InboxModel.receiver_id.in_(address_ids),
                    ),
                )
                .order_by(InboxModel.id.asc())
                .all()
            )
            historical_barriers: list[Any] = (
                db.query(CallbackBarrierModel)
                .filter(
                    CallbackBarrierModel.owner_mailbox_id == mailbox.id,
                    CallbackBarrierModel.owner_generation != generation,
                    CallbackBarrierModel.state == "OPEN",
                )
                .all()
            )
            historical_held: list[Any] = []
            for barrier in historical_barriers:
                barrier.state = "DIGESTED_REBIND"
                barrier.close_reason = "supervisor_rebind"
                barrier.fired_at = datetime.now(timezone.utc).replace(tzinfo=None)
                historical_held.extend(
                    db.query(InboxModel)
                    .filter(
                        InboxModel.barrier_id == barrier.id,
                        InboxModel.status == MessageStatus.HELD.value,
                    )
                    .order_by(InboxModel.id)
                    .all()
                )
            for row in [*pending, *historical_held]:
                _park_inbox_row(db, row, logical_default_generation=generation)
            if delivered:
                mailbox.consumed_through_id = max(
                    int(mailbox.consumed_through_id), max(int(row.id) for row in delivered)
                )
            cast(Any, incarnation).digest_message_id = None
            mailbox_id = str(mailbox.id)
            cursor = int(mailbox.consumed_through_id)
            parked_count = len(pending) + len(historical_held)
            db.commit()
            logger.info(
                "published supervisor mailbox %s generation %s cursor=%s parked=%s",
                mailbox_id,
                generation,
                cursor,
                parked_count,
            )
            return {
                "mailbox_id": mailbox.id,
                "generation": generation,
                "digest_message_id": None,
                "adopted_receiver_ids": wake_ids,
            }
    except IntegrityError as exc:
        raise MailboxDomainError("mailbox_conflict", "mailbox publication conflict") from exc
    finally:
        if authority_acquired:
            lock.release()
        delivery_lock.release()


@overload
def digest_stale_pending_for_terminal(terminal_id: str) -> int: ...


@overload
def digest_stale_pending_for_terminal(
    terminal_id: str, *, include_generation: Literal[True]
) -> tuple[int, int | None]: ...


def digest_stale_pending_for_terminal(
    terminal_id: str, *, include_generation: bool = False
) -> int | tuple[int, int | None]:
    """Atomically park old-generation pending rows without creating a push row."""

    def result(count: int, generation: int | None) -> int | tuple[int, int | None]:
        return (count, generation) if include_generation else count

    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        mailbox: Any = (
            db.query(MailboxModel).filter_by(current_terminal_id=terminal_id).one_or_none()
        )
        if mailbox is not None:
            generation = int(mailbox.generation)
            axis = InboxModel.logical_receiver_id == mailbox.id
        else:
            terminal = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
            if terminal is None:
                db.rollback()
                return result(0, None)
            generation = int(terminal.lifecycle_generation)
            axis = and_(
                InboxModel.logical_receiver_id.is_(None),
                InboxModel.receiver_id == terminal_id,
            )
        stale: list[Any] = (
            db.query(InboxModel)
            .filter(
                axis,
                InboxModel.status == MessageStatus.PENDING.value,
                or_(
                    InboxModel.enqueue_generation.is_(None),
                    InboxModel.enqueue_generation != generation,
                ),
            )
            .order_by(InboxModel.id)
            .all()
        )
        if not stale:
            db.rollback()
            return result(0, generation)
        for row in stale:
            _park_inbox_row(db, row, logical_default_generation=generation)
        db.commit()
        return result(len(stale), generation)


def create_logical_inbox_message(
    *,
    sender_id: str,
    mailbox_id: str,
    message: str,
    refresh_ingest: bool = False,
    orchestration_type: OrchestrationType = OrchestrationType.SEND_MESSAGE,
    dispatch_barrier: dict[str, Any] | None = None,
) -> InboxMessage:
    """Holder (d): resolve, guard, and insert one logical row under one authority."""
    with SessionLocal() as db:
        mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if mailbox is None:
            raise MailboxDomainError("unknown_mailbox", "unknown mailbox")
        key = (mailbox.session_name, mailbox.role)
    lock = get_mailbox_authority_lock(*key)
    _acquire(lock)
    try:
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        with stalled_callback_watchdog.callback_insert_guard(sender_id):
            with SessionLocal() as db:
                receiver_cache, logical_receiver_id, enqueue_generation = resolve_inbox_receiver(
                    db, mailbox_id
                )

                if receiver_cache and not receiver_cache.startswith("mb_"):
                    from cli_agent_orchestrator.services.terminal_guard_service import (
                        require_input_allowed,
                    )

                    require_input_allowed(receiver_cache, refresh_ingest=refresh_ingest)
                row = _insert_routed_inbox_row(
                    db,
                    sender_id=sender_id,
                    receiver_id=receiver_cache,
                    logical_receiver_id=logical_receiver_id,
                    message=message,
                    orchestration_type=orchestration_type,
                    dispatch_barrier=dispatch_barrier,
                )
                db.commit()
                db.refresh(row)
                result = _inbox_message_from_row(row)
                if result.status == MessageStatus.HELD:
                    stalled_callback_watchdog.record_callback_if_to_caller(
                        sender_id, logical_receiver_id or receiver_cache
                    )
                return result
    finally:
        lock.release()


def acquire_logical_sender_authority(
    mailbox_id: str,
    receiver_terminal_id: str,
    expected_generation: int,
) -> threading.Lock | None:
    """Acquire holder (b) and revalidate the current incarnation under it."""
    with SessionLocal() as db:
        mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if mailbox is None:
            return None
        key = (mailbox.session_name, mailbox.role)
    lock = get_mailbox_authority_lock(*key)
    _acquire(lock)
    with SessionLocal() as db:
        current: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if (
            current is None
            or current.current_terminal_id != receiver_terminal_id
            or current.generation != expected_generation
        ):
            lock.release()
            return None
    return lock


def _attempt_outcome(db: Any, message_id: int) -> str:
    attempt = (
        db.query(InboxDeliveryAttemptModel)
        .join(
            InboxDeliveryAttemptMemberModel,
            InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid,
        )
        .filter(InboxDeliveryAttemptMemberModel.message_id == message_id)
        .order_by(
            InboxDeliveryAttemptModel.started_at.desc(),
            InboxDeliveryAttemptModel.attempt_uuid.desc(),
        )
        .first()
    )
    if attempt is None:
        return "none"
    if attempt.outcome == "confirmed":
        try:
            evidence = json.loads(attempt.evidence or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        return (
            "confirmed_unverified"
            if evidence.get("kind") == "send_returned_unverified"
            else "confirmed_hit"
        )
    return (
        attempt.outcome
        if attempt.outcome in {"ambiguous", "failed", "deferred", "interrupted", "unresolved"}
        else "none"
    )


def list_messages(
    receiver: str,
    *,
    since: datetime | None = None,
    after_id: int | None = None,
    limit: int = 25,
    status: MessageStatus | None = None,
    generation: int | None = None,
    original_receiver_id: str | None = None,
    audit_browse: bool = False,
) -> dict[str, Any]:
    if (
        status == MessageStatus.PARKED
        and generation is None
        and original_receiver_id is None
        and not audit_browse
    ):
        raise MailboxDomainError(
            "parked_query_requires_incarnation",
            "parked queries require generation or original_receiver_id",
        )
    with SessionLocal() as db:
        query = db.query(InboxModel)
        if receiver.startswith("mb_"):
            mailbox = db.query(MailboxModel).filter_by(id=receiver).one_or_none()
            if mailbox is None:
                raise MailboxDomainError("unknown_mailbox", "unknown mailbox")
            addresses = _address_ids(db, receiver)
            query = query.filter(
                or_(
                    InboxModel.logical_receiver_id == receiver,
                    InboxModel.receiver_id.in_(addresses),
                )
            )
        else:
            query = query.filter(InboxModel.receiver_id == receiver)
        if since is not None:
            query = query.filter(InboxModel.created_at >= since)
        if after_id is not None:
            query = query.filter(InboxModel.id > after_id)
        if status is not None:
            query = query.filter(InboxModel.status == status.value)
        elif not audit_browse:
            query = query.filter(InboxModel.status != MessageStatus.PARKED.value)
        if generation is not None:
            query = query.filter(InboxModel.owner_generation == generation)
        if original_receiver_id is not None:
            query = query.filter(InboxModel.owner_receiver_id == original_receiver_id)
        rows: list[Any] = query.order_by(InboxModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "id": row.id,
                "sender_id": row.sender_id,
                "receiver_id": row.receiver_id,
                "logical_receiver_id": row.logical_receiver_id,
                "message": row.message,
                "orchestration_type": row.orchestration_type,
                "status": row.status,
                "failure_reason": row.failure_reason,
                "digested_into": row.digested_into,
                "enqueue_generation": row.enqueue_generation,
                "barrier_id": row.barrier_id,
                "barrier_member_key": row.barrier_member_key,
                "last_attempt_outcome": _attempt_outcome(db, row.id),
                "created_at": row.created_at.isoformat(),
            }
            if row.status == MessageStatus.PARKED.value:
                item["owner_receiver_id"] = row.owner_receiver_id
                item["owner_generation"] = row.owner_generation
                if row.logical_receiver_id:
                    authority = (
                        db.query(MailboxModel.generation, MailboxModel.current_terminal_id)
                        .filter(MailboxModel.id == row.logical_receiver_id)
                        .one_or_none()
                    )
                    item["dead_to_successor"] = bool(
                        authority is not None
                        and (
                            int(authority.generation) != row.owner_generation
                            or authority.current_terminal_id != row.owner_receiver_id
                        )
                    )
                else:
                    current_generation = (
                        db.query(TerminalModel.lifecycle_generation)
                        .filter(TerminalModel.id == row.owner_receiver_id)
                        .scalar()
                    )
                    item["dead_to_successor"] = bool(
                        type(current_generation) is int
                        and int(current_generation) != row.owner_generation
                    )
            items.append(item)
        return {
            "items": items,
            "next_after_id": rows[-1].id if has_more else None,
            "has_more": has_more,
        }


def ack_messages(terminal_id: str, up_to_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        mailbox: Any = (
            db.query(MailboxModel).filter_by(current_terminal_id=terminal_id).one_or_none()
        )
        if mailbox is None:
            raise MailboxDomainError("not_current_incarnation", "terminal is not current")
        key, observed_generation, mailbox_id = (
            (mailbox.session_name, mailbox.role),
            mailbox.generation,
            mailbox.id,
        )
    lock = get_mailbox_authority_lock(*key)
    _acquire(lock)
    try:
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            mailbox = cast(Any, db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none())
            if mailbox is None or mailbox.current_terminal_id != terminal_id:
                raise MailboxDomainError("not_current_incarnation", "terminal is not current")
            addresses = _address_ids(db, mailbox.id)
            high_water = (
                db.query(func.max(InboxModel.id))
                .filter(
                    or_(
                        InboxModel.logical_receiver_id == mailbox.id,
                        InboxModel.receiver_id.in_(addresses),
                    )
                )
                .scalar()
                or 0
            )
            if up_to_id > high_water:
                raise MailboxDomainError("ack_out_of_range", "ack exceeds visible high-water")
            if up_to_id < mailbox.consumed_through_id:
                db.commit()
                return {
                    "mailbox_id": mailbox.id,
                    "consumed_through_id": mailbox.consumed_through_id,
                    "changed": False,
                }
            changed = (
                db.query(MailboxModel)
                .filter(
                    MailboxModel.id == mailbox.id,
                    MailboxModel.generation == observed_generation,
                    MailboxModel.current_terminal_id == terminal_id,
                    MailboxModel.consumed_through_id <= up_to_id,
                )
                .update(
                    {
                        MailboxModel.consumed_through_id: up_to_id,
                        MailboxModel.updated_at: datetime.now(),
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                raise MailboxDomainError("not_current_incarnation", "terminal is not current")
            prior = mailbox.consumed_through_id
            db.commit()
            return {
                "mailbox_id": mailbox.id,
                "consumed_through_id": up_to_id,
                "changed": prior != up_to_id,
            }
    finally:
        lock.release()


def list_mailboxes() -> dict[str, Any]:
    with SessionLocal() as db:
        rows: list[Any] = (
            db.query(MailboxModel).order_by(MailboxModel.created_at, MailboxModel.id).all()
        )
        return {
            "items": [
                {
                    "id": row.id,
                    "session_name": row.session_name,
                    "role": row.role,
                    "current_terminal_id": row.current_terminal_id,
                    "generation": row.generation,
                    "consumed_through_id": row.consumed_through_id,
                    "incarnation_count": db.query(MailboxIncarnationModel)
                    .filter_by(mailbox_id=row.id)
                    .count(),
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]
        }


def delete_mailbox(mailbox_id: str) -> dict[str, int]:
    with SessionLocal() as db:
        mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if mailbox is None:
            raise MailboxDomainError("unknown_mailbox", "unknown mailbox")
        key = (mailbox.session_name, mailbox.role)
    lock = get_mailbox_authority_lock(*key)
    _acquire(lock)
    try:
        for attempt in range(3):
            try:
                with SessionLocal() as db:
                    db.execute(text("PRAGMA busy_timeout=1000"))
                    db.execute(text("BEGIN IMMEDIATE"))
                    mailbox = cast(
                        Any, db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
                    )
                    if mailbox is None:
                        raise MailboxDomainError("unknown_mailbox", "unknown mailbox")
                    if (
                        db.query(CallbackBarrierModel.id)
                        .filter(
                            CallbackBarrierModel.owner_mailbox_id == mailbox_id,
                            CallbackBarrierModel.state == "OPEN",
                        )
                        .first()
                        is not None
                    ):
                        raise MailboxDomainError("mailbox_busy", "mailbox has an open barrier")
                    if (
                        mailbox.current_terminal_id
                        and db.query(TerminalModel)
                        .filter_by(id=mailbox.current_terminal_id)
                        .first()
                        is not None
                    ):
                        raise MailboxDomainError("mailbox_in_use", "mailbox is in use")
                    logical_ids = [
                        row[0]
                        for row in db.query(InboxModel.id)
                        .filter_by(logical_receiver_id=mailbox_id)
                        .all()
                    ]
                    busy = (
                        db.query(InboxModel)
                        .filter(
                            InboxModel.id.in_(logical_ids),
                            InboxModel.status == MessageStatus.DELIVERING.value,
                        )
                        .first()
                    )
                    open_attempt = (
                        db.query(InboxDeliveryAttemptModel)
                        .join(
                            InboxDeliveryAttemptMemberModel,
                            InboxDeliveryAttemptMemberModel.attempt_uuid
                            == InboxDeliveryAttemptModel.attempt_uuid,
                        )
                        .filter(
                            InboxDeliveryAttemptMemberModel.message_id.in_(logical_ids),
                            InboxDeliveryAttemptModel.settled_at.is_(None),
                        )
                        .first()
                    )
                    if busy is not None or open_attempt is not None:
                        raise MailboxDomainError("mailbox_busy", "mailbox has an active delivery")
                    pending: list[Any] = (
                        db.query(InboxModel)
                        .filter_by(
                            logical_receiver_id=mailbox_id, status=MessageStatus.PENDING.value
                        )
                        .all()
                    )
                    notices = 0
                    settled: list[Any] = []
                    for row in pending:
                        changed = (
                            db.query(InboxModel)
                            .filter(
                                InboxModel.id == row.id,
                                InboxModel.logical_receiver_id == mailbox_id,
                                InboxModel.status == MessageStatus.PENDING.value,
                            )
                            .update(
                                {
                                    InboxModel.status: MessageStatus.DELIVERY_FAILED.value,
                                    InboxModel.failure_reason: "mailbox_deleted",
                                },
                                synchronize_session=False,
                            )
                        )
                        if changed != 1:
                            continue
                        settled.append(row)
                        try:
                            receiver, logical, _enqueue_generation = resolve_inbox_receiver(
                                db, row.sender_id
                            )
                        except ValueError:
                            continue
                        if (
                            logical is None
                            and db.query(TerminalModel.id)
                            .filter(TerminalModel.id == receiver)
                            .first()
                            is None
                        ):
                            continue
                        header = f"mailbox-delete receiver={mailbox_id} message={row.id}\n"
                        prior = (
                            db.query(InboxModel)
                            .filter(
                                InboxModel.sender_id == f"message-trace:{mailbox_id}",
                                InboxModel.receiver_id == receiver,
                                InboxModel.message.startswith(header),
                            )
                            .first()
                        )
                        if prior is None:
                            db.add(
                                InboxModel(
                                    **_stamp_enqueue_generation(
                                        db,
                                        {
                                            "sender_id": f"message-trace:{mailbox_id}",
                                            "receiver_id": receiver,
                                            "logical_receiver_id": logical,
                                            "message": header
                                            + "delivery failed because the logical mailbox "
                                            "was deleted",
                                            "orchestration_type": (
                                                OrchestrationType.SEND_MESSAGE.value
                                            ),
                                            "status": MessageStatus.PENDING.value,
                                        },
                                    )
                                )
                            )
                            notices += 1
                    if (
                        db.query(InboxModel)
                        .filter(
                            InboxModel.logical_receiver_id == mailbox_id,
                            InboxModel.status == MessageStatus.DELIVERING.value,
                        )
                        .first()
                        is not None
                    ):
                        raise MailboxDomainError("mailbox_busy", "mailbox has an active delivery")
                    db.query(MailboxIncarnationModel).filter_by(mailbox_id=mailbox_id).delete(
                        synchronize_session=False
                    )
                    deleted = (
                        db.query(MailboxModel)
                        .filter(
                            MailboxModel.id == mailbox_id,
                            ~exists().where(
                                and_(
                                    InboxModel.logical_receiver_id == mailbox_id,
                                    InboxModel.status == MessageStatus.DELIVERING.value,
                                )
                            ),
                        )
                        .delete(synchronize_session=False)
                    )
                    if deleted != 1:
                        raise MailboxDomainError("mailbox_busy", "mailbox became busy")
                    db.commit()
                    return {"settled_pending": len(settled), "notices_sent": notices}
            except OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 2:
                    raise MailboxDomainError("mailbox_busy", "mailbox database is busy") from exc
                time.sleep(1)
        raise MailboxDomainError("mailbox_busy", "mailbox database is busy")
    finally:
        lock.release()
