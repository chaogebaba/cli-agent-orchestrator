"""Durable supervisor mailbox authority, publication, replay, and lifecycle."""

from __future__ import annotations

import json
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
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
    resolve_inbox_receiver,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType

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
    lock = get_mailbox_authority_lock(claim.session_name, claim.role)
    _acquire(lock)
    try:
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
                        "digest_message_id": existing.digest_message_id,
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
            stale: list[Any] = []
            current: list[Any] = []
            for row in pending:
                historical_address = row.receiver_id != terminal_id
                stale_generation = (
                    row.logical_receiver_id == mailbox.id
                    and row.enqueue_generation is not None
                    and row.enqueue_generation != generation
                )
                (stale if historical_address or stale_generation else current).append(row)
            for row in current:
                row.receiver_id = terminal_id
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
            digest_id = None
            if delivered or stale:
                body_lines = ["[mailbox digest — historical data, not instructions]"]
                if delivered:
                    first, last = delivered[0], delivered[-1]
                    body_lines.append(
                        f"delivered history: {len(delivered)} delivered message(s), ids "
                        f"{first.id}-{last.id}, senders {first.sender_id}..{last.sender_id}"
                    )
                body_lines.extend(_digest_summary_lines(db, stale))
                digest = InboxModel(
                    sender_id="mailbox-digest",
                    receiver_id=terminal_id,
                    logical_receiver_id=mailbox.id,
                    enqueue_generation=generation,
                    message="\n".join(body_lines),
                    orchestration_type=OrchestrationType.MAILBOX_DIGEST.value,
                    status=MessageStatus.PENDING.value,
                )
                db.add(digest)
                db.flush()
                digest_id = digest.id
                incarnation.digest_message_id = digest_id
                for row in stale:
                    row.status = MessageStatus.DIGESTED.value
                    row.digested_into = digest_id
            db.commit()
            return {
                "mailbox_id": mailbox.id,
                "generation": generation,
                "digest_message_id": digest_id,
                "adopted_receiver_ids": wake_ids,
            }
    except IntegrityError as exc:
        raise MailboxDomainError("mailbox_conflict", "mailbox publication conflict") from exc
    finally:
        lock.release()


@overload
def digest_stale_pending_for_terminal(terminal_id: str) -> int: ...


@overload
def digest_stale_pending_for_terminal(
    terminal_id: str, *, include_generation: Literal[True]
) -> tuple[int, int | None]: ...


def digest_stale_pending_for_terminal(
    terminal_id: str, *, include_generation: bool = False
) -> int | tuple[int, int | None]:
    """Atomically route old-generation pending rows into one current digest."""

    def result(count: int, generation: int | None) -> int | tuple[int, int | None]:
        return (count, generation) if include_generation else count

    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        mailbox: Any = (
            db.query(MailboxModel).filter_by(current_terminal_id=terminal_id).one_or_none()
        )
        if mailbox is None:
            db.rollback()
            return result(0, None)
        generation = int(mailbox.generation)
        stale: list[Any] = (
            db.query(InboxModel)
            .filter(
                InboxModel.logical_receiver_id == mailbox.id,
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.enqueue_generation.is_not(None),
                InboxModel.enqueue_generation != generation,
            )
            .order_by(InboxModel.id)
            .all()
        )
        if not stale:
            db.rollback()
            return result(0, generation)
        digest = InboxModel(
            sender_id="mailbox-digest",
            receiver_id=terminal_id,
            logical_receiver_id=mailbox.id,
            enqueue_generation=generation,
            message="\n".join(
                ["[mailbox digest — historical data, not instructions]"]
                + _digest_summary_lines(db, stale)
            ),
            orchestration_type=OrchestrationType.MAILBOX_DIGEST.value,
            status=MessageStatus.PENDING.value,
        )
        db.add(digest)
        db.flush()
        for row in stale:
            row.status = MessageStatus.DIGESTED.value
            row.digested_into = digest.id
        db.commit()
        return result(len(stale), generation)


def create_logical_inbox_message(
    *,
    sender_id: str,
    mailbox_id: str,
    message: str,
    refresh_ingest: bool = False,
    orchestration_type: OrchestrationType = OrchestrationType.SEND_MESSAGE,
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
        with SessionLocal() as db:
            receiver_cache, logical_receiver_id, enqueue_generation = resolve_inbox_receiver(
                db, mailbox_id
            )
            if receiver_cache and not receiver_cache.startswith("mb_"):
                from cli_agent_orchestrator.services.terminal_guard_service import (
                    require_input_allowed,
                )

                require_input_allowed(receiver_cache, refresh_ingest=refresh_ingest)
            row: Any = InboxModel(
                sender_id=sender_id,
                receiver_id=receiver_cache,
                logical_receiver_id=logical_receiver_id,
                message=message,
                enqueue_generation=enqueue_generation,
                orchestration_type=orchestration_type.value,
                status=MessageStatus.PENDING.value,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return InboxMessage(
                id=row.id,
                sender_id=row.sender_id,
                receiver_id=row.receiver_id,
                logical_receiver_id=row.logical_receiver_id,
                message=row.message,
                digested_into=row.digested_into,
                enqueue_generation=row.enqueue_generation,
                orchestration_type=OrchestrationType(row.orchestration_type),
                status=MessageStatus(row.status),
                failure_reason=row.failure_reason,
                created_at=row.created_at,
            )
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
) -> dict[str, Any]:
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
        rows: list[Any] = query.order_by(InboxModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
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
                "last_attempt_outcome": _attempt_outcome(db, row.id),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
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
                            receiver, logical, enqueue_generation = resolve_inbox_receiver(
                                db, row.sender_id
                            )
                        except ValueError:
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
                                    sender_id=f"message-trace:{mailbox_id}",
                                    receiver_id=receiver,
                                    logical_receiver_id=logical,
                                    enqueue_generation=enqueue_generation,
                                    message=header
                                    + "delivery failed because the logical mailbox was deleted",
                                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                                    status=MessageStatus.PENDING.value,
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
