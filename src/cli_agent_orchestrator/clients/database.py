"""Minimal database client with only terminal metadata."""

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    event,
    exists,
    func,
    insert,
    or_,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, declarative_base, deferred, sessionmaker
from sqlalchemy.orm.exc import DetachedInstanceError, ObjectDeletedError, StaleDataError
from tzlocal import get_localzone

from cli_agent_orchestrator.constants import DATABASE_URL, DB_DIR, DEFAULT_PROVIDER
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import RecoveryState, TerminalStatus

logger = logging.getLogger(__name__)

Base: Any = declarative_base()
_ImmediateResult = TypeVar("_ImmediateResult")


@dataclass(frozen=True)
class ReadyBacklogObservation:
    receiver_id: str
    oldest_message_id: int
    oldest_pending_age_seconds: float
    has_open_delivering_attempt: bool
    attempt_fingerprint: tuple[int, datetime | None, datetime | None, datetime | None]


class NoticeInsertOutcome(str, Enum):
    INSERTED = "inserted"
    FAILED_BEFORE_COMMIT = "failed_before_commit"
    UNCERTAIN_COMMIT = "uncertain_commit"
    FAILED_AFTER_COMMIT = "failed_after_commit"


@dataclass(frozen=True)
class WatchdogInsertResult:
    kind: str
    message_id: int | None = None


TRANSCRIPT_BINDING_SOURCES = frozenset({"startup", "resume", "clear", "compact", "server_recovery"})
# Provider SessionStart-hook sources (api/main TranscriptBindingRequest allowlist).
TRANSCRIPT_HOOK_BINDING_SOURCES = TRANSCRIPT_BINDING_SOURCES - {"server_recovery"}

SEAM_ACTIVATION_CONSUMER_OPS = (
    "watchdog.cached_status",
    "watchdog.waiting_inbox_gate",
    "watchdog.ready_backlog_gate",
    "agent_step.status_reads",
    "delivery.admission_status",
    "watchdog.pane_classify",
    "delivery.fresh_probe",
    "delivery.park_identity_probe",
    "auto_responder.frame_classify",
)


def _utcnow() -> datetime:
    from cli_agent_orchestrator.sim.clock import active as _sim_clock_active

    clock = _sim_clock_active()
    if clock is not None:
        return clock.utcnow()
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Stamp naive-at-rest DB reads as UTC (our storage convention).

    Post-hotfix rows: naive-UTC-at-rest -> correct.
    Pre-hotfix rows: naive-local-at-rest -> <=4h skew, direction varies by
    consumer site (all harmless — see blueprint table). Ages out by 2026-08-24.

    MUST NOT be applied to barrier columns (fired_at, timeout_at, arrived_at)
    which were ALREADY naive-UTC via _barrier_now() -- they need no coercion.
    """
    if dt is None:
        return None
    # Convenience for non-DB callers passing already-aware values; DB reads
    # NEVER take this branch (SQLAlchemy-sqlite always returns tzinfo=None).
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


class TerminalModel(Base):
    """SQLAlchemy model for terminal metadata only."""

    __tablename__ = "terminals"

    id = Column(String, primary_key=True)  # "abc123ef"
    tmux_session = Column(String, nullable=False)  # "cao-session-name"
    tmux_window = Column(String, nullable=False)  # "window-name"
    provider = Column(String, nullable=False)  # "kiro_cli", "claude_code"
    agent_profile = Column(String)  # "developer", "reviewer" (optional)
    working_directory = Column(String, nullable=True)  # launch-time cwd (optional)
    allowed_tools = Column(String, nullable=True)  # JSON-encoded list of CAO tool names
    shell_command = Column(String, nullable=True)  # shell process name captured before kiro launch
    caller_id = Column(String, nullable=True)  # terminal that created this one (callback target)
    lifecycle = Column(String, nullable=False, default="ephemeral", server_default="ephemeral")
    reparented_from = Column(String, nullable=True)
    instance_id = Column(String, nullable=True)
    caller_mailbox_id = deferred(Column(String, nullable=True))
    auth_token = Column(String, nullable=True, unique=True)
    provider_session_id = Column(String, nullable=True)
    recovery_state = Column(String, nullable=True)
    recovery_error = Column(String, nullable=True)
    recovery_updated_at = Column(DateTime(timezone=True), nullable=True)
    fallback_terminal_id = Column(String, nullable=True)
    init_state = Column(String, nullable=False, default="ready", server_default="ready")
    init_started_at = Column(DateTime(timezone=True), nullable=True)
    init_owner_epoch = Column(String, nullable=True)
    init_failure_token = Column(String, nullable=True, unique=True)
    init_deadline_s = Column(Float, nullable=True)
    lifecycle_generation = Column(Integer, nullable=False, default=0, server_default="0")
    engine = Column(String, nullable=True)  # resolved Kiro engine; NULL for legacy/non-Kiro rows
    # Ordered, general-to-specific array of strings (JSON-encoded), e.g.
    # '["tenant_1", "project_5", "folder_12"]'. CAO only does ordered-prefix
    # matching (list_siblings); consumers own what the levels mean (#432).
    group = Column(Text, nullable=True)
    # Free-form JSON (JSON-encoded dict), consumer-defined, no fixed schema.
    # Python attribute is ``metadata_json`` (not ``metadata``) because
    # SQLAlchemy's declarative Base reserves ``.metadata`` for the schema
    # MetaData object on every mapped class; the DB column itself is still
    # literally named "metadata" per #432's design.
    metadata_json = Column("metadata", Text, nullable=True)
    # Dedicated CAO-owned worktree authority record (F121). JSON-encoded dict
    # with keys: repo_root, worktree_path, expected_branch, terminal_id,
    # provisioned_at. Written once at provision time via server-internal
    # accessors; never exposed through the worker-reachable metadata API.
    # Protected by a SQLite BEFORE UPDATE trigger (immutable once non-NULL).
    worktree_info = Column("worktree_info", Text, nullable=True)
    # F175: Dedicated high-water columns for server-internal dedup state.
    # Stored outside metadata_json so supervisor update_metadata (whole-dict
    # REPLACE) cannot clobber them.
    last_notified_inbox_id = Column(Integer, nullable=True)
    last_doorbell_row_id = Column(Integer, nullable=True)
    last_active = Column(DateTime(timezone=True), default=_utcnow)
    # F127: resolved model string persisted post-initialize
    resolved_model = Column(String, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('ephemeral','sticky')",
            name="ck_terminals_lifecycle",
        ),
        CheckConstraint(
            "init_state IN ('init_pending','ready','init_failed_notified',"
            "'init_failed_caller_gone')",
            name="ck_terminals_init_state",
        ),
        CheckConstraint(
            "init_state != 'init_pending' OR "
            "(init_started_at IS NOT NULL AND init_owner_epoch IS NOT NULL AND "
            "length(init_owner_epoch) = 36 AND init_owner_epoch = lower(init_owner_epoch) AND "
            "substr(init_owner_epoch,9,1) = '-' AND substr(init_owner_epoch,14,1) = '-' AND "
            "substr(init_owner_epoch,19,1) = '-' AND substr(init_owner_epoch,24,1) = '-' AND "
            "init_deadline_s IS NOT NULL AND init_deadline_s >= 1.0 AND "
            "init_deadline_s <= 600.0 AND init_deadline_s = init_deadline_s)",
            name="ck_terminals_pending_init_fields",
        ),
        CheckConstraint(
            "init_failure_token IS NULL OR (length(init_failure_token) = 36 AND "
            "init_failure_token = lower(init_failure_token) AND "
            "substr(init_failure_token,9,1) = '-' AND "
            "substr(init_failure_token,14,1) = '-' AND "
            "substr(init_failure_token,19,1) = '-' AND "
            "substr(init_failure_token,24,1) = '-')",
            name="ck_terminals_init_failure_token_uuid",
        ),
    )


class ProviderSessionModel(Base):
    __tablename__ = "provider_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    session_uuid = Column(Text, nullable=False)
    cwd = Column(Text, nullable=False)
    agent_profile = Column(Text, nullable=False)
    git_sha = Column(Text, nullable=True)
    dirty_hashes = Column(Text, nullable=False, default="{}", server_default="{}")
    digest_head = Column(Text, nullable=True)
    retained_persona_home = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    kind = Column(Text, nullable=False, default="base", server_default="base")
    source_terminal_id = Column(Text, nullable=True)
    session_name = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready','superseded','retired')",
            name="ck_provider_sessions_status",
        ),
        CheckConstraint("kind IN ('base','anchor')", name="ck_provider_sessions_kind"),
        Index("uq_provider_sessions_ready", "name", unique=True, sqlite_where=(status == "ready")),
    )


class WarmIntentModel(Base):
    __tablename__ = "warm_intents"
    intent_id = Column(String, primary_key=True)
    worker_terminal_id = Column(String, nullable=False, unique=True)
    replaces_worker_terminal_id = Column(String, nullable=True)
    session_name = Column(String, nullable=False, index=True)
    worker_profile = Column(String, nullable=False)
    parent_base_name = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TeardownIntentModel(Base):
    """Durable proof that CAO issued a Herdr workspace close."""

    __tablename__ = "herdr_teardown_intents"
    workspace_id = Column(String, primary_key=True)
    session_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    state = Column(String, nullable=False)
    generation = Column(Integer, nullable=False, default=1)
    __table_args__ = (
        CheckConstraint(
            "state IN ('issuing','issued_ok','void','consumed')",
            name="ck_herdr_teardown_intent_state",
        ),
    )


class WorkspaceMapModel(Base):
    """Durable Herdr workspace-to-session routing with retirement history."""

    __tablename__ = "herdr_workspace_map"
    workspace_id = Column(String, primary_key=True)
    session_name = Column(String, nullable=False, index=True)
    active = Column(Boolean, nullable=False, default=True, server_default="1")
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SessionEpochModel(Base):
    __tablename__ = "session_epochs"
    session_name = Column(String, primary_key=True)
    count = Column(Integer, nullable=False, default=0)
    last_epoch_at = Column(DateTime(timezone=True), nullable=True)


class MailboxModel(Base):
    """Durable logical receiver for one session role."""

    __tablename__ = "mailboxes"
    id = Column(String, primary_key=True)
    session_name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    current_terminal_id = Column(String, nullable=True)
    generation = Column(Integer, nullable=False, default=1, server_default="1")
    consumed_through_id = Column(Integer, nullable=False, default=0, server_default="0")
    schema_version = Column(Integer, nullable=False, default=1, server_default="1")
    # F136: callback notification cursor and path authority
    callback_notified_through_id = Column(Integer, nullable=True)
    cc_inbox_path = Column(String, nullable=True)
    cc_inbox_path_version = Column(Integer, nullable=False, default=0, server_default="0")
    # F476: Wake recovery columns (D4)
    wake_notified_at = Column(DateTime(timezone=True), nullable=True)
    wake_streak = Column(Integer, nullable=False, default=0, server_default="0")
    wake_notified_id = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    __table_args__ = (UniqueConstraint("session_name", "role", name="uq_mailbox_session_role"),)


class CallbackReplayQueueModel(Base):
    """F136: Durable replay set for old IDs that become newly eligible."""

    __tablename__ = "callback_replay_queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mailbox_id = Column(String, ForeignKey("mailboxes.id"), nullable=False)
    inbox_row_id = Column(Integer, ForeignKey("inbox.id"), nullable=False)
    queued_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    __table_args__ = (
        UniqueConstraint("mailbox_id", "inbox_row_id", name="uq_replay_mailbox_row"),
        Index("ix_callback_replay_mailbox_row", "mailbox_id", "inbox_row_id"),
    )


class MailboxIncarnationModel(Base):
    """Append-only terminal incarnations of a logical mailbox."""

    __tablename__ = "mailbox_incarnations"
    mailbox_id = Column(String, ForeignKey("mailboxes.id"), primary_key=True, nullable=False)
    generation = Column(Integer, primary_key=True, nullable=False)
    terminal_id = Column(String, nullable=False, unique=True)
    published_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    digest_message_id = Column(Integer, nullable=True)


class TranscriptBindingModel(Base):
    """Append-only Claude transcript binding epochs reported by SessionStart."""

    __tablename__ = "transcript_bindings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    terminal_id = Column(String, nullable=False)
    session_id = Column(String, nullable=False)
    transcript_path = Column(Text, nullable=False)
    inode = Column(Integer, nullable=True)
    source = Column(String, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    __table_args__ = (
        Index("ix_transcript_bindings_terminal_received", "terminal_id", "received_at", "id"),
    )


class InboxModel(Base):
    """SQLAlchemy model for inbox messages."""

    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sender_id = Column(String, nullable=False)
    receiver_id = Column(String, nullable=False)
    logical_receiver_id = deferred(Column(String, nullable=True))
    message = Column(String, nullable=False)
    orchestration_type = Column(
        String,
        nullable=False,
        default=OrchestrationType.SEND_MESSAGE.value,
        server_default=OrchestrationType.SEND_MESSAGE.value,
    )
    status = Column(String, nullable=False)  # MessageStatus enum value
    park_warm = Column(Boolean, nullable=True, default=False)
    failure_reason = Column(Text, nullable=True)
    digested_into = deferred(Column(Integer, nullable=True))
    enqueue_generation = deferred(Column(Integer, nullable=True))
    owner_receiver_id = deferred(Column(String, nullable=True))
    owner_generation = deferred(Column(Integer, nullable=True))
    barrier_id = deferred(Column(Integer, nullable=True))
    barrier_member_key = deferred(Column(String, nullable=True))
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (Index("ix_inbox_sender_receiver", "sender_id", "receiver_id"),)


class CallbackBarrierModel(Base):
    """One supervisor-owned callback aggregation episode."""

    __tablename__ = "callback_barrier"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_mailbox_id = Column(String, nullable=True)
    owner_terminal_id = Column(String, nullable=True)
    owner_generation = Column(Integer, nullable=False)
    label = Column(String, nullable=False)
    state = Column(String, nullable=False, default="OPEN", server_default="OPEN")
    close_reason = Column(Text, nullable=True)
    timeout_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    fired_at = Column(DateTime(timezone=True), nullable=True)
    combined_message_id = Column(Integer, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "((owner_mailbox_id IS NOT NULL AND owner_terminal_id IS NULL) OR "
            "(owner_mailbox_id IS NULL AND owner_terminal_id IS NOT NULL)) "
            "AND owner_generation IS NOT NULL",
            name="ck_callback_barrier_exactly_one_owner",
        ),
        CheckConstraint(
            "state IN ('OPEN','FIRED_COMPLETE','FIRED_TIMEOUT','CANCELLED','DIGESTED_REBIND')",
            name="ck_callback_barrier_state",
        ),
        Index(
            "uq_callback_barrier_open_mailbox",
            "owner_mailbox_id",
            "owner_generation",
            "label",
            unique=True,
            sqlite_where=text("state = 'OPEN' AND owner_mailbox_id IS NOT NULL"),
        ),
        Index(
            "uq_callback_barrier_open_terminal",
            "owner_terminal_id",
            "owner_generation",
            "label",
            unique=True,
            sqlite_where=text("state = 'OPEN' AND owner_terminal_id IS NOT NULL"),
        ),
        Index(
            "uq_callback_barrier_combined_message",
            "combined_message_id",
            unique=True,
            sqlite_where=text("combined_message_id IS NOT NULL"),
        ),
    )


class CallbackBarrierMemberModel(Base):
    """One terminal incarnation expected to answer a callback barrier."""

    __tablename__ = "callback_barrier_member"

    id = Column(Integer, primary_key=True, autoincrement=True)
    barrier_id = Column(Integer, ForeignKey("callback_barrier.id"), nullable=False)
    member_key = Column(String, nullable=False)
    position = Column(Integer, nullable=False)
    terminal_id = Column(String, nullable=False)
    lifecycle_generation = Column(Integer, nullable=False)
    state = Column(String, nullable=False, default="AWAITING", server_default="AWAITING")
    failure_class = Column(String, nullable=True)
    message_id = Column(
        Integer,
        ForeignKey("inbox.id", ondelete="SET NULL"),
        nullable=True,
    )
    arrived_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "state IN ('AWAITING','ARRIVED','FAILED','GONE')",
            name="ck_callback_barrier_member_state",
        ),
        UniqueConstraint("barrier_id", "member_key", name="uq_callback_barrier_member_key"),
        UniqueConstraint(
            "barrier_id",
            "terminal_id",
            "lifecycle_generation",
            name="uq_callback_barrier_member_binding",
        ),
    )


class InboxDeliveryAttemptModel(Base):
    __tablename__ = "inbox_delivery_attempt"
    attempt_uuid = Column(String, primary_key=True)
    receiver_terminal_id = Column(String, nullable=False, index=True)
    provider = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    payload_hash = Column(String, nullable=False)
    payload_length = Column(Integer, nullable=False)
    pre_input_gen = Column(Integer, nullable=True)
    pre_status_gen = Column(Integer, nullable=True)
    settled_status_gen = Column(Integer, nullable=True)
    evidence = Column(Text, nullable=False, default="{}", server_default="{}")
    count = Column(Integer, nullable=False, default=1, server_default="1")
    last_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    prior_attempt_uuid = Column(String, nullable=True)
    sender_id = Column(String, nullable=False)
    orchestration_type = Column(String, nullable=False)
    __table_args__ = (
        Index(
            "uq_inbox_deferred_attempt",
            "receiver_terminal_id",
            "payload_hash",
            "reason",
            unique=True,
            sqlite_where=(outcome == "deferred"),
        ),
    )


class InboxDeliveryAttemptMemberModel(Base):
    __tablename__ = "inbox_delivery_attempt_member"
    attempt_uuid = Column(String, primary_key=True)
    message_id = Column(Integer, primary_key=True, index=True)
    position = Column(Integer, nullable=False)


class InboxMessageTraceEventModel(Base):
    """Append-only message trace evidence shared by delivery and compact recovery."""

    __tablename__ = "inbox_message_trace_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("inbox.id"), nullable=False)
    kind = Column(String, nullable=False)
    # FX191 D7: three nullable indexed columns for convergent-delivery trace
    phase = Column(String, nullable=True)
    decision = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    __table_args__ = (
        Index("ix_inbox_message_trace_event_message_id", "message_id"),
        Index(
            "ix_inbox_message_trace_event_kind_created_message",
            "kind",
            "created_at",
            "message_id",
        ),
        Index(
            "ix_inbox_message_trace_event_fx191_phase_decision",
            "phase",
            "decision",
            sqlite_where=text("phase IS NOT NULL"),
        ),
    )


class DeliveryObligationModel(Base):
    """FX191 D1: one delivery obligation per supervisor-directed message."""

    __tablename__ = "delivery_obligation"

    inbox_row_id = Column(Integer, ForeignKey("inbox.id"), primary_key=True)
    mailbox_id = Column(String, ForeignKey("mailboxes.id"), nullable=False)
    state = Column(String, nullable=False, default="OPEN", server_default="OPEN")
    accepted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    first_attempt_at = Column(DateTime(timezone=True), nullable=True)
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, default=_utcnow)
    terminal_reason = Column(String, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "state IN ('OPEN','ACKED','ESCALATED','SETTLED_TARGET_DEAD')",
            name="ck_delivery_obligation_state",
        ),
        Index(
            "ix_delivery_obligation_open_next",
            "state",
            "next_attempt_at",
            sqlite_where=text("state = 'OPEN'"),
        ),
        Index("ix_delivery_obligation_mailbox", "mailbox_id"),
    )


# ---------------------------------------------------------------------------
# F413: ORM listeners — obligation + sentinel + doorbell structurally unbypassable
# ---------------------------------------------------------------------------

_F413_DOORBELL_STASH_KEY = "_f413_doorbell"
_F413_DOORBELL_SNAPSHOT_KEY = "_f413_doorbell_snapshot"


def _f413_row_qualifies(status: str, logical_receiver_id: str | None) -> bool:
    """Single predicate: row is PENDING and directed at a supervisor mailbox.

    The supervisor-mailbox check is deferred to the caller that has a connection
    (after_insert) or a session (D7b helper) — this only checks the column-level
    preconditions.
    """
    return status == MessageStatus.PENDING.value and logical_receiver_id is not None


@event.listens_for(InboxModel, "after_insert")
def _f413_after_insert(mapper: Any, connection: Any, target: Any) -> None:
    """D1/D2: Create delivery obligation + trace event + touch sentinel on qualifying insert.

    Uses Core-only inserts on the same connection — NO Session operations.
    """
    status = target.status
    logical_receiver_id = target.logical_receiver_id
    if not _f413_row_qualifies(status, logical_receiver_id):
        return

    # D2: Core select to check supervisor-mailbox
    result = connection.execute(
        select(MailboxModel.__table__.c.id).where(
            MailboxModel.__table__.c.id == logical_receiver_id,
            MailboxModel.__table__.c.role == "supervisor",
        )
    )
    if result.first() is None:
        return

    # Create obligation via Core insert (idempotent — skip if already exists)
    now = _utcnow()
    row_id = int(target.id)
    existing_obl = connection.execute(
        select(DeliveryObligationModel.__table__.c.inbox_row_id).where(
            DeliveryObligationModel.__table__.c.inbox_row_id == row_id,
        )
    ).first()
    if existing_obl is not None:
        _touch_supervisor_pending_flag()
        # D3: Stash doorbell even on idempotent hit
        from sqlalchemy.orm import Session as _Session

        sess = _Session.object_session(target)
        if sess is not None:
            stash = sess.info.setdefault(_F413_DOORBELL_STASH_KEY, [])
            preview = (target.message or "").split("\n", 1)[0]
            stash.append((logical_receiver_id, row_id, preview[:120]))
        return
    connection.execute(
        insert(DeliveryObligationModel.__table__).values(
            inbox_row_id=row_id,
            mailbox_id=logical_receiver_id,
            state="OPEN",
            accepted_at=now,
            next_attempt_at=now,
            attempts=0,
        )
    )
    # Create trace event via Core insert
    connection.execute(
        insert(InboxMessageTraceEventModel.__table__).values(
            message_id=row_id,
            kind="fx191.accept",
            phase="accept",
            decision="proceed",
            reason=None,
            payload="{}",
            created_at=now,
        )
    )
    # Touch sentinel (filesystem, best-effort)
    _touch_supervisor_pending_flag()

    # D3: Stash doorbell tuple for after_commit drain
    from sqlalchemy.orm import Session as _Session

    sess = _Session.object_session(target)
    if sess is not None:
        stash = sess.info.setdefault(_F413_DOORBELL_STASH_KEY, [])
        preview = (target.message or "").split("\n", 1)[0]
        stash.append(
            (
                target.receiver_id,
                row_id,
                (target.sender_id or "")[:8],
                preview,
            )
        )


def _f413_after_commit(session: Any) -> None:
    """D3: Drain doorbell stash after commit, with nested-tx guard."""
    if session.in_nested_transaction():
        return
    stash = session.info.pop(_F413_DOORBELL_STASH_KEY, [])
    # Clear snapshot too — commit succeeded, no longer needed
    session.info.pop(_F413_DOORBELL_SNAPSHOT_KEY, None)
    if not stash:
        return
    for terminal_id, row_id, sender_short, preview in stash:
        try:
            from cli_agent_orchestrator.services.ws_doorbell import push_doorbell_frame_sync

            push_doorbell_frame_sync(terminal_id, row_id, sender_short, preview)
        except Exception:
            pass  # advisory-only
        try:
            from cli_agent_orchestrator.services.inbox_service import request_delivery

            request_delivery(terminal_id)
        except Exception:
            pass  # best-effort


def _f413_after_rollback(session: Any) -> None:
    """D3: On rollback, restore snapshot (nested) or clear (outer).

    Register after_rollback ONLY (not after_soft_rollback — redundant in SA 2.0.x).
    On nested-tx rollback: restore the stash to the snapshot taken before that
    nested block (so earlier successful barriers' doorbells survive).
    On outer rollback: full clear.
    """
    if session.in_nested_transaction():
        # Still inside an outer transaction — this was a savepoint rollback.
        # Restore stash to the snapshot (entries from before this nested block).
        snapshot = session.info.get(_F413_DOORBELL_SNAPSHOT_KEY, [])
        session.info[_F413_DOORBELL_STASH_KEY] = list(snapshot)
    else:
        # Outer rollback — discard everything
        session.info.pop(_F413_DOORBELL_STASH_KEY, None)
        session.info.pop(_F413_DOORBELL_SNAPSHOT_KEY, None)


def _f413_after_begin(session: Any, transaction: Any, connection: Any) -> None:
    """Snapshot the doorbell stash before each nested transaction begins.

    after_begin fires for both outer and nested transactions. We only snapshot
    when entering a nested (savepoint) — so a subsequent rollback can restore
    to this point without losing earlier successful barriers' stash entries.
    """
    if transaction.nested:
        current = session.info.get(_F413_DOORBELL_STASH_KEY, [])
        session.info[_F413_DOORBELL_SNAPSHOT_KEY] = list(current)


def _f413_qualify_and_create(db: Any, rows: list[Any]) -> None:
    """D7b: Create obligations + sentinel for HELD→PENDING flipped rows that qualify.

    Called after bulk status-flip paths that bypass mapper events (barrier-cancel,
    terminal-reap). Uses the single predicate _f413_row_qualifies, plus a DB check
    for supervisor mailbox. Creates obligations/sentinel ONLY — NO doorbell stash
    (barrier-CANCEL wake rides F136-D17 request_delivery, terminal-reap rides the
    delivery sweep).
    """
    if not rows:
        return
    for row_status, row_id, logical_receiver_id in rows:
        if not _f413_row_qualifies(row_status, logical_receiver_id):
            continue
        if not _is_supervisor_mailbox_id(db, logical_receiver_id):
            continue
        now = _utcnow()
        db.add(
            DeliveryObligationModel(
                inbox_row_id=row_id,
                mailbox_id=logical_receiver_id,
                state="OPEN",
                accepted_at=now,
                next_attempt_at=now,
            )
        )
        db.add(
            InboxMessageTraceEventModel(
                message_id=row_id,
                kind="fx191.accept",
                phase="accept",
                decision="proceed",
                reason=None,
                payload={},
            )
        )
        _touch_supervisor_pending_flag()
    db.flush()


# D6: Registration at model-definition time (runs on import of this module)
# Note: session-level listeners registered after SessionLocal creation below.
# The mapper-level @event.listens_for(InboxModel, "after_insert") is already active.


class AuthorityPinModel(Base):
    """Append-only authority-file hashes scoped to one worker task."""

    __tablename__ = "authority_pin"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_key = Column(String, nullable=False)
    file_path = Column(Text, nullable=False)
    sha256 = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    registered_by = Column(String, nullable=False)
    frozen = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    __table_args__ = (
        UniqueConstraint(
            "task_key",
            "file_path",
            "version",
            name="uq_authority_pin_task_file_version",
        ),
        Index(
            "ix_authority_pin_task_file_version_desc",
            "task_key",
            "file_path",
            version.desc(),
        ),
    )


class MemoryMetadataModel(Base):
    """SQLAlchemy model for memory metadata (Phase 2 U1).

    SQLite is the source of truth for metadata queries; wiki markdown
    files remain the content store. Each row corresponds to exactly one
    wiki file on disk.
    """

    __tablename__ = "memory_metadata"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String, nullable=False)
    memory_type = Column(String, nullable=False)
    scope = Column(String, nullable=False)
    scope_id = Column(String, nullable=True)
    file_path = Column(String, nullable=False)
    tags = Column(String, nullable=False, default="")
    source_provider = Column(String, nullable=True)
    source_terminal_id = Column(String, nullable=True)
    token_estimate = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    # 3-factor scoring. ``access_count`` feeds the usage factor;
    # ``last_accessed_at`` backs a server-side rate-limit on increments. NOT
    # NULL DEFAULT 0 so existing rows read as "never recalled" without a
    # backfill. Migrated onto existing DBs by ``_migrate_add_access_count``.
    access_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_accessed_at = Column(DateTime(timezone=True), nullable=True, default=None)
    # LLM wiki compilation. NULL = never LLM-compiled (pre-existing rows, or
    # every compile attempt fell back to append). Non-NULL = UTC timestamp of
    # the last successful compile.
    last_compiled_at = Column(DateTime(timezone=True), nullable=True, default=None)
    # Comma-separated sanitised keys of cross-referenced articles. NULL =
    # never computed (pre-existing rows or LLM error). ``""`` = computed, no
    # related found (success — distinct from NULL to avoid endless retries).
    # Practical max ≤ 256 bytes (3 keys × 60 chars + 2 commas). The CHECK
    # constraint applies on FRESH databases only — existing DBs rely on the
    # parse-side cap in ``_parse_related_keys``.
    related_keys = Column(Text, nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("key", "scope", "scope_id", name="uq_memory_key_scope"),
        CheckConstraint(
            "related_keys IS NULL OR length(related_keys) < 1024",
            name="ck_related_keys_length",
        ),
    )


# Relationship-store sentinel: ``memory_relationships.scope_id`` is NOT NULL and
# stores this value for global/federated scope. SQLite treats ``NULL != NULL``
# in a UNIQUE index, so a nullable scope_id would make the dedup index (and thus
# ``INSERT ... ON CONFLICT``) inert for global scope — silently duplicating
# exactly the edges hardest to notice. Storing a NOT-NULL sentinel keeps the
# dedup tuple total. ``""`` cannot collide with a real sanitized scope_id
# (``MemoryService._sanitize_key``/``_sanitize_scope_id`` never yield empty —
# the latter returns ``"unknown"``). This sentinel is scoped to the
# ``memory_relationships`` table ONLY; ``MemoryMetadataModel.scope_id`` remains
# genuinely nullable (stores real NULL for global), so cross-table endpoint
# checks against it use logical ``None`` + ``.is_(None)`` (see the relationship
# service), never this sentinel.
RELATIONSHIP_SCOPE_ID_SENTINEL = ""


class MemoryRelationshipModel(Base):
    """SQLAlchemy model for a typed, durable memory relationship edge (issue #511).

    The authoritative relationship store that replaces the lossy
    ``memory_metadata.related_keys`` text column. One row per typed edge between
    two memory keys in the same ``(scope, scope_id)``. Written and read ONLY
    through ``MemoryRelationshipService`` — no other component issues SQL against
    this table (FR-2.1 single-boundary invariant).

    ``related_keys`` on ``MemoryMetadataModel`` is retained UNCHANGED as the
    compiler's computation-state marker (NULL = never computed/error, ``""`` =
    computed-empty) and is NOT modified or retired by this table (retirement is a
    separate, later change gated on a loss-free proof).
    """

    __tablename__ = "memory_relationships"

    # Application-generated uuid4 string PK, matching ``MemoryMetadataModel.id``
    # (str(uuid4())). API-stable identifier exposed in mutation responses.
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    scope = Column(String, nullable=False)
    # NOT NULL: sentinel RELATIONSHIP_SCOPE_ID_SENTINEL ("") for global/federated
    # so the dedup UNIQUE index is total (see the sentinel comment above).
    scope_id = Column(String, nullable=False)
    source_key = Column(String, nullable=False)
    target_key = Column(String, nullable=False)
    # Closed taxonomy reusing the graph EdgeType values.
    type = Column(String, nullable=False)  # relates_to | contradiction | supersedes
    # compiler | wiki_lint | human | legacy_related_keys | external_import(reserved)
    origin = Column(String, nullable=False)
    # active | proposal | rejected | superseded | deleted (auditable soft-delete)
    status = Column(String, nullable=False, default="active")
    # Optional evidence metadata. NULL = no evidence (NEVER fabricated / coerced
    # to 0); a stored value is a validated REAL in [0, 1].
    confidence = Column(Float, nullable=True, default=None)
    # Optional ordering hint (e.g. legacy related_keys position). NULL if none.
    rank = Column(Integer, nullable=True, default=None)
    # Bounded JSON blob. NULL if none; the CHECK caps FRESH DBs, the service
    # caps existing DBs (mirrors the ck_related_keys_length precedent).
    attributes_json = Column(Text, nullable=True, default=None)
    # The source memory's updated_at at write time; basis for staleness
    # detection (an edge is stale when this predates the source's current
    # updated_at). NULL when unknown.
    source_updated_at = Column(DateTime(timezone=True), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        # Dedup: differing type or origin coexist as distinct rows (multi-edge +
        # provenance-aware coexistence); a repeat of the same tuple upserts.
        # Every column is non-NULL (scope_id sentinel), so the index and
        # ON CONFLICT fire for ALL scopes including global.
        UniqueConstraint(
            "scope",
            "scope_id",
            "source_key",
            "target_key",
            "type",
            "origin",
            name="uq_memory_rel",
        ),
        # FRESH-DB CHECKs only (SQLite cannot retro-add a CHECK); the service
        # validates confidence range and attributes size on existing DBs.
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_memory_rel_confidence_range",
        ),
        CheckConstraint(
            "attributes_json IS NULL OR length(attributes_json) <= 2048",
            name="ck_memory_rel_attributes_size",
        ),
    )


class ProjectAliasModel(Base):
    """SQLAlchemy model for project identity aliases (Phase 2.5 U6).

    Maps historical/alternate project identifiers (cwd hashes, manual labels)
    to a canonical ``project_id`` so memory recall survives directory rename
    and worktree layouts.
    """

    __tablename__ = "project_aliases"

    # ``alias`` is the sole primary key: an alias maps to exactly one canonical
    # project_id, so reverse lookups (get_project_id_by_alias) are stable. A
    # cwd-hash first resolved via an override and later via its git remote
    # upserts the same row rather than creating a second, ambiguous mapping.
    alias = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)  # "git_remote" | "cwd_hash" | "manual"
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class WorkflowOutcomeModel(Base):
    """SQLAlchemy model for workflow outcome records (self-learning Phase 1).

    One row per reported outcome of a unit of agent work (a workflow step,
    a package conversion, a review round). Outcomes are the raw signal the
    retrospector agent distills into memory lessons — they carry short
    labels and notes, never transcripts or file contents.
    """

    __tablename__ = "workflow_outcomes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_name = Column(String, nullable=False)
    workflow_name = Column(String, nullable=True)  # optional grouping label
    task_label = Column(String, nullable=False)  # e.g. "convert package X"
    agent_profile = Column(String, nullable=True)  # profile that did the work
    source_terminal_id = Column(String, nullable=True)
    success = Column(Boolean, nullable=False)
    score = Column(Integer, nullable=True)  # optional 0-100 metric
    friction_notes = Column(Text, nullable=False, default="")  # short, content-free
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class FlowModel(Base):
    """SQLAlchemy model for flow metadata."""

    __tablename__ = "flows"

    name = Column(String, primary_key=True)
    file_path = Column(String, nullable=False)
    schedule = Column(String, nullable=False)
    agent_profile = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    script = Column(String, nullable=True)
    last_run = Column(DateTime(timezone=True), nullable=True)
    next_run = Column(DateTime(timezone=True), nullable=True)
    enabled = Column(Boolean, default=True)


class SeamActivationModel(Base):
    """Durable authority switch for one closed consumer operation."""

    __tablename__ = "seam_activation"

    consumer_op = Column(Text, primary_key=True)
    active_authority = Column(Text, nullable=False, default="legacy", server_default="legacy")
    accepted_version = Column(Integer, nullable=False, default=0, server_default="0")
    active_version = Column(Integer, nullable=False, default=0, server_default="0")
    rollback_version = Column(Integer, nullable=False, default=0, server_default="0")
    acceptance_token = Column(Text, nullable=True)
    evidence_ref = Column(Text, nullable=True)
    tombstoned_legacy = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "active_authority IN ('legacy','receiver_state')",
            name="ck_seam_activation_authority",
        ),
        CheckConstraint(
            "accepted_version >= active_version AND accepted_version <= active_version + 1",
            name="ck_seam_activation_versions",
        ),
        CheckConstraint(
            "active_version >= rollback_version",
            name="ck_seam_activation_rollback_version",
        ),
        CheckConstraint(
            "NOT (active_authority='receiver_state' AND active_version=0)",
            name="ck_seam_activation_active_version",
        ),
        CheckConstraint(
            "NOT (accepted_version > active_version AND acceptance_token IS NULL)",
            name="ck_seam_activation_acceptance_token",
        ),
        CheckConstraint(
            "tombstoned_legacy IN (0,1)",
            name="ck_seam_activation_tombstoned",
        ),
    )


class SeamActivationEvidenceModel(Base):
    """Append-only evidence history preventing duplicate acceptance replay."""

    __tablename__ = "seam_activation_evidence"

    consumer_op = Column(Text, primary_key=True)
    evidence_ref = Column(Text, primary_key=True)
    acceptance_token = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "consumer_op",
            "evidence_ref",
            name="uq_seam_activation_evidence_ref",
        ),
    )


class SeamParityModel(Base):
    """Current parity window for one status-read consumer seam."""

    __tablename__ = "seam_parity"

    consumer_op = Column(Text, primary_key=True)
    build_id = Column(Text, nullable=False)
    phase = Column(Text, nullable=False)
    window_started_at = Column(Text, nullable=False)
    window_nonce = Column(Text, nullable=False)
    clean_samples = Column(Integer, nullable=False, default=0, server_default="0")
    mismatch_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_sample_at = Column(Text, nullable=True)
    last_mismatch_detail = Column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "phase IN ('collecting','confirming','done')",
            name="ck_seam_parity_phase",
        ),
    )


class SeamParityMismatchModel(Base):
    """Append-only status parity mismatch history."""

    __tablename__ = "seam_parity_mismatch"

    id = Column(Integer, primary_key=True, autoincrement=True)
    consumer_op = Column(Text, nullable=False)
    build_id = Column(Text, nullable=False)
    window_nonce = Column(Text, nullable=False)
    phase = Column(Text, nullable=False)
    acted_answer = Column(Text, nullable=False)
    shadow_answer = Column(Text, nullable=False)
    detail = Column(Text, nullable=True)
    source = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "source IN ('live','poison_recovery')",
            name="ck_seam_parity_mismatch_source",
        ),
        UniqueConstraint(
            "consumer_op",
            "window_nonce",
            "created_at",
            "source",
            name="uq_seam_parity_mismatch_replay",
        ),
    )


# --- F138: process incarnation and orphan reconciliation models ---------------


class ProcessIncarnationModel(Base):
    """Immutable per-launch incarnation row. Survives terminal deletion."""

    __tablename__ = "process_incarnations"

    id = Column(String, primary_key=True)
    terminal_id = Column(String, nullable=False)
    terminal_generation = Column(Integer, nullable=False)
    token = Column(String, nullable=False, unique=True)
    token_hash = Column(String, nullable=False, unique=True)
    owner_uid = Column(Integer, nullable=False)
    provider = Column(String, nullable=False)
    pane_pid = Column(Integer, nullable=True)
    pane_start_ticks = Column(Integer, nullable=True)
    state = Column(String, nullable=False)
    issuance_ticks = Column(Integer, nullable=True)
    issuance_boot_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("terminal_id", "terminal_generation", name="uq_incarnation_term_gen"),
        CheckConstraint(
            "state IN ('launching','active','reconcile_pending','reconciled','abandoned')",
            name="ck_incarnation_state",
        ),
    )


class OrphanReconcileJobModel(Base):
    """Durable leased orphan-reconciliation job."""

    __tablename__ = "orphan_reconcile_jobs"

    id = Column(String, primary_key=True)
    incarnation_id = Column(String, nullable=False, unique=True)
    terminal_id = Column(String, nullable=False)
    terminal_generation = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    attempt = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    gone_observed_at = Column(DateTime(timezone=True), nullable=False)
    source = Column(String, nullable=False)
    last_result_json = Column(Text, nullable=True)
    notified_failure_code = Column(String, nullable=True)
    notify_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','leased','retry_wait','succeeded','attention_required')",
            name="ck_reconcile_job_state",
        ),
    )


# ─── F218-a / F219 dead-supervisor safety models ────────────────────────────


class PaneExitTombstoneModel(Base):
    """F218-a: forensic record of a confirmed-gone pane, written BEFORE any signal."""

    __tablename__ = "pane_exit_tombstones"

    id = Column(String, primary_key=True)
    incarnation_id = Column(String, nullable=False, unique=True)  # duplicate-proof
    terminal_id = Column(String, nullable=False)
    terminal_generation = Column(Integer, nullable=False)
    token_hash = Column(String, nullable=True)  # NEVER the raw token
    session_name = Column(String, nullable=False)
    session_incarnation = Column(String, nullable=False)  # D15: NOT NULL, total derivation

    # --- classifier ---
    scope = Column(String, nullable=False)  # window_gone|session_gone|unknown
    scope_hint = Column(String, nullable=True)  # from the error string (D1) — hint only
    scope_evidence_json = Column(Text, nullable=True)  # probe rc/stderr/samples, verbatim
    confirm_samples = Column(Integer, nullable=True)

    # --- former identity ---
    window_name = Column(String, nullable=True)
    window_index = Column(Integer, nullable=True)
    pane_id = Column(String, nullable=True)  # tmux %N
    sibling_windows_json = Column(Text, nullable=True)  # [] proves "session had nothing left"

    # --- process identity (forensic only — never addressable, Do-NOT 8) ---
    pane_pid = Column(Integer, nullable=True)
    pane_start_ticks = Column(Integer, nullable=True)
    pane_pgid = Column(Integer, nullable=True)
    issuance_boot_id = Column(String, nullable=True)
    matched_pids_json = Column(Text, nullable=True)  # scan taken AT tombstone time, pre-TERM
    cgroup_path = Column(String, nullable=True)
    systemd_scope = Column(String, nullable=True)
    proc_status = Column(String, nullable=False)  # ok|unavailable|denied|not_applicable
    proc_reason = Column(String, nullable=True)

    # --- exit evidence (D10: normally absent, and says so) ---
    exit_code = Column(Integer, nullable=True)
    term_signal = Column(Integer, nullable=True)
    exit_evidence_status = Column(String, nullable=False)  # default unavailable_no_waiter
    exit_evidence_reason = Column(String, nullable=True)

    # --- memory / pressure snapshot ---
    memory_events_json = Column(Text, nullable=True)
    memory_current = Column(Integer, nullable=True)
    memory_peak = Column(Integer, nullable=True)
    memory_max = Column(String, nullable=True)  # N1: RAW cgroup file content, verbatim
    memory_pressure_json = Column(Text, nullable=True)
    memory_status = Column(String, nullable=False)
    memory_reason = Column(String, nullable=True)

    # --- provenance ---
    writer = Column(String, nullable=False)  # observation|job
    schema_version = Column(Integer, nullable=False)  # 1
    complete = Column(Boolean, nullable=False)
    incomplete_reason = Column(String, nullable=True)  # D11 degenerate path
    observed_at = Column(DateTime(timezone=True), nullable=False)
    written_at = Column(DateTime(timezone=True), nullable=False)
    server_pid = Column(Integer, nullable=True)
    server_boot_id = Column(String, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "scope IN ('window_gone','session_gone','unknown')",
            name="ck_tombstone_scope",
        ),
        CheckConstraint(
            "writer IN ('observation','job')",
            name="ck_tombstone_writer",
        ),
        Index("ix_tombstone_session", "session_name", "session_incarnation"),
        Index("ix_tombstone_terminal", "terminal_id", "terminal_generation"),
    )


class SessionDegradationModel(Base):
    """F218-a: exactly-once session degradation marker + alarm ledger."""

    __tablename__ = "session_degradations"

    id = Column(String, primary_key=True)
    session_name = Column(String, nullable=False)
    session_incarnation = Column(String, nullable=False)  # D15 — NOT NULL is load-bearing
    cause = Column(String, nullable=False)
    tombstone_id = Column(String, nullable=True)
    terminal_id = Column(String, nullable=True)
    detail_json = Column(Text, nullable=True)
    alarm_rungs_json = Column(Text, nullable=True)  # per-rung attempted/ok/failed + reason
    alarm_delivered = Column(Boolean, nullable=False)
    suppressed_by_teardown = Column(Boolean, nullable=False, default=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)  # R5 re-surface gate
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "session_name",
            "session_incarnation",
            "cause",
            name="uq_session_degradation",
        ),
        CheckConstraint(
            "cause IN ('supervisor_window_gone','session_gone','pane_unreachable_scope_unknown')",
            name="ck_degradation_cause",
        ),
    )


class F218TeardownIntentModel(Base):
    """D16: durable 'this teardown is deliberate' marker. Written BEFORE tmux is touched."""

    __tablename__ = "f218_teardown_intents"

    id = Column(String, primary_key=True)
    scope_kind = Column(String, nullable=False)  # session | terminal
    scope_key = Column(String, nullable=False)  # session_name | terminal_id
    requested_by = Column(String, nullable=True)  # caller_id, when known
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)  # created_at + TTL
    __table_args__ = (
        UniqueConstraint("scope_kind", "scope_key", name="uq_f218_teardown_intent"),
        CheckConstraint(
            "scope_kind IN ('session','terminal')",
            name="ck_f218_teardown_scope_kind",
        ),
    )


def _ensure_db_dir() -> None:
    """Create the DB dir owner-only (0o700).

    The DB stores sensitive data (workflow spec_snapshot carries full prompt
    bodies + inputs_json), so the dir is owner-only — the same posture as
    claude_code prompt files (0o600) and the audit log (0o700/0o600). mkdir's
    mode is ignored when the dir already exists (exist_ok) and is masked by
    umask on creation — the chmod enforces 0o700 in both cases, best-effort.
    """
    DB_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(DB_DIR, 0o700)
    except OSError as e:
        logger.warning(f"Could not restrict DB dir permissions on {DB_DIR}: {e}")


# Module-level singletons
_ensure_db_dir()


# ── F334 (#190): Production DB fence ──────────────────────────────────────────
# If PYTEST_CURRENT_TEST is set (pytest injects this automatically), refuse to
# bind the real production DATABASE_FILE unless explicitly overridden. This
# makes it impossible for an import-order regression to silently point the test
# suite at the production database.
def _fence_production_db() -> None:
    """Raise if tests are about to bind the production database."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return  # not in a test — no fence
    if os.environ.get("CAO_ALLOW_PROD_DB_IN_TESTS") == "1":
        return  # explicit override — operator knows what they're doing
    import pwd

    # Use the REAL user home from passwd (immune to HOME env override)
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    prod_db_dir = (real_home / ".aws" / "cli-agent-orchestrator" / "db").resolve()
    resolved_db = DB_DIR.resolve()
    if resolved_db == prod_db_dir:
        raise RuntimeError(
            f"F334 FENCE: test process attempted to bind the PRODUCTION database "
            f"at {resolved_db / 'cli-agent-orchestrator.db'}. "
            f"This means CAO_HOME_DIR was not set before constants.py was imported. "
            f"Set CAO_HOME_DIR to a temp dir in conftest.py BEFORE any "
            f"cli_agent_orchestrator import, or set CAO_ALLOW_PROD_DB_IN_TESTS=1 "
            f"to override (contract tests only)."
        )


_fence_production_db()

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# F413 D6: Register session-level listeners for doorbell drain / rollback clear.
event.listen(SessionLocal, "after_commit", _f413_after_commit)
event.listen(SessionLocal, "after_rollback", _f413_after_rollback)
# Snapshot stash before each nested transaction so rollback can restore it.
event.listen(SessionLocal, "after_begin", _f413_after_begin)

_READY_COMMIT_CALLBACK = "_cao_ready_commit_callback"


@event.listens_for(Session, "after_commit", insert=True)
def _publish_ready_commit(session: Session) -> None:
    """Publish the ready winner before later after_commit observers run."""
    callback = session.info.pop(_READY_COMMIT_CALLBACK, None)
    if callback is not None:
        callback()


def init_db() -> None:
    """Initialize database tables and apply schema migrations."""
    _migrate_project_aliases_schema()
    Base.metadata.create_all(bind=engine)
    _bootstrap_seam_activation()
    _migrate_mailbox_columns()
    _migrate_callback_barrier_columns()
    _migrate_transcript_bindings_inode_nullable()
    _migrate_provider_sessions_status()
    _migrate_provider_sessions_session_name()
    _migrate_provider_sessions_kind()
    _migrate_provider_sessions_digest_head()
    _migrate_provider_sessions_retained_persona_home()
    _restrict_db_file_permissions()
    _migrate_terminals_schema()
    _migrate_fallback_parent_edges()
    _migrate_inbox_orchestration_type()
    _migrate_inbox_failure_reason()
    _migrate_memory_indexes()
    _migrate_add_access_count()
    _migrate_add_last_compiled_at()
    _migrate_add_related_keys()
    _migrate_workflow_index()
    _migrate_workflow_run()
    _migrate_workflow_run_indexes()
    _migrate_workflow_run_step()
    _migrate_workflow_outcome_indexes()
    _migrate_workflow_run_event()
    _migrate_workflow_run_seq()
    # Appended LAST (issue #511). Disjoint from the workflow_run* tables that
    # #504 also migrates, so registry order is immaterial — never reorder the
    # entries above.
    _migrate_memory_relationships()
    _migrate_mailbox_schema_version()
    _migrate_f136_callback_delivery()
    _migrate_f138_orphan_reconciliation()
    _migrate_f175_dedup_columns()
    _migrate_fx191_trace_extension()
    _migrate_f218_dead_supervisor_safety()
    _migrate_f129_frozen_authority()
    _migrate_f127_resolved_model()
    _migrate_terminal_auth_token()
    _migrate_f476_wake_recovery()


def _migrate_f218_dead_supervisor_safety() -> None:
    """F218-a/F219: Create dead-supervisor safety tables + widen obligation state constraint."""
    with engine.begin() as connection:
        # Check which tables already exist
        tables = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
        existing_tables = {r[0] for r in tables}

        # The three new tables are created by Base.metadata.create_all above if
        # the DB is fresh. For existing DBs we need explicit CREATE IF NOT EXISTS.
        if "pane_exit_tombstones" not in existing_tables:
            connection.execute(text("""
                CREATE TABLE pane_exit_tombstones (
                    id TEXT PRIMARY KEY,
                    incarnation_id TEXT NOT NULL UNIQUE,
                    terminal_id TEXT NOT NULL,
                    terminal_generation INTEGER NOT NULL,
                    token_hash TEXT,
                    session_name TEXT NOT NULL,
                    session_incarnation TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('window_gone','session_gone','unknown')),
                    scope_hint TEXT,
                    scope_evidence_json TEXT,
                    confirm_samples INTEGER,
                    window_name TEXT,
                    window_index INTEGER,
                    pane_id TEXT,
                    sibling_windows_json TEXT,
                    pane_pid INTEGER,
                    pane_start_ticks INTEGER,
                    pane_pgid INTEGER,
                    issuance_boot_id TEXT,
                    matched_pids_json TEXT,
                    cgroup_path TEXT,
                    systemd_scope TEXT,
                    proc_status TEXT NOT NULL,
                    proc_reason TEXT,
                    exit_code INTEGER,
                    term_signal INTEGER,
                    exit_evidence_status TEXT NOT NULL,
                    exit_evidence_reason TEXT,
                    memory_events_json TEXT,
                    memory_current INTEGER,
                    memory_peak INTEGER,
                    memory_max TEXT,
                    memory_pressure_json TEXT,
                    memory_status TEXT NOT NULL,
                    memory_reason TEXT,
                    writer TEXT NOT NULL CHECK(writer IN ('observation','job')),
                    schema_version INTEGER NOT NULL,
                    complete BOOLEAN NOT NULL,
                    incomplete_reason TEXT,
                    observed_at DATETIME NOT NULL,
                    written_at DATETIME NOT NULL,
                    server_pid INTEGER,
                    server_boot_id TEXT
                )
            """))
            connection.execute(
                text(
                    "CREATE INDEX ix_tombstone_session ON pane_exit_tombstones(session_name, session_incarnation)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_tombstone_terminal ON pane_exit_tombstones(terminal_id, terminal_generation)"
                )
            )

        if "session_degradations" not in existing_tables:
            connection.execute(text("""
                CREATE TABLE session_degradations (
                    id TEXT PRIMARY KEY,
                    session_name TEXT NOT NULL,
                    session_incarnation TEXT NOT NULL,
                    cause TEXT NOT NULL CHECK(cause IN ('supervisor_window_gone','session_gone','pane_unreachable_scope_unknown')),
                    tombstone_id TEXT,
                    terminal_id TEXT,
                    detail_json TEXT,
                    alarm_rungs_json TEXT,
                    alarm_delivered BOOLEAN NOT NULL,
                    suppressed_by_teardown BOOLEAN NOT NULL DEFAULT 0,
                    acknowledged_at DATETIME,
                    created_at DATETIME NOT NULL,
                    UNIQUE(session_name, session_incarnation, cause)
                )
            """))

        if "f218_teardown_intents" not in existing_tables:
            connection.execute(text("""
                CREATE TABLE f218_teardown_intents (
                    id TEXT PRIMARY KEY,
                    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('session','terminal')),
                    scope_key TEXT NOT NULL,
                    requested_by TEXT,
                    created_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL,
                    UNIQUE(scope_kind, scope_key)
                )
            """))

        # Widen obligation state constraint — SQLite does not support ALTER
        # CONSTRAINT, but the constraint is in the model for new DBs. For
        # existing DBs the check constraint is named; SQLite ignores named
        # constraints on existing tables so no migration is needed for the
        # runtime (SQLite does not enforce named CHECK constraints after
        # creation without a table rebuild). The model definition governs
        # new DBs.


def _migrate_f129_frozen_authority() -> None:
    """Idempotent startup migration for F129 frozen-pin schema additions.

    Called from init_db() after Base.metadata.create_all() and before any
    service startup. Safe to run repeatedly — checks existence before acting.

    Ordering:
      1. init_db() calls Base.metadata.create_all()  (creates missing tables)
      2. init_db() calls _migrate_f129_frozen_authority()
      3. Service startup proceeds

    Transaction/idempotence/error behavior:
      - Uses the repository-standard `with engine.begin() as connection:`
        migration pattern, so the column and index steps share one managed
        transaction and commit together on successful context exit.
      - Column check uses PRAGMA table_info(authority_pin) — if 'frozen' is
        already present, the ALTER TABLE is skipped (idempotent).
      - Index uses CREATE INDEX IF NOT EXISTS — inherently idempotent and
        independently safe after any interrupted prior startup.
      - On unrecoverable error (e.g. table does not exist at all), the context
        rolls back where SQLite permits and the exception propagates; init_db()
        aborts server startup. A half-migrated DB never serves traffic.
      - Running twice in succession produces identical schema state (no-op on
        second run).
    """
    with engine.begin() as connection:
        # ── Step 1: Add 'frozen' column if missing ──
        result = connection.execute(text("PRAGMA table_info(authority_pin)"))
        columns = [row[1] for row in result.fetchall()]

        if "frozen" not in columns:
            connection.execute(
                text("ALTER TABLE authority_pin " "ADD COLUMN frozen BOOLEAN NOT NULL DEFAULT 0")
            )
            # Existing rows receive frozen=0 (FALSE) via DEFAULT — they remain
            # mutable pins, preserving backward compatibility.

        # ── Step 2: Add covering index if missing ──
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_inbox_sender_receiver "
                "ON inbox (sender_id, receiver_id)"
            )
        )


def _migrate_f127_resolved_model() -> None:
    """F127: Add nullable resolved_model column to terminals table."""
    from sqlalchemy import text as _text

    with engine.begin() as connection:
        columns = connection.execute(_text("PRAGMA table_info(terminals)")).mappings().all()
        if columns and not any(column["name"] == "resolved_model" for column in columns):
            connection.execute(
                _text("ALTER TABLE terminals ADD COLUMN resolved_model TEXT DEFAULT NULL")
            )


def _migrate_terminal_auth_token() -> None:
    """F332: Add nullable auth_token column to terminals table for sender authentication."""
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(terminals)")).mappings().all()
        if columns and "auth_token" not in {row["name"] for row in columns}:
            connection.execute(text("ALTER TABLE terminals ADD COLUMN auth_token TEXT"))


def _migrate_fx191_trace_extension() -> None:
    """FX191 D7: Add phase/decision/reason columns to inbox_message_trace_event."""
    with engine.begin() as connection:
        columns = (
            connection.execute(text("PRAGMA table_info(inbox_message_trace_event)"))
            .mappings()
            .all()
        )
        col_names = {col["name"] for col in columns}
        if "phase" not in col_names:
            connection.execute(text("ALTER TABLE inbox_message_trace_event ADD COLUMN phase TEXT"))
        if "decision" not in col_names:
            connection.execute(
                text("ALTER TABLE inbox_message_trace_event ADD COLUMN decision TEXT")
            )
        if "reason" not in col_names:
            connection.execute(text("ALTER TABLE inbox_message_trace_event ADD COLUMN reason TEXT"))
        # Add the partial index for fx191 phase/decision if not present
        indexes = connection.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inbox_message_trace_event'"
            )
        ).fetchall()
        idx_names = {r[0] for r in indexes}
        if "ix_inbox_message_trace_event_fx191_phase_decision" not in idx_names:
            connection.execute(
                text(
                    "CREATE INDEX ix_inbox_message_trace_event_fx191_phase_decision "
                    "ON inbox_message_trace_event(phase, decision) WHERE phase IS NOT NULL"
                )
            )


def _migrate_f175_dedup_columns() -> None:
    """F175: Add dedicated dedup high-water columns to terminals table.

    These live outside metadata_json so supervisor update_metadata (whole-dict
    REPLACE) cannot clobber server-internal dedup state.
    """
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(terminals)")).mappings().all()
        col_names = {col["name"] for col in columns}
        if "last_notified_inbox_id" not in col_names:
            connection.execute(
                text("ALTER TABLE terminals ADD COLUMN last_notified_inbox_id INTEGER")
            )
        if "last_doorbell_row_id" not in col_names:
            connection.execute(
                text("ALTER TABLE terminals ADD COLUMN last_doorbell_row_id INTEGER")
            )


def _migrate_f476_wake_recovery() -> None:
    """F476: Add wake recovery columns to mailboxes table (D4).

    wake_notified_at: timestamp of last wake claim (lease).
    wake_streak: consecutive re-wake count for the same high-water id.
    wake_notified_id: the high-water id at last claim (lease pointer).
    """
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        col_names = {col["name"] for col in columns}
        if "wake_notified_at" not in col_names:
            connection.execute(text("ALTER TABLE mailboxes ADD COLUMN wake_notified_at DATETIME"))
        if "wake_streak" not in col_names:
            connection.execute(
                text("ALTER TABLE mailboxes ADD COLUMN wake_streak INTEGER NOT NULL DEFAULT 0")
            )
        if "wake_notified_id" not in col_names:
            connection.execute(
                text("ALTER TABLE mailboxes ADD COLUMN wake_notified_id INTEGER NOT NULL DEFAULT 0")
            )


def _migrate_f136_callback_delivery() -> None:
    """F136: Add callback cursor/path columns to mailboxes and replay queue table."""
    with engine.begin() as connection:
        # Mailbox columns
        columns = connection.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        col_names = {col["name"] for col in columns}
        if "callback_notified_through_id" not in col_names:
            connection.execute(
                text("ALTER TABLE mailboxes ADD COLUMN callback_notified_through_id INTEGER")
            )
        if "cc_inbox_path" not in col_names:
            connection.execute(text("ALTER TABLE mailboxes ADD COLUMN cc_inbox_path TEXT"))
        if "cc_inbox_path_version" not in col_names:
            connection.execute(
                text(
                    "ALTER TABLE mailboxes ADD COLUMN cc_inbox_path_version INTEGER NOT NULL DEFAULT 0"
                )
            )
        # Replay queue table
        tables = connection.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='callback_replay_queue'"
            )
        ).fetchall()
        if not tables:
            connection.execute(
                text(
                    "CREATE TABLE callback_replay_queue ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  mailbox_id TEXT NOT NULL REFERENCES mailboxes(id),"
                    "  inbox_row_id INTEGER NOT NULL REFERENCES inbox(id),"
                    "  queued_at DATETIME NOT NULL,"
                    "  UNIQUE(mailbox_id, inbox_row_id)"
                    ")"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_callback_replay_mailbox_row "
                    "ON callback_replay_queue(mailbox_id, inbox_row_id)"
                )
            )


def _migrate_mailbox_schema_version() -> None:
    """Add schema_version column to mailboxes table (WP-MAILBOX-CHANNEL)."""
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        if not columns or "schema_version" in {col["name"] for col in columns}:
            return
        connection.execute(
            text("ALTER TABLE mailboxes ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
        )


def _bootstrap_seam_activation() -> None:
    """Install the nine legacy-default authority rows idempotently."""

    with SessionLocal() as db:
        try:
            for consumer_op in SEAM_ACTIVATION_CONSUMER_OPS:
                if db.get(SeamActivationModel, consumer_op) is None:
                    db.add(
                        SeamActivationModel(
                            consumer_op=consumer_op,
                            active_authority="legacy",
                            accepted_version=0,
                            active_version=0,
                            rollback_version=0,
                            acceptance_token=None,
                            evidence_ref=None,
                            tombstoned_legacy=0,
                            updated_at=_utcnow().isoformat(),
                        )
                    )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to bootstrap seam activation rows")


def _migrate_provider_sessions_status() -> None:
    """Rebuild legacy provider_sessions tables so ``retired`` is valid."""
    from sqlalchemy import text

    with engine.begin() as connection:
        table_sql = connection.execute(
            text("SELECT sql FROM sqlite_master " "WHERE type='table' AND name='provider_sessions'")
        ).scalar_one_or_none()
        if table_sql is None or "'retired'" in table_sql:
            return

        connection.execute(text("ALTER TABLE provider_sessions RENAME TO provider_sessions_legacy"))
        connection.execute(
            text(
                "CREATE TABLE provider_sessions ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL, provider TEXT NOT NULL, session_uuid TEXT NOT NULL, "
                "cwd TEXT NOT NULL, agent_profile TEXT NOT NULL, git_sha TEXT, "
                "dirty_hashes TEXT DEFAULT '{}' NOT NULL, digest_head TEXT, "
                "retained_persona_home TEXT, summary TEXT, status TEXT NOT NULL, "
                "kind TEXT DEFAULT 'base' NOT NULL, "
                "source_terminal_id TEXT, session_name TEXT, created_at DATETIME, updated_at DATETIME, "
                "CONSTRAINT ck_provider_sessions_status "
                "CHECK (status IN ('ready','superseded','retired')), "
                "CONSTRAINT ck_provider_sessions_kind CHECK (kind IN ('base','anchor')))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO provider_sessions "
                "(id, name, provider, session_uuid, cwd, agent_profile, git_sha, dirty_hashes, "
                "digest_head, retained_persona_home, summary, status, kind, "
                "source_terminal_id, session_name, created_at, updated_at) "
                "SELECT id, name, provider, session_uuid, cwd, agent_profile, git_sha, "
                "dirty_hashes, NULL, NULL, summary, status, 'base', source_terminal_id, NULL, "
                "created_at, updated_at "
                "FROM provider_sessions_legacy"
            )
        )
        connection.execute(text("DROP TABLE provider_sessions_legacy"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_provider_sessions_ready ON provider_sessions (name) "
                "WHERE status = 'ready'"
            )
        )


def _migrate_provider_sessions_session_name() -> None:
    """Add nullable session scope to legacy base registrations."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(provider_sessions)")).mappings().all()
        if columns and not any(column["name"] == "session_name" for column in columns):
            connection.execute(text("ALTER TABLE provider_sessions ADD COLUMN session_name TEXT"))


def _migrate_provider_sessions_kind() -> None:
    """Type legacy provider-session rows as forkable bases."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(provider_sessions)")).mappings().all()
        if columns and not any(column["name"] == "kind" for column in columns):
            connection.execute(
                text(
                    "ALTER TABLE provider_sessions ADD COLUMN kind TEXT NOT NULL " "DEFAULT 'base'"
                )
            )


def _migrate_provider_sessions_digest_head() -> None:
    """Add the nullable digest lineage head idempotently."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(provider_sessions)")).mappings().all()
        if columns and not any(column["name"] == "digest_head" for column in columns):
            connection.execute(text("ALTER TABLE provider_sessions ADD COLUMN digest_head TEXT"))
    _migrate_provider_sessions_retained_persona_home()


def _migrate_provider_sessions_retained_persona_home() -> None:
    """Add the nullable retained Codex persona-home claim idempotently."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(provider_sessions)")).mappings().all()
        if columns and not any(column["name"] == "retained_persona_home" for column in columns):
            connection.execute(
                text("ALTER TABLE provider_sessions ADD COLUMN retained_persona_home TEXT")
            )


def _migrate_transcript_bindings_inode_nullable() -> None:
    """Rebuild the r4 table so startup bindings may defer inode discovery."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = (
            connection.execute(text("PRAGMA table_info(transcript_bindings)")).mappings().all()
        )
        inode = next((column for column in columns if column["name"] == "inode"), None)
        if inode is None or not inode["notnull"]:
            return
        connection.execute(
            text(
                "CREATE TABLE transcript_bindings_new ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "terminal_id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, "
                "transcript_path TEXT NOT NULL, inode INTEGER, source VARCHAR NOT NULL, "
                "received_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO transcript_bindings_new "
                "(id, terminal_id, session_id, transcript_path, inode, source, received_at) "
                "SELECT id, terminal_id, session_id, transcript_path, inode, source, received_at "
                "FROM transcript_bindings"
            )
        )
        connection.execute(text("DROP TABLE transcript_bindings"))
        connection.execute(
            text("ALTER TABLE transcript_bindings_new RENAME TO transcript_bindings")
        )
        connection.execute(
            text(
                "CREATE INDEX ix_transcript_bindings_terminal_received "
                "ON transcript_bindings (terminal_id, received_at, id)"
            )
        )


def _migrate_inbox_orchestration_type() -> None:
    """Add the orchestration mode to inbox rows created by older releases."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(inbox)")).mappings().all()
        if not columns or "orchestration_type" in {column["name"] for column in columns}:
            return
        connection.execute(
            text(
                "ALTER TABLE inbox ADD COLUMN orchestration_type TEXT NOT NULL "
                "DEFAULT 'send_message'"
            )
        )


def _migrate_inbox_failure_reason() -> None:
    """Add the nullable terminal-settlement reason to legacy inbox rows."""
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(inbox)")).mappings().all()
        if not columns or "failure_reason" in {column["name"] for column in columns}:
            return
        connection.execute(text("ALTER TABLE inbox ADD COLUMN failure_reason TEXT"))


def _migrate_mailbox_columns() -> None:
    """Install the two nullable Wave 3B columns on legacy databases."""
    with engine.begin() as connection:
        inbox_columns = connection.execute(text("PRAGMA table_info(inbox)")).mappings().all()
        if inbox_columns and "logical_receiver_id" not in {row["name"] for row in inbox_columns}:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN logical_receiver_id TEXT"))
        inbox_column_names = {row["name"] for row in inbox_columns}
        if inbox_columns and "digested_into" not in inbox_column_names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN digested_into INTEGER"))
        if inbox_columns and "enqueue_generation" not in inbox_column_names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN enqueue_generation INTEGER"))
        if inbox_columns and "owner_receiver_id" not in inbox_column_names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN owner_receiver_id TEXT"))
        if inbox_columns and "owner_generation" not in inbox_column_names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN owner_generation INTEGER"))
        if inbox_columns and "park_warm" not in inbox_column_names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN park_warm BOOLEAN"))
        if inbox_columns:
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS inbox_parked_owner_immutable "
                    "BEFORE UPDATE OF owner_receiver_id, owner_generation ON inbox "
                    "WHEN (OLD.owner_receiver_id IS NOT NULL OR OLD.owner_generation IS NOT NULL) "
                    "AND (NEW.owner_receiver_id IS NOT OLD.owner_receiver_id "
                    "OR NEW.owner_generation IS NOT OLD.owner_generation) "
                    "BEGIN SELECT RAISE(ABORT, 'parked_owner_immutable'); END"
                )
            )
        terminal_columns = connection.execute(text("PRAGMA table_info(terminals)")).mappings().all()
        if terminal_columns and "caller_mailbox_id" not in {
            row["name"] for row in terminal_columns
        }:
            connection.execute(text("ALTER TABLE terminals ADD COLUMN caller_mailbox_id TEXT"))


def _migrate_callback_barrier_columns() -> None:
    """Add WPQ7's nullable inbox routing tags to legacy databases."""
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(inbox)")).mappings().all()
        names = {column["name"] for column in columns}
        if columns and "barrier_id" not in names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN barrier_id INTEGER"))
        if columns and "barrier_member_key" not in names:
            connection.execute(text("ALTER TABLE inbox ADD COLUMN barrier_member_key TEXT"))


def _restrict_db_file_permissions() -> None:
    """Chmod the SQLite file (+ -wal/-shm siblings if present) to 0o600.

    The DB persists sensitive data (workflow spec_snapshot prompt bodies,
    inputs_json), matching the owner-only posture of prompt files and the audit
    log. Called after ``create_all`` so the file exists. Best-effort: a chmod
    failure (exotic filesystems) degrades permissions only, never blocks startup.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE

    for path in (
        DATABASE_FILE,
        DATABASE_FILE.with_name(DATABASE_FILE.name + "-wal"),
        DATABASE_FILE.with_name(DATABASE_FILE.name + "-shm"),
    ):
        if not path.exists():
            continue
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.warning(f"Could not restrict DB file permissions on {path}: {e}")


def _migrate_project_aliases_schema() -> None:
    """Rebuild project_aliases if it predates the alias-only primary key.

    The table originally used a composite PK ``(project_id, alias)``, which
    allowed one alias to map to several project_ids and made reverse lookups
    nondeterministic. The new schema keys on ``alias`` alone. SQLite cannot
    alter a primary key in place, so drop and recreate. The table is an
    opportunistic identity cache rebuilt by ``resolve_project_id`` on demand,
    so dropping rows is safe. Runs before ``create_all`` so the fresh schema
    is created with the new PK.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master " "WHERE type='table' AND name='project_aliases'"
            ).fetchone()
            if row is None:
                return  # table doesn't exist yet — create_all builds it fresh
            cols = conn.execute("PRAGMA table_info(project_aliases)").fetchall()
            # PRAGMA returns rows: (cid, name, type, notnull, dflt_value, pk).
            # In the legacy schema both project_id and alias have pk>0; in the
            # new schema only alias does.
            pk_cols = {c[1] for c in cols if c[5]}
            if pk_cols != {"alias"}:
                conn.execute("DROP TABLE project_aliases")
                conn.commit()
                logger.info("Migration: rebuilt project_aliases with alias-only primary key")
    except Exception as e:
        logger.debug(f"project_aliases migration skipped: {e}")


def _migrate_memory_indexes() -> None:
    """Add explicit indexes on memory_metadata for query performance."""
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_metadata (scope, scope_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_metadata (updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_metadata (memory_type)"
            )
    except Exception as e:
        logger.debug(f"Memory index migration skipped: {e}")


def _migrate_add_access_count() -> None:
    """Add access_count and last_accessed_at columns to memory_metadata if missing.

    Idempotent: PRAGMA table_info gate, ALTER TABLE ADD COLUMN only
    when missing. Fresh DBs already have the columns from
    ``Base.metadata.create_all``. Existing rows get ``0`` / ``NULL`` — the
    correct values for "never recalled".
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            cursor = conn.execute("PRAGMA table_info(memory_metadata)")
            columns = {row[1] for row in cursor.fetchall()}
            if "access_count" not in columns:
                conn.execute(
                    "ALTER TABLE memory_metadata ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("Migration: added access_count column to memory_metadata")
            if "last_accessed_at" not in columns:
                conn.execute("ALTER TABLE memory_metadata ADD COLUMN last_accessed_at DATETIME")
                logger.info("Migration: added last_accessed_at column to memory_metadata")
    except Exception as e:
        logger.debug(f"Migration check for access_count failed: {e}")


def _migrate_add_last_compiled_at() -> None:
    """Add last_compiled_at column to memory_metadata if missing.

    Idempotent: skipped on fresh DBs (the column ships in the model) and on
    repeated runs. Existing Phase 1/2 rows get NULL — correct, since they were
    never LLM-compiled.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            cursor = conn.execute("PRAGMA table_info(memory_metadata)")
            columns = {row[1] for row in cursor.fetchall()}
            if "last_compiled_at" not in columns:
                conn.execute("ALTER TABLE memory_metadata ADD COLUMN last_compiled_at DATETIME")
                logger.info("Migration: added last_compiled_at column to memory_metadata")
    except Exception as e:
        logger.debug(f"Migration check for last_compiled_at failed: {e}")


def _migrate_add_related_keys() -> None:
    """Add related_keys column to memory_metadata if missing.

    Reuses the idempotent ALTER pattern: PRAGMA table_info gate, ALTER TABLE
    ADD COLUMN only when missing. The CHECK(length < 1024) constraint applies
    to FRESH DBs only — adding a CHECK to an existing SQLite table requires a
    full table rebuild we deliberately avoid. Existing DBs rely on the
    parse-side 1024-byte cap in ``_parse_related_keys``.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            cursor = conn.execute("PRAGMA table_info(memory_metadata)")
            columns = {row[1] for row in cursor.fetchall()}
            if "related_keys" not in columns:
                conn.execute("ALTER TABLE memory_metadata ADD COLUMN related_keys TEXT")
                logger.info("Migration: added related_keys column to memory_metadata")
    except Exception as e:
        logger.debug(f"Migration check for related_keys failed: {e}")


def _migrate_memory_relationships() -> None:
    """Create the ``memory_relationships`` table + indexes and backfill legacy
    links (issue #511). Appended LAST to the ``init_db()`` registry.

    Idempotent, zero-arg, self-connecting — mirrors the existing migrators.
    Failure is logged at debug and never propagated (a missing table is
    recoverable; the service degrades). ``CREATE TABLE IF NOT EXISTS`` covers
    existing DBs where ``Base.metadata.create_all`` (which builds the model with
    its CHECK constraints on fresh DBs) has already run or will run — the same
    fresh-vs-existing split the codebase uses for ``related_keys``.

    Disjoint from the ``workflow_run*`` tables (#504); registry order is
    immaterial.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_relationships ("
                "id TEXT PRIMARY KEY, "
                "scope TEXT NOT NULL, "
                "scope_id TEXT NOT NULL, "
                "source_key TEXT NOT NULL, "
                "target_key TEXT NOT NULL, "
                "type TEXT NOT NULL, "
                "origin TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'active', "
                "confidence REAL, "
                "rank INTEGER, "
                "attributes_json TEXT, "
                "source_updated_at DATETIME, "
                "created_at DATETIME, "
                "updated_at DATETIME"
                ")"
            )
            # Dedup UNIQUE index — total because scope_id is NOT NULL (sentinel),
            # so ON CONFLICT fires for all scopes including global.
            #
            # ACCEPTED REDUNDANCY on a FRESH db (human review, PR #524): there,
            # create_all() has already satisfied the model's UniqueConstraint via
            # an unnamed sqlite_autoindex, so this statement adds a SECOND index
            # over identical columns (the name matches the constraint, but SQLite
            # does not treat a table-level UNIQUE as a named index, so
            # IF NOT EXISTS does not suppress it). Kept deliberately: this
            # migrator must remain zero-arg and idempotent for EXISTING dbs,
            # where CREATE TABLE IF NOT EXISTS is a no-op and this is the ONLY
            # thing that establishes the dedup index that replace_set/create rely
            # on. Making it fresh-db-aware would mean probing pragma index_list
            # and branching — more moving parts in a path whose failure mode is
            # silent duplicate edges. The cost is one extra index on new
            # installs: some write amplification and disk, no correctness or
            # query-plan impact.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_rel ON memory_relationships "
                "(scope, scope_id, source_key, target_key, type, origin)"
            )
            # Lookup index for the common (scope, scope_id, source_key) read path.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_rel_lookup ON memory_relationships "
                "(scope, scope_id, source_key)"
            )
            conn.commit()
            _backfill_legacy_related_keys(conn)
    except Exception as e:
        logger.debug(f"memory_relationships migration skipped: {e}")


def _backfill_legacy_related_keys(conn: Any) -> None:
    """One-time, idempotent backfill of ``memory_metadata.related_keys`` into
    ``memory_relationships`` as ``type=relates_to, origin=legacy_related_keys,
    status=active, confidence=NULL`` rows (issue #511, FR-1.4/FR-1.5).

    - Gated per source memory: if any ``legacy_related_keys`` row already exists
      for that ``(scope, scope_id, source_key)``, the source is skipped, so
      re-running ``init_db()`` is a no-op (idempotent).
    - ``related_keys IS NULL`` or ``""`` yields zero rows (never-computed /
      computed-empty carry no edge). The NULL-vs-"" marker stays on
      ``related_keys`` UNCHANGED — this backfill only READS it (ADR-4).
    - ``confidence`` is always NULL (never fabricated — NFR-2.1). Order is
      preserved as ``rank``.
    - A target that no longer resolves to an in-scope memory, a self-link, or a
      key that fails the sanitiser is REPORTED (logged) and NOT written active
      (FR-1.5) — never silently activated.
    - ``scope_id`` is normalised to the sentinel ``""`` for global/federated so
      the dedup index is total.

    Best-effort: any failure is logged at debug and never propagated (the
    service can compute relationships later; a partial backfill is safe because
    the per-source gate resumes cleanly).
    """
    # Lazy import to avoid a circular import (memory_service imports database).
    try:
        from cli_agent_orchestrator.services.memory_service import MemoryService
    except Exception as e:  # pragma: no cover - import guard
        logger.debug(f"backfill skipped (memory_service import): {e}")
        return

    now_iso = _utcnow().isoformat()
    reported: list[str] = []
    try:
        rows = conn.execute(
            "SELECT key, scope, scope_id, related_keys, updated_at "
            "FROM memory_metadata "
            "WHERE related_keys IS NOT NULL AND related_keys != ''"
        ).fetchall()
    except Exception as e:
        logger.debug(f"backfill skipped (memory_metadata read): {e}")
        return

    for key, scope, scope_id, related_keys, src_updated_at in rows:
        sentinel = scope_id if scope_id is not None else RELATIONSHIP_SCOPE_ID_SENTINEL
        # Per-source idempotency gate (exact = on the sentinel, never IS NULL).
        existing = conn.execute(
            "SELECT 1 FROM memory_relationships "
            "WHERE source_key = ? AND scope = ? AND scope_id = ? "
            "AND origin = 'legacy_related_keys' LIMIT 1",
            (key, scope, sentinel),
        ).fetchone()
        if existing is not None:
            continue

        targets = MemoryService._parse_related_keys(related_keys, scope)
        # Resolve which target keys actually exist in the SAME (scope, scope_id).
        for rank, target in enumerate(targets):
            if target == key:
                reported.append(f"{scope}/{scope_id}/{key}->{target}: self-link")
                continue
            # Endpoint existence against memory_metadata: scope_id is genuinely
            # nullable there (real NULL for global), so match logical NULL, NOT
            # the sentinel.
            if scope_id is None:
                found = conn.execute(
                    "SELECT 1 FROM memory_metadata "
                    "WHERE key = ? AND scope = ? AND scope_id IS NULL LIMIT 1",
                    (target, scope),
                ).fetchone()
            else:
                found = conn.execute(
                    "SELECT 1 FROM memory_metadata "
                    "WHERE key = ? AND scope = ? AND scope_id = ? LIMIT 1",
                    (target, scope, scope_id),
                ).fetchone()
            if found is None:
                reported.append(f"{scope}/{scope_id}/{key}->{target}: dangling")
                continue
            try:
                conn.execute(
                    "INSERT INTO memory_relationships "
                    "(id, scope, scope_id, source_key, target_key, type, origin, "
                    "status, confidence, rank, attributes_json, source_updated_at, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'relates_to', 'legacy_related_keys', "
                    "'active', NULL, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT (scope, scope_id, source_key, target_key, type, origin) "
                    "DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        scope,
                        sentinel,
                        key,
                        target,
                        rank,
                        src_updated_at,
                        now_iso,
                        now_iso,
                    ),
                )
            except Exception as e:
                logger.debug(f"backfill insert skipped for {key}->{target}: {e}")
    try:
        conn.commit()
    except Exception:  # pragma: no cover
        pass
    if reported:
        logger.warning(
            "memory_relationships backfill reported %d stale/malformed legacy "
            "links (NOT activated): %s",
            len(reported),
            "; ".join(reported[:20]),
        )


def _migrate_f77_lifecycle_pointers() -> None:
    """F77 data-repair: terminalize orphaned lifecycle pointers (idempotent).

    1. AWAITING barrier members under non-OPEN barriers → FAILED(barrier_closed_historical)
    2. PENDING inbox rows whose receiver_id is gone AND logical_receiver_id is a mailbox
       → DELIVERY_FAILED(receiver_gone_historical)
    """
    with SessionLocal.begin() as db:
        # (1) Stranded AWAITING members under already-closed barriers
        from sqlalchemy import and_

        subq = (
            db.query(CallbackBarrierMemberModel.id)
            .join(
                CallbackBarrierModel,
                CallbackBarrierMemberModel.barrier_id == CallbackBarrierModel.id,
            )
            .filter(
                CallbackBarrierMemberModel.state == "AWAITING",
                CallbackBarrierModel.state != "OPEN",
            )
            .subquery()
        )
        db.query(CallbackBarrierMemberModel).filter(
            CallbackBarrierMemberModel.id.in_(subq.select())
        ).update(
            {
                CallbackBarrierMemberModel.state: "FAILED",
                CallbackBarrierMemberModel.failure_class: "barrier_closed_historical",
            },
            synchronize_session=False,
        )

        # (2) Stranded PENDING inbox rows addressed to dead terminals via mailbox
        dead_pending = (
            db.query(InboxModel)
            .filter(
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.logical_receiver_id.isnot(None),
                ~InboxModel.receiver_id.in_(db.query(TerminalModel.id)),
            )
            .all()
        )
        for row in dead_pending:
            row.status = MessageStatus.DELIVERY_FAILED.value
            row.failure_reason = "receiver_gone_historical"


def _migrate_workflow_index() -> None:
    """Create/upgrade the derived ``workflow_index`` table (issue #312, N2).

    The table is a **derived, non-authoritative** projection of the workflow
    spec YAML files on disk (B2-BR-2): it can be dropped and rebuilt
    byte-identically from the files alone (``rebuild_index_from_files``). It
    carries no run/execution state — runs and per-step state are N5/N6.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors the existing ``_migrate_memory_indexes`` pattern. Failure is logged
    at debug and never propagated (a missing index table is recoverable: the
    next ``list`` rebuilds it).

    U5 additively widens ``step_count`` to nullable: script-tier rows carry
    NULL (step count is run-time-determined, unknowable at index time), while
    YAML rows keep populating an int. ``CREATE TABLE IF NOT EXISTS`` only
    covers fresh DBs — on a pre-U5 DB the column already exists as NOT NULL,
    and SQLite cannot ``ALTER COLUMN`` to relax a NOT NULL constraint in
    place. Same drop/rebuild precedent as ``_migrate_project_aliases_schema``:
    the table is fully derived, so dropping it is safe — the next ``list``
    rebuilds it from the workflow files on disk.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_index'"
            ).fetchone()
            if row is not None:
                cols = conn.execute("PRAGMA table_info(workflow_index)").fetchall()
                # PRAGMA row: (cid, name, type, notnull, dflt_value, pk).
                step_count_col = next((c for c in cols if c[1] == "step_count"), None)
                if step_count_col is not None and step_count_col[3]:  # notnull flag set
                    conn.execute("DROP TABLE workflow_index")
                    conn.commit()
                    logger.info(
                        "Migration: rebuilt workflow_index with nullable step_count "
                        "(dropped legacy table; rebuilt from workflow files on next list)"
                    )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_index ("
                "name TEXT PRIMARY KEY, "
                "source_path TEXT NOT NULL, "
                "mode TEXT NOT NULL, "
                "step_count INTEGER, "  # nullable: script-tier rows carry NULL
                "description TEXT NOT NULL DEFAULT '', "
                "indexed_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived table; rebuilt on next list
        logger.debug(f"workflow_index migration skipped: {e}")


def _migrate_workflow_run() -> None:
    """Create the durable ``workflow_run`` journal table if missing (issue #312, N6).

    The run aggregate root: one row per run, keyed by ``run_id`` (E1,
    domain-entities). Per Q1=B this is the **source of truth** for run execution
    state; the Bolt-3 in-memory ``run_registry`` is a cache over it. No loop
    columns (``iteration_counter`` etc.) — deferred to N8 (Q4=B, B4-BR-12).

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors ``_migrate_workflow_index`` (B2, B4-BR-1). Failure is logged at debug
    and never propagated: a missing table is recoverable, the next write retries
    the path and the live run completes on the in-memory floor (B4-RD-4).

    U3 (issue #312, script-tier journal extension) additively appends two
    columns — ``tier`` and ``generation`` (E1, domain-entities) — via the same
    idempotent ``PRAGMA table_info`` gate used by ``_migrate_add_access_count`` /
    ``_migrate_add_related_keys``. Both default to values that make a pre-U3 /
    YAML row read identically to its pre-extension form (INV-1/INV-2): existing
    rows back-fill to ``tier='yaml'``, ``generation='1'``. ``generation`` is TEXT,
    not INTEGER, so it compares byte-identically against the env-var-transported
    string generation value (domain-entities B4 fix).
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run ("
                "run_id TEXT PRIMARY KEY, "
                "workflow_name TEXT NOT NULL, "
                "spec_snapshot TEXT NOT NULL, "
                "inputs_json TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "current_step_id TEXT, "
                "started_at TEXT NOT NULL, "
                "finished_at TEXT"
                ")"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_run)")}
            if "tier" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run ADD COLUMN tier TEXT NOT NULL DEFAULT 'yaml'"
                )
                logger.info("Migration: added tier column to workflow_run")
            if "generation" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run ADD COLUMN generation TEXT NOT NULL DEFAULT '1'"
                )
                logger.info("Migration: added generation column to workflow_run")
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_run migration skipped: {e}")


def _migrate_workflow_run_step() -> None:
    """Create the durable ``workflow_run_step`` table if missing (issue #312, N6).

    Per-step durable state: one row per ``(run_id, step_id)`` (E2,
    domain-entities). ``reprompted``/``terminal_id`` are deliberately NOT
    journaled (F3) — they are in-memory-only and defaulted on rebuild. No
    ``which_guard_fired``/``iterations_run`` columns — N8 adds them via its own
    additive migrator (Q4=B, B4-BR-12).

    Idempotent, zero-arg, self-connecting; failure logged at debug and never
    propagated (B4-BR-1 / B4-RD-4), same precedent as ``_migrate_workflow_index``.

    U3 (issue #312, script-tier journal extension) additively appends
    ``call_fingerprint`` (E2, domain-entities) via the same idempotent
    ``PRAGMA table_info`` gate. Defaults to ``NULL`` so a pre-U3 / YAML row is
    indistinguishable from its pre-extension form (INV-1/INV-2); ``append_step``
    is the sole write path for the column (``update_step`` stays untouched — the
    fingerprint is set once, at the RUNNING insert).

    U1 (issue #504, event-log substrate) additively appends three nullable
    columns via the same PRAGMA-gated ``ALTER TABLE ADD COLUMN`` idiom:
    ``terminal_id`` (associated terminal), ``reprompted`` (reprompt flag), and
    ``error_kind`` (structured error kind). All default to ``NULL`` so a
    pre-U1 row reads back observably identical to its pre-extension form
    (additive-only, C-1/C-4). ``workflow_run`` itself is untouched.

    ``result-envelope`` (issue #583, BR-7) then additively appends ONE column,
    ``result_json``, through the same gate — the serialised ``StepResultEnvelope``
    that replay returns (FR-1). ``DEFAULT NULL`` means every pre-#583 row reads as
    "envelope absent" (BR-10), which is safe rather than a gap: such a row's
    fingerprint is legacy-scheme or NULL, so FR-6 already keeps it off the replay
    path and the two guards agree instead of disagreeing.

    Because the failure is silent, the column's existence is VERIFIED rather than
    assumed (BR-8): see ``test/services/test_step_result.py`` for the
    ``PRAGMA table_info`` assertion on a fresh database. A silent failure would
    otherwise surface far away as every settle losing its envelope, which the
    replay gate would read as crash-window rows and halt on.

    MERGE NOTE (2026-08-17, #583 x #504). #583's original rationale for adding ONE
    column rather than three read: "three statements would triple the chance of a
    partial, silent migration", because this body is wrapped in
    ``except Exception`` -> ``logger.debug``. **That argument is overtaken by the
    merge** — #504 independently added three columns to the same silently-failing
    block, so the combined body now issues FOUR guarded ``ALTER`` statements, not
    one. The risk #583 minimised is materially larger than either change assumed
    alone. #583's mitigation (assert the column exists on a fresh database) is
    therefore MORE load-bearing after this merge, and #504's three columns have no
    equivalent assertion. Flagged rather than silently reconciled.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run_step ("
                "run_id TEXT NOT NULL, "
                "step_id TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "attempts INTEGER NOT NULL, "
                "output_json TEXT, "
                "error TEXT, "
                "updated_at TEXT NOT NULL, "
                "PRIMARY KEY (run_id, step_id)"
                ")"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_run_step)")}
            if "call_fingerprint" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN call_fingerprint TEXT DEFAULT NULL"
                )
                logger.info("Migration: added call_fingerprint column to workflow_run_step")
            if "terminal_id" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN terminal_id TEXT DEFAULT NULL"
                )
                logger.info("Migration: added terminal_id column to workflow_run_step")
            if "reprompted" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN reprompted INTEGER DEFAULT NULL"
                )
                logger.info("Migration: added reprompted column to workflow_run_step")
            if "error_kind" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN error_kind TEXT DEFAULT NULL"
                )
                logger.info("Migration: added error_kind column to workflow_run_step")
            if "result_json" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN result_json TEXT DEFAULT NULL"
                )
                logger.info("Migration: added result_json column to workflow_run_step")
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_run_step migration skipped: {e}")


def _migrate_workflow_outcome_indexes() -> None:
    """Add indexes on workflow_outcomes for retrospector queries.

    The table itself is created by ``Base.metadata.create_all`` (it ships in
    the model, so fresh and existing DBs both get it). Retrospection filters
    by session and by agent profile over a recency window — index both.
    Idempotent, self-connecting, failure logged at debug — mirrors
    ``_migrate_memory_indexes``.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome_session "
                "ON workflow_outcomes (session_name, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome_agent "
                "ON workflow_outcomes (agent_profile, created_at)"
            )
    except Exception as e:
        logger.debug(f"workflow_outcomes index migration skipped: {e}")


def _migrate_workflow_run_event() -> None:
    """Create the durable append-only ``workflow_run_event`` table if missing (issue #504, U1).

    The event log root: one row per emitted workflow domain event, keyed by the
    composite ``(run_id, seq)`` PRIMARY KEY (ADR-1, domain-entities). Per
    NFR-DUR-1 this table is the authoritative, append-only, versioned record of
    workflow execution — rows are inserted and never updated or reordered; ``seq``
    (a per-run monotonically increasing sequence) is the SOLE ordering authority,
    ``ts`` is display/duration only (BR-5). ``run_id``, ``seq``, ``event_type``,
    ``event_schema_version`` (FR-1.1) and ``ts`` are NOT NULL; the remaining
    columns are nullable and populated where applicable. ``iteration`` and
    ``which_guard_fired`` are RESERVED for a later deterministic-loops feature
    (FR-1.5) and stay NULL in the MVP.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors ``_migrate_workflow_run`` (C-1/C-4, additive-only). Failure is logged
    at debug and never propagated: a missing table is recoverable, the next
    best-effort append retries the path.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run_event ("
                "run_id TEXT NOT NULL, "
                "seq INTEGER NOT NULL, "
                "event_type TEXT NOT NULL, "
                "event_schema_version INTEGER NOT NULL, "
                "ts TEXT NOT NULL, "
                "step_id TEXT, "
                "attempt INTEGER, "
                "state TEXT, "
                "elapsed_ms INTEGER, "
                "provider TEXT, "
                "agent_profile TEXT, "
                "engine TEXT, "
                "terminal_id TEXT, "
                "terminal_offset_start INTEGER, "
                "terminal_offset_len INTEGER, "
                "error_kind TEXT, "
                "reason TEXT, "
                "validation_result TEXT, "
                "output_ref TEXT, "
                "iteration INTEGER, "
                "which_guard_fired TEXT, "
                "PRIMARY KEY (run_id, seq)"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug
        logger.debug(f"workflow_run_event migration skipped: {e}")


def _migrate_workflow_run_seq() -> None:
    """Create the durable ``workflow_run_seq`` high-water table if missing (issue #504, U1).

    One row per run: ``high_water`` records the highest per-run ``seq`` ever
    ALLOCATED (best-effort persisted before the matching event append), so a
    rebuild can resume strictly above any allocated slot even when its append was
    swallowed (BR-3). ``high_water`` advances monotonically (BR-11) and is NOT
    NULL; ``run_id`` is the PRIMARY KEY.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    same additive-only posture as ``_migrate_workflow_run_event``. Failure is
    logged at debug and never propagated.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run_seq ("
                "run_id TEXT PRIMARY KEY, "
                "high_water INTEGER NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug
        logger.debug(f"workflow_run_seq migration skipped: {e}")


def _migrate_workflow_run_indexes() -> None:
    """Add explicit indexes on ``workflow_run`` for list-query performance (U1, FR-3.2).

    Two single-column indexes serving the two shapes ``list_runs`` produces: the
    unfiltered newest-first list orders by ``started_at`` alone (served by
    ``idx_workflow_run_started_at``), and the state-filtered list narrows on
    ``state`` (served by ``idx_workflow_run_state``). Two single-column indexes
    cover both paths; a single composite ``(state, started_at)`` would not serve
    the unfiltered ``started_at``-only ordering (ADR-6, IR-1).

    Zero-arg, self-connecting, and idempotent — mirrors ``_migrate_memory_indexes``.
    Each statement uses ``CREATE INDEX IF NOT EXISTS`` so a second ``init_db()`` is
    a no-op (IR-2); no destructive migration, no Alembic (NFR-5). It creates only
    indexes, never columns — so the C-4 exact-column migration test is untouched
    (IR-4). Registered AFTER ``_migrate_workflow_run`` in ``init_db`` so the base
    table exists first. Failure is logged at debug and never raised: a missing
    index degrades to a table scan, not a crash (IR-3).
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_run_started_at "
                "ON workflow_run (started_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_run_state ON workflow_run (state)"
            )
    except Exception as e:  # noqa: BLE001 — missing index degrades to a scan (IR-3)
        logger.debug(f"workflow_run index migration skipped: {e}")


def _migrate_terminals_schema() -> None:
    """Atomically rebuild legacy terminals with the frozen init lifecycle.

    This migration is deliberately fatal: startup cannot safely run H3/H5 on
    a partial schema.  The rename, rebuild, copy, constraints, and index land
    in one rollback-capable transaction.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    conn = sqlite3.connect(str(DATABASE_FILE), isolation_level=None)
    try:
        table_info = list(conn.execute("PRAGMA table_info(terminals)"))
        columns = {row[1] for row in table_info}
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='terminals'"
        ).fetchone()
        table_sql = table_sql_row[0] if table_sql_row else ""
        init_columns = {
            "init_state",
            "init_started_at",
            "init_owner_epoch",
            "init_failure_token",
            "init_deadline_s",
        }
        has_token_unique = any(
            bool(row[2])
            and any(
                detail[2] == "init_failure_token"
                for detail in conn.execute(f"PRAGMA index_info('{row[1]}')")
            )
            for row in conn.execute("PRAGMA index_list('terminals')")
        )
        trigger_exists = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
                "name='terminals_init_failure_token_immutable'"
            ).fetchone()
            is not None
        )
        worktree_info_trigger_exists = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
                "name='terminals_worktree_info_immutable'"
            ).fetchone()
            is not None
        )
        schema_current = (
            init_columns.issubset(columns)
            and "caller_mailbox_id" in columns
            and "instance_id" in columns
            and "lifecycle" in columns
            and "reparented_from" in columns
            and any(row[1] == "lifecycle_generation" and bool(row[3]) for row in table_info)
            and "lifecycle IN" in table_sql
            and "init_state IN" in table_sql
            and "init_deadline_s >= 1.0" in table_sql
            and has_token_unique
        )
        if not columns:
            return
        if schema_current:
            if "engine" not in columns:
                conn.execute("ALTER TABLE terminals ADD COLUMN engine TEXT")
                conn.commit()
                logger.info("Migration: added engine column to terminals table")
            if "group" not in columns:
                conn.execute('ALTER TABLE terminals ADD COLUMN "group" TEXT')
                conn.commit()
                logger.info("Migration: added group column to terminals table")
            if "metadata" not in columns:
                conn.execute('ALTER TABLE terminals ADD COLUMN "metadata" TEXT')
                conn.commit()
                logger.info("Migration: added metadata column to terminals table")
            if not trigger_exists:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TRIGGER terminals_init_failure_token_immutable "
                    "BEFORE UPDATE OF init_failure_token ON terminals "
                    "WHEN OLD.init_failure_token IS NOT NULL AND "
                    "NEW.init_failure_token IS NOT OLD.init_failure_token "
                    "BEGIN SELECT RAISE(ABORT, 'init_failure_token_immutable'); END"
                )
                conn.execute("COMMIT")
            if "working_directory" not in columns:
                conn.execute("ALTER TABLE terminals ADD COLUMN working_directory TEXT")
                conn.commit()
                logger.info("Migration: added working_directory column to terminals table")
            if "worktree_info" not in columns:
                conn.execute("ALTER TABLE terminals ADD COLUMN worktree_info TEXT")
                conn.commit()
                logger.info("Migration: added worktree_info column to terminals table")
            if not worktree_info_trigger_exists:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TRIGGER terminals_worktree_info_immutable "
                    "BEFORE UPDATE OF worktree_info ON terminals "
                    "WHEN OLD.worktree_info IS NOT NULL AND "
                    "NEW.worktree_info IS NOT OLD.worktree_info "
                    "BEGIN SELECT RAISE(ABORT, 'worktree_info_immutable'); END"
                )
                conn.execute("COMMIT")
            return
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE terminals RENAME TO terminals_wpm4a_legacy")
        conn.execute(
            "CREATE TABLE terminals ("
            "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, tmux_window TEXT NOT NULL, "
            "provider TEXT NOT NULL, agent_profile TEXT, allowed_tools TEXT, "
            "shell_command TEXT, caller_id TEXT, engine TEXT, "
            "lifecycle TEXT NOT NULL DEFAULT 'ephemeral' "
            "CHECK (lifecycle IN ('ephemeral','sticky')), reparented_from TEXT, "
            "provider_session_id TEXT, instance_id TEXT, "
            "caller_mailbox_id TEXT, "
            "recovery_state TEXT, recovery_error TEXT, recovery_updated_at DATETIME, "
            "fallback_terminal_id TEXT, "
            "init_state TEXT NOT NULL DEFAULT 'ready' "
            "CHECK (init_state IN ('init_pending','ready','init_failed_notified',"
            "'init_failed_caller_gone')), "
            "init_started_at DATETIME, init_owner_epoch TEXT, "
            "init_failure_token TEXT UNIQUE, init_deadline_s REAL, "
            "lifecycle_generation INTEGER NOT NULL DEFAULT 0, "
            '"group" TEXT, '
            '"metadata" TEXT, '
            "working_directory TEXT, "
            "worktree_info TEXT, "
            "last_active DATETIME, "
            "CHECK (init_state != 'init_pending' OR "
            "(init_started_at IS NOT NULL AND init_owner_epoch IS NOT NULL AND "
            "length(init_owner_epoch) = 36 AND init_owner_epoch = lower(init_owner_epoch) AND "
            "substr(init_owner_epoch,9,1) = '-' AND substr(init_owner_epoch,14,1) = '-' AND "
            "substr(init_owner_epoch,19,1) = '-' AND substr(init_owner_epoch,24,1) = '-' AND "
            "init_deadline_s IS NOT NULL AND init_deadline_s >= 1.0 AND "
            "init_deadline_s <= 600.0 AND init_deadline_s = init_deadline_s)), "
            "CHECK (init_failure_token IS NULL OR "
            "(length(init_failure_token) = 36 AND init_failure_token = lower(init_failure_token) AND "
            "substr(init_failure_token,9,1) = '-' AND substr(init_failure_token,14,1) = '-' AND "
            "substr(init_failure_token,19,1) = '-' AND substr(init_failure_token,24,1) = '-')))"
        )
        legacy_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(terminals_wpm4a_legacy)")
        }
        destination = [
            "id",
            "tmux_session",
            "tmux_window",
            "provider",
            "agent_profile",
            "allowed_tools",
            "shell_command",
            "caller_id",
            "engine",
            "lifecycle",
            "reparented_from",
            "provider_session_id",
            "caller_mailbox_id",
            "instance_id",
            "recovery_state",
            "recovery_error",
            "recovery_updated_at",
            "fallback_terminal_id",
            "last_active",
            "init_state",
            "init_started_at",
            "init_owner_epoch",
            "init_failure_token",
            "init_deadline_s",
            "lifecycle_generation",
            "worktree_info",
        ]
        copied = [name for name in destination if name in legacy_columns]
        selected = []
        for name in copied:
            if name == "lifecycle_generation":
                selected.append("COALESCE(lifecycle_generation, 0)")
            elif name == "lifecycle":
                selected.append("COALESCE(lifecycle, 'ephemeral')")
            else:
                selected.append(name)
        conn.execute(
            f"INSERT INTO terminals ({','.join(copied)}) "
            f"SELECT {','.join(selected)} FROM terminals_wpm4a_legacy"
        )
        conn.execute("DROP TABLE terminals_wpm4a_legacy")
        conn.execute(
            "CREATE TRIGGER terminals_init_failure_token_immutable "
            "BEFORE UPDATE OF init_failure_token ON terminals "
            "WHEN OLD.init_failure_token IS NOT NULL AND "
            "NEW.init_failure_token IS NOT OLD.init_failure_token "
            "BEGIN SELECT RAISE(ABORT, 'init_failure_token_immutable'); END"
        )
        conn.execute(
            "CREATE TRIGGER terminals_worktree_info_immutable "
            "BEFORE UPDATE OF worktree_info ON terminals "
            "WHEN OLD.worktree_info IS NOT NULL AND "
            "NEW.worktree_info IS NOT OLD.worktree_info "
            "BEGIN SELECT RAISE(ABORT, 'worktree_info_immutable'); END"
        )
        conn.execute("COMMIT")
        logger.info("Migration: atomically installed deferred-init terminal schema")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        logger.exception("Fatal deferred-init terminal schema migration failure")
        raise
    finally:
        conn.close()


def _migrate_fallback_parent_edges() -> None:
    """Repair legacy child edges left on settled recovery husks."""
    with SessionLocal.begin() as db:
        husks = (
            db.query(TerminalModel.id, TerminalModel.fallback_terminal_id)
            .filter(
                TerminalModel.recovery_state == "fallback_ready",
                TerminalModel.fallback_terminal_id.is_not(None),
            )
            .all()
        )
        for old_id, new_id in husks:
            if db.query(TerminalModel.id).filter_by(id=new_id).one_or_none() is None:
                continue
            db.query(TerminalModel).filter(TerminalModel.caller_id == old_id).update(
                {
                    TerminalModel.caller_id: new_id,
                    TerminalModel.caller_mailbox_id: _mailbox_id_for_terminal(db, new_id),
                    TerminalModel.reparented_from: old_id,
                },
                synchronize_session=False,
            )


def _mailbox_schema_available(db: Any) -> bool:
    try:
        tables = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "name IN ('mailboxes','mailbox_incarnations')"
                )
            ).all()
        }
    except TypeError:
        return False
    if tables != {"mailboxes", "mailbox_incarnations"}:
        return False
    inbox_columns = {row[1] for row in db.execute(text("PRAGMA table_info(inbox)")).all()}
    return not inbox_columns or "logical_receiver_id" in inbox_columns


def _terminal_mailbox_column_available(db: Any) -> bool:
    return "caller_mailbox_id" in {
        row[1] for row in db.execute(text("PRAGMA table_info(terminals)")).all()
    }


def _mailbox_id_for_terminal(db: Any, terminal_id: str | None) -> str | None:
    if not terminal_id:
        return None
    if not _mailbox_schema_available(db):
        return None
    row = (
        db.query(MailboxIncarnationModel.mailbox_id)
        .filter(MailboxIncarnationModel.terminal_id == terminal_id)
        .one_or_none()
    )
    return cast(str, row[0]) if row is not None else None


def resolve_inbox_receiver(db: Any, receiver_id: str) -> tuple[str, str | None, int | None]:
    """Resolve a logical or historical inbox address inside the caller's transaction."""
    if not _mailbox_schema_available(db):
        return receiver_id, None, None
    if receiver_id.startswith("mb_"):
        mailbox = db.query(MailboxModel).filter(MailboxModel.id == receiver_id).one_or_none()
        if mailbox is None:
            raise ValueError("unknown_mailbox")
        cache = mailbox.current_terminal_id
        if cache is None:
            latest = (
                db.query(MailboxIncarnationModel.terminal_id)
                .filter(MailboxIncarnationModel.mailbox_id == mailbox.id)
                .order_by(MailboxIncarnationModel.generation.desc())
                .first()
            )
            cache = latest[0] if latest is not None else receiver_id
        return cast(str, cache), cast(str, mailbox.id), cast(int, mailbox.generation)
    mailbox_id = _mailbox_id_for_terminal(db, receiver_id)
    if mailbox_id is None:
        return receiver_id, None, None
    generation = db.query(MailboxModel.generation).filter_by(id=mailbox_id).scalar()
    return receiver_id, mailbox_id, cast(int | None, generation)


def callback_barrier_dispatch_allowed_in_db(
    db: Any,
    sender_id: str,
    receiver_id: str,
) -> bool:
    """Return whether sender owns the receiver route in the caller's transaction."""
    try:
        receiver_cache, _, _ = resolve_inbox_receiver(db, receiver_id)
    except ValueError:
        return False
    receiver = db.query(TerminalModel).filter_by(id=receiver_cache).one_or_none()
    if receiver is None:
        return False
    if receiver.caller_id == sender_id:
        return True
    sender_mailbox_id = _mailbox_id_for_terminal(db, sender_id)
    return bool(sender_mailbox_id is not None and receiver.caller_mailbox_id == sender_mailbox_id)


def callback_barrier_dispatch_allowed(sender_id: str, receiver_id: str) -> bool:
    """Return whether sender owns the target worker's callback route."""
    with SessionLocal() as db:
        return callback_barrier_dispatch_allowed_in_db(db, sender_id, receiver_id)


def get_current_mailbox_generation(mailbox_id: str) -> int | None:
    """Return the current logical generation without rewriting receiver caches."""
    with SessionLocal() as db:
        value = db.query(MailboxModel.generation).filter_by(id=mailbox_id).scalar()
        return cast(int | None, value)


def get_mailbox_consumption_cursor(terminal_id: str) -> int | None:
    """Best-effort: return consumed_through_id for the mailbox owning terminal_id.

    Returns None (fail-open) when no mailbox exists or on any DB error.
    """
    try:
        with SessionLocal() as db:
            value = (
                db.query(MailboxModel.consumed_through_id)
                .filter(MailboxModel.current_terminal_id == terminal_id)
                .scalar()
            )
            return int(value) if value is not None else None
    except Exception:
        return None


def _receiver_is_terminal_or_mailbox_address(db: Any, receiver_id: str | None) -> bool:
    if not receiver_id:
        return False
    if db.query(TerminalModel.id).filter(TerminalModel.id == receiver_id).first() is not None:
        return True
    return _mailbox_id_for_terminal(db, receiver_id) is not None


def create_terminal(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    shell_command: Optional[str] = None,
    caller_id: Optional[str] = None,
    lifecycle: str = "ephemeral",
    provider_session_id: Optional[str] = None,
    init_state: str = "ready",
    init_started_at: Optional[datetime] = None,
    init_owner_epoch: Optional[str] = None,
    init_deadline_s: Optional[float] = None,
    dispatch_barrier: dict[str, Any] | None = None,
    engine: Optional[str] = None,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    working_directory: Optional[str] = None,
    worktree_info: Optional[Dict[str, str]] = None,
    authority_files: Optional[List[Dict[str, str]]] = None,
    resolved_model: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create terminal metadata record."""
    import json as _json

    with SessionLocal() as db:
        caller_mailbox_id = _mailbox_id_for_terminal(db, caller_id)
        terminal = TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile,
            working_directory=working_directory,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
            shell_command=shell_command,
            caller_id=caller_id,
            lifecycle=lifecycle,
            instance_id=os.environ.get("CAO_INSTANCE_ID") or None,
            caller_mailbox_id=caller_mailbox_id,
            provider_session_id=provider_session_id,
            init_state=init_state,
            init_started_at=init_started_at,
            init_owner_epoch=init_owner_epoch,
            init_deadline_s=init_deadline_s,
            engine=engine,
            group=_json.dumps(group) if group else None,
            metadata_json=_json.dumps(metadata) if metadata else None,
            worktree_info=_json.dumps(worktree_info) if worktree_info else None,
            resolved_model=resolved_model,
            auth_token=auth_token,
        )
        db.add(terminal)
        db.flush()
        db.query(TerminalModel).filter(TerminalModel.id == terminal_id).update(
            {TerminalModel.lifecycle_generation: (TerminalModel.lifecycle_generation + 1)},
            synchronize_session=False,
        )
        db.refresh(terminal)
        if dispatch_barrier is not None:
            if caller_id is None:
                raise ValueError("barrier_owner_not_found")
            attach_terminal_dispatch_barrier(
                db,
                sender_id=caller_id,
                terminal_id=terminal_id,
                profile_name=agent_profile,
                dispatch_barrier=dispatch_barrier,
            )
        # F129: Register frozen authority pins in the SAME transaction
        if authority_files:
            from cli_agent_orchestrator.services.authority_pin_service import (
                register_frozen_pins,
            )

            register_frozen_pins(
                db,
                task_key=terminal_id,
                authority_files=authority_files,
                registered_by=caller_id or "unknown",
            )
        db.commit()
        invalidate_terminal_metadata_cache(terminal.id)
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "working_directory": terminal.working_directory,
            "allowed_tools": allowed_tools,
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
            "lifecycle": terminal.lifecycle,
            "reparented_from": terminal.reparented_from,
            "instance_id": terminal.instance_id,
            "caller_mailbox_id": (
                terminal.caller_mailbox_id if _terminal_mailbox_column_available(db) else None
            ),
            "provider_session_id": terminal.provider_session_id,
            "recovery_state": terminal.recovery_state,
            "recovery_error": terminal.recovery_error,
            "recovery_updated_at": terminal.recovery_updated_at,
            "fallback_terminal_id": terminal.fallback_terminal_id,
            "init_state": terminal.init_state,
            "init_started_at": terminal.init_started_at,
            "init_owner_epoch": terminal.init_owner_epoch,
            "init_failure_token": terminal.init_failure_token,
            "init_deadline_s": terminal.init_deadline_s,
            "lifecycle_generation": terminal.lifecycle_generation,
            "engine": terminal.engine,
            # Normalized the same way as what was actually stored (an empty
            # container is stored as NULL, same as omitted) -- self-ROAST
            # finding: echoing the raw `group`/`metadata` input here made
            # create_terminal(group=[]) return {"group": []} while an
            # immediately-following get_terminal_metadata() on the same row
            # returns {"group": None}, an API-consistency gap.
            "group": group if group else None,
            "metadata": metadata if metadata else None,
        }


class WarmIntentPublishError(RuntimeError):
    """Terminal and warm-intent publication could not settle atomically."""


def create_terminal_with_warm_intent(
    *,
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str],
    allowed_tools: Optional[List[str]],
    caller_id: Optional[str],
    lifecycle: str = "ephemeral",
    parent_base_name: Optional[str],
    fork_mode: Optional[str],
    cas_hook=None,
    init_state: str = "ready",
    init_started_at: Optional[datetime] = None,
    init_owner_epoch: Optional[str] = None,
    init_deadline_s: Optional[float] = None,
    dispatch_barrier: dict[str, Any] | None = None,
    engine: Optional[str] = None,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    working_directory: Optional[str] = None,
    worktree_info: Optional[Dict[str, str]] = None,
    authority_files: Optional[List[Dict[str, str]]] = None,
    resolved_model: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish terminal metadata and a fork-only warm intent together."""
    import json as _json
    import uuid

    with SessionLocal.begin() as db:
        caller_mailbox_id = _mailbox_id_for_terminal(db, caller_id)
        terminal = TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
            caller_id=caller_id,
            lifecycle=lifecycle,
            instance_id=os.environ.get("CAO_INSTANCE_ID") or None,
            caller_mailbox_id=caller_mailbox_id,
            init_state=init_state,
            init_started_at=init_started_at,
            init_owner_epoch=init_owner_epoch,
            init_deadline_s=init_deadline_s,
            engine=engine,
            group=_json.dumps(group) if group else None,
            metadata_json=_json.dumps(metadata) if metadata else None,
            working_directory=working_directory,
            worktree_info=_json.dumps(worktree_info) if worktree_info else None,
            resolved_model=resolved_model,
            auth_token=auth_token,
        )
        db.add(terminal)
        db.flush()
        db.query(TerminalModel).filter(TerminalModel.id == terminal_id).update(
            {TerminalModel.lifecycle_generation: (TerminalModel.lifecycle_generation + 1)},
            synchronize_session=False,
        )
        db.refresh(terminal)
        if dispatch_barrier is not None:
            if caller_id is None:
                raise ValueError("barrier_owner_not_found")
            attach_terminal_dispatch_barrier(
                db,
                sender_id=caller_id,
                terminal_id=terminal_id,
                profile_name=agent_profile,
                dispatch_barrier=dispatch_barrier,
            )
        # F129: Register frozen authority pins in the SAME transaction
        if authority_files:
            from cli_agent_orchestrator.services.authority_pin_service import (
                register_frozen_pins,
            )

            register_frozen_pins(
                db,
                task_key=terminal_id,
                authority_files=authority_files,
                registered_by=caller_id or "unknown",
            )
        if fork_mode == "fork" and parent_base_name and agent_profile:
            # (frozen pins already registered above)
            claimed = False
            for attempt in range(3):
                dead = (
                    db.query(WarmIntentModel)
                    .outerjoin(
                        TerminalModel, TerminalModel.id == WarmIntentModel.worker_terminal_id
                    )
                    .filter(
                        WarmIntentModel.session_name == tmux_session,
                        WarmIntentModel.worker_profile == agent_profile,
                        WarmIntentModel.parent_base_name == parent_base_name,
                        TerminalModel.id.is_(None),
                    )
                    .order_by(WarmIntentModel.created_at, WarmIntentModel.intent_id)
                    .first()
                )
                if dead is None:
                    db.add(
                        WarmIntentModel(
                            intent_id=str(uuid.uuid4()),
                            worker_terminal_id=terminal_id,
                            session_name=tmux_session,
                            worker_profile=agent_profile,
                            parent_base_name=parent_base_name,
                            provider=provider,
                            created_at=_utcnow(),
                        )
                    )
                    claimed = True
                    break
                old_id = dead.worker_terminal_id
                if cas_hook and cas_hook(attempt, old_id, db) is False:
                    db.expire_all()
                    continue
                changed = (
                    db.query(WarmIntentModel)
                    .filter(
                        WarmIntentModel.intent_id == dead.intent_id,
                        WarmIntentModel.worker_terminal_id == old_id,
                        ~db.query(TerminalModel).filter(TerminalModel.id == old_id).exists(),
                    )
                    .update(
                        {
                            "worker_terminal_id": terminal_id,
                            "replaces_worker_terminal_id": old_id,
                            "created_at": _utcnow(),
                        },
                        synchronize_session=False,
                    )
                )
                if changed:
                    claimed = True
                    break
                db.expire_all()
            if not claimed:
                raise WarmIntentPublishError("db_publish_failed")
        db.flush()
        return {"id": terminal_id, "tmux_session": tmux_session, "tmux_window": tmux_window}


# ---------------------------------------------------------------------------
# F351: TTL cache for get_terminal_metadata — eliminates repeated identical DB
# queries within the same tick cycle (watchdog, status_monitor, delivery all
# call this for the same terminal_id within milliseconds of each other).
#
# CPython GIL guarantees atomic dict get/set/pop, so the read/write hot path
# is lock-free. The lock is only held during clear() to prevent
# iteration-during-mutation in test fixtures.
# ---------------------------------------------------------------------------
from threading import Lock as _CacheLock

_terminal_metadata_cache: Dict[str, tuple[float, Any]] = {}
_terminal_metadata_cache_lock = _CacheLock()
_TERMINAL_METADATA_TTL_S = 2.0


def invalidate_terminal_metadata_cache(terminal_id: str) -> None:
    """Evict a terminal's cached metadata after a mutation."""
    _terminal_metadata_cache.pop(terminal_id, None)
    # Also evict session-level entries that may include stale data
    for k in list(_terminal_metadata_cache):
        if k.startswith("__session__"):
            _terminal_metadata_cache.pop(k, None)


def clear_terminal_metadata_cache() -> None:
    """Clear the entire cache. Called by test fixtures (B2) to prevent cross-test leakage."""
    with _terminal_metadata_cache_lock:
        _terminal_metadata_cache.clear()


def get_terminal_metadata(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Get terminal metadata by ID (F351: TTL-cached to eliminate N+1 per tick)."""
    import json as _json

    now = time.monotonic()
    entry = _terminal_metadata_cache.get(terminal_id)
    if entry is not None and (now - entry[0]) < _TERMINAL_METADATA_TTL_S:
        return entry[1]

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            logger.warning(f"Terminal metadata not found for terminal_id: {terminal_id}")
            # F351: Do NOT cache None results — the terminal may be mid-creation
            # and the row not yet committed/visible. Caching None for the full
            # TTL causes wait_until_status to miss newly-created terminals.
            return None
        logger.debug(
            f"Retrieved terminal metadata for {terminal_id}: provider={terminal.provider}, session={terminal.tmux_session}"
        )
        allowed_tools = _json.loads(terminal.allowed_tools) if terminal.allowed_tools else None
        group = _json.loads(terminal.group) if terminal.group else None
        metadata = _json.loads(terminal.metadata_json) if terminal.metadata_json else None
        worktree_info_raw = (
            _json.loads(terminal.worktree_info) if isinstance(terminal.worktree_info, str) else None
        )
        result: Dict[str, Any] = {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "working_directory": terminal.working_directory,
            "allowed_tools": allowed_tools,
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
            "lifecycle": terminal.lifecycle,
            "reparented_from": terminal.reparented_from,
            "caller_mailbox_id": terminal.caller_mailbox_id,
            "provider_session_id": terminal.provider_session_id,
            "recovery_state": terminal.recovery_state,
            "recovery_error": terminal.recovery_error,
            "recovery_updated_at": terminal.recovery_updated_at,
            "fallback_terminal_id": terminal.fallback_terminal_id,
            "init_state": terminal.init_state,
            "init_started_at": terminal.init_started_at,
            "init_owner_epoch": terminal.init_owner_epoch,
            "init_failure_token": terminal.init_failure_token,
            "init_deadline_s": terminal.init_deadline_s,
            "lifecycle_generation": terminal.lifecycle_generation,
            "engine": terminal.engine or ("v2" if terminal.provider == "kiro_cli" else None),
            "group": group,
            "metadata": metadata,
            "worktree_info": worktree_info_raw,
            "last_active": terminal.last_active,
        }
        _terminal_metadata_cache[terminal_id] = (now, result)
        return result


def terminal_exists(terminal_id: str) -> bool:
    """Return whether a terminal row exists without logging on a miss."""
    with SessionLocal() as db:
        return (
            db.query(TerminalModel.id).filter(TerminalModel.id == terminal_id).first() is not None
        )


def update_terminal_group(terminal_id: str, group: Optional[List[str]]) -> bool:
    """Replace a terminal's group array. ``None``/``[]`` clears it (opts out of discovery)."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return False
        terminal.group = _json.dumps(group) if group else None
        db.commit()
    invalidate_terminal_metadata_cache(terminal_id)
    return True


def update_terminal_metadata(terminal_id: str, metadata: Optional[Dict[str, Any]]) -> bool:
    """Replace a terminal's free-form metadata dict. ``None``/``{}`` clears it.

    D12: preserves the reserved ``cao`` system namespace across worker full-replace.
    """
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return False
        # D12: preserve the reserved 'cao' key from existing metadata
        existing_cao: Dict[str, Any] | None = None
        if terminal.metadata_json:
            try:
                existing = _json.loads(str(terminal.metadata_json))
                if isinstance(existing, dict):
                    existing_cao = existing.get("cao")
            except (ValueError, TypeError):
                pass
        # Build new dict: worker payload + preserved cao namespace
        if metadata:
            new_meta = dict(metadata)
            # Strip any worker-provided 'cao' key (workers cannot write system namespace)
            new_meta.pop("cao", None)
            if existing_cao is not None:
                new_meta["cao"] = existing_cao
        else:
            # Clearing: keep only the system namespace if it exists
            new_meta = {"cao": existing_cao} if existing_cao else None
        terminal.metadata_json = _json.dumps(new_meta) if new_meta else None
        db.commit()
    invalidate_terminal_metadata_cache(terminal_id)
    return True


# ---------------------------------------------------------------------------
# F295 Half 2 D12: System metadata (reserved 'cao' namespace)
# ---------------------------------------------------------------------------

_SYSTEM_KEY = "cao"


def merge_terminal_system_metadata(terminal_id: str, patch: Dict[str, Any]) -> bool:
    """Read-modify-write the reserved ``cao`` sub-dict inside metadata_json.

    The ``cao`` key is system-owned and invisible to worker full-replace (D12).
    ``patch`` keys are merged (upsert); keys not in ``patch`` are preserved.
    """
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return False
        existing: Dict[str, Any] = {}
        if terminal.metadata_json:
            try:
                existing = _json.loads(str(terminal.metadata_json))
                if not isinstance(existing, dict):
                    existing = {}
            except (ValueError, TypeError):
                existing = {}
        cao_ns = existing.get(_SYSTEM_KEY)
        if not isinstance(cao_ns, dict):
            cao_ns = {}
        cao_ns.update(patch)
        existing[_SYSTEM_KEY] = cao_ns
        terminal.metadata_json = _json.dumps(existing)
        db.commit()
        # S3: evict from metadata cache after system metadata write
        invalidate_terminal_metadata_cache(terminal_id)
        return True


def read_terminal_system_metadata(terminal_id: str) -> Dict[str, Any]:
    """Read the reserved ``cao`` sub-dict from a terminal's metadata, or ``{}``."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal or not terminal.metadata_json:
            return {}
        try:
            metadata = _json.loads(str(terminal.metadata_json))
        except (ValueError, TypeError):
            return {}
        if not isinstance(metadata, dict):
            return {}
        cao_ns = metadata.get(_SYSTEM_KEY)
        return dict(cao_ns) if isinstance(cao_ns, dict) else {}


# ---------------------------------------------------------------------------
# F175: Dedicated dedup high-water accessors (clobber-proof)
# F476 D5: Readers/writers REMOVED — columns kept for backward compat.
# ---------------------------------------------------------------------------


def set_terminal_worktree_info(terminal_id: str, info: Dict[str, str]) -> None:
    """Recovery/idempotency seam: write worktree_info only when NULL.

    Accepts an identical retry (same JSON string). Raises ValueError on a
    conflicting second value. Normal creation writes via the INSERT in
    create_terminal and does not need this.
    """
    import json as _json

    encoded = _json.dumps(info)
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            raise ValueError(f"Terminal '{terminal_id}' not found")
        if terminal.worktree_info is None:
            terminal.worktree_info = encoded
            db.commit()
            invalidate_terminal_metadata_cache(terminal_id)
        elif terminal.worktree_info == encoded:
            # Identical retry -- idempotent no-op.
            pass
        else:
            raise ValueError(
                f"worktree_info for terminal '{terminal_id}' is already set to a different value"
            )


def get_terminal_worktree_info(terminal_id: str) -> Optional[Dict[str, str]]:
    """Return the dedicated worktree_info dict or None."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return None
        return _json.loads(terminal.worktree_info) if terminal.worktree_info else None


def get_terminal_group(terminal_id: str) -> Optional[List[str]]:
    """Return a terminal's own group array, or None if unset or the terminal doesn't exist."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal or not terminal.group:
            return None
        return cast(List[str], _json.loads(terminal.group))


def list_siblings_by_group_prefix(
    caller_id: str,
    prefix: List[str],
    caller_session: Optional[str] = None,
    cross_session: bool = False,
) -> List[Dict[str, Any]]:
    """Return ``{id, group, metadata}`` for every OTHER terminal sharing ``prefix``.

    ``prefix`` is the caller's own group truncated to the (already-clamped)
    depth — this function does no clamping itself, it only matches. A
    candidate terminal with no group, or a group shorter than ``len(prefix)``,
    is excluded rather than compared partially or raising (#432).

    Session-scoped by default (issue #432 design discussion, tedswinyar +
    klabulan, 2026-07-17/18): ``caller_session`` (the caller's own
    ``tmux_session``) is an implicit, non-bypassable first filter ON TOP of
    the group-prefix match, unless ``cross_session=True`` is explicitly
    passed. Without this, two unrelated CAO sessions that happen to reuse
    the same ``group`` prefix (a naming collision, a copy-pasted template,
    two features that picked the same tenant/project id) would silently
    discover each other -- the same class of "implicitly-scoped state that
    turns out not to be" mistake cited in that discussion's incident
    history. Cross-session discovery is a legitimate use case and stays
    available, just opt-in rather than the unstated default.

    ``group`` is stored JSON-encoded (see ``TerminalModel.group``), so the
    query prefilters with a SQL ``LIKE`` prefix match on that encoding before
    loading/decoding candidate rows in Python (Copilot review, PR #433) —
    without it this scanned and JSON-decoded every grouped terminal on the
    server regardless of how narrow ``prefix`` is. Because ``json.dumps``
    closes each string element in a quote immediately, the encoded prefix
    (full array minus its trailing ``]``) can't false-positive match a
    longer sibling element that merely shares a text prefix (e.g. prefix
    element ``"project_5"`` vs. a sibling group containing ``"project_50"``
    — the sibling's extra ``0`` before its closing ``"`` breaks the SQL
    match). The exact Python-level comparison below is kept regardless, as
    the source of truth — the SQL match only narrows candidates (a prefilter
    defect can only cause a false negative here, i.e. a missed perf win,
    never a false positive / correctness or security regression).

    This SQL-level match assumes the stored ``group`` was encoded with the
    same ``json.dumps`` defaults used below (notably ``ensure_ascii=True``,
    the default) — true today of both write paths (``create_terminal`` and
    ``update_terminal_group``), which both use plain ``json.dumps(group)``.
    If either write path ever changes its encoding, this prefilter must
    change with it.

    A single row with corrupt ``group`` JSON (e.g. hand-edited DB, a future
    write-path bug) is logged and excluded rather than raising and failing
    discovery for every OTHER terminal in the same request (tedswinyar, PR
    #433 review). Corrupt ``metadata`` JSON on an otherwise-matching sibling
    is likewise logged and reported back as ``metadata=None`` -- the sibling
    itself is still real and discoverable, only its metadata is unreadable.
    """
    import json as _json

    depth = len(prefix)
    # Encode the prefix array and drop its trailing ']' so this matches both
    # a sibling group of the same length and a longer one that starts with
    # it, e.g. prefix ["a", "b"] -> '["a", "b"' matches '["a", "b"]' and
    # '["a", "b", "c"]'.
    like_prefix = _json.dumps(prefix)[:-1]
    with SessionLocal() as db:
        query = db.query(TerminalModel).filter(
            TerminalModel.id != caller_id,
            TerminalModel.group.isnot(None),
            TerminalModel.group.startswith(like_prefix, autoescape=True),
        )
        if not cross_session and caller_session is not None:
            query = query.filter(TerminalModel.tmux_session == caller_session)
        rows = query.all()
        siblings = []
        for row in rows:
            try:
                sibling_group = _json.loads(row.group)
                if not isinstance(sibling_group, list):
                    raise ValueError(f"decoded to {type(sibling_group).__name__}, expected list")
            except (TypeError, ValueError) as e:
                logger.warning(
                    "list_siblings_by_group_prefix: skipping terminal %s -- "
                    "corrupt group JSON (%s)",
                    row.id,
                    e,
                )
                continue
            if len(sibling_group) < depth:
                continue
            if sibling_group[:depth] == prefix:
                metadata = None
                if row.metadata_json:
                    try:
                        metadata = _json.loads(row.metadata_json)
                    except (TypeError, ValueError) as e:
                        logger.warning(
                            "list_siblings_by_group_prefix: terminal %s has "
                            "corrupt metadata JSON (%s); returning it with "
                            "metadata=None",
                            row.id,
                            e,
                        )
                siblings.append(
                    {
                        "id": row.id,
                        "group": sibling_group,
                        "metadata": metadata,
                    }
                )
        return siblings


def list_terminals_by_session(tmux_session: str) -> List[Dict[str, Any]]:
    """List all terminals in a tmux session (F351: TTL-cached)."""
    now = time.monotonic()
    cache_key = f"__session__{tmux_session}"
    entry = _terminal_metadata_cache.get(cache_key)
    if entry is not None and (now - entry[0]) < _TERMINAL_METADATA_TTL_S:
        return entry[1]

    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).all()
        results = []
        for t in terminals:
            try:
                results.append(
                    {
                        "id": t.id,
                        "tmux_session": t.tmux_session,
                        "tmux_window": t.tmux_window,
                        "provider": t.provider,
                        "agent_profile": t.agent_profile,
                        "working_directory": t.working_directory,
                        "allowed_tools": (
                            __import__("json").loads(t.allowed_tools)
                            if isinstance(t.allowed_tools, str) and t.allowed_tools
                            else None
                        ),
                        "shell_command": t.shell_command,
                        "caller_id": t.caller_id,
                        "caller_mailbox_id": t.caller_mailbox_id,
                        "lifecycle": t.lifecycle,
                        "reparented_from": t.reparented_from,
                        "provider_session_id": t.provider_session_id,
                        "recovery_state": t.recovery_state,
                        "recovery_error": t.recovery_error,
                        "recovery_updated_at": t.recovery_updated_at,
                        "fallback_terminal_id": t.fallback_terminal_id,
                        "init_state": t.init_state,
                        "init_started_at": t.init_started_at,
                        "init_owner_epoch": t.init_owner_epoch,
                        "init_failure_token": t.init_failure_token,
                        "init_deadline_s": t.init_deadline_s,
                        "engine": t.engine or ("v2" if t.provider == "kiro_cli" else None),
                        "last_active": t.last_active,
                        "metadata": (
                            __import__("json").loads(t.metadata_json) if t.metadata_json else None
                        ),
                    }
                )
            except (ObjectDeletedError, DetachedInstanceError, StaleDataError) as exc:
                # F264: skip stale/zombie rows whose attribute load raises
                # ObjectDeletedError (or similar) instead of crashing the pass
                logger.debug("list_terminals_by_session: skipping stale row: %s", exc)
                continue
    _terminal_metadata_cache[cache_key] = (now, results)
    return results


def update_last_active(terminal_id: str) -> bool:
    """Update last active timestamp."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.last_active = _utcnow()
            db.commit()
            invalidate_terminal_metadata_cache(terminal_id)
            return True
        return False


def update_terminal_shell_command(terminal_id: str, shell_command: str) -> bool:
    """Update the shell_command baseline for a terminal."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.shell_command = shell_command
            db.commit()
            invalidate_terminal_metadata_cache(terminal_id)
            return True
        return False


def update_terminal_resolved_model(terminal_id: str, resolved_model: str) -> bool:
    """F127: Persist the resolved model string for a terminal."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.resolved_model = resolved_model
            db.commit()
            invalidate_terminal_metadata_cache(terminal_id)
            return True
        return False


def update_terminal_tmux_window(terminal_id: str, new_tmux_window: str) -> bool:
    """Update a terminal's window unless another row in its session owns it."""
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal is None:
            db.rollback()
            return False
        conflict = (
            db.query(TerminalModel.id)
            .filter(
                TerminalModel.tmux_session == terminal.tmux_session,
                TerminalModel.tmux_window == new_tmux_window,
                TerminalModel.id != terminal_id,
            )
            .first()
        )
        if conflict is not None:
            db.rollback()
            return False
        terminal.tmux_window = new_tmux_window
        db.commit()
        invalidate_terminal_metadata_cache(terminal_id)
        return True


def update_terminal_provider_session_id_if_null(terminal_id: str, session_uuid: str) -> str | None:
    """Claim an unset provider session id and return the persisted winner."""
    with SessionLocal.begin() as db:
        db.query(TerminalModel).filter(
            TerminalModel.id == terminal_id,
            TerminalModel.provider_session_id.is_(None),
        ).update({TerminalModel.provider_session_id: session_uuid}, synchronize_session=False)
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        result = terminal.provider_session_id if terminal else None
    invalidate_terminal_metadata_cache(terminal_id)
    return result


def update_terminal_provider_session_id(terminal_id: str, session_uuid: str) -> bool:
    """Explicitly set a provider session id (base registration/allocated UUID paths)."""
    with SessionLocal.begin() as db:
        changed = (
            db.query(TerminalModel)
            .filter(TerminalModel.id == terminal_id)
            .update({TerminalModel.provider_session_id: session_uuid}, synchronize_session=False)
        )
    invalidate_terminal_metadata_cache(terminal_id)
    return changed > 0


def update_terminal_runtime_identity(
    terminal_id: str,
    session_uuid: str,
    shell_command: str | None,
    *,
    supersede_other_claims: bool = False,
    require_published_uuid: bool = False,
) -> bool:
    """Confirm identity and optionally transfer the UUID claim atomically.

    The conditional new-row UPDATE runs first and establishes SQLite write
    intent before the claimant update; no deferred read snapshot is trusted.
    """
    with SessionLocal.begin() as db:
        values: dict[str, Any] = {"provider_session_id": session_uuid}
        if shell_command:
            values["shell_command"] = shell_command
        query = db.query(TerminalModel).filter(TerminalModel.id == terminal_id)
        if supersede_other_claims or require_published_uuid:
            query = query.filter(TerminalModel.provider_session_id == session_uuid)
        else:
            query = query.filter(TerminalModel.provider_session_id.is_(None))
        changed = query.update(values, synchronize_session=False)
        if changed != 1:
            return False
        if supersede_other_claims:
            db.query(TerminalModel).filter(
                TerminalModel.provider_session_id == session_uuid,
                TerminalModel.id != terminal_id,
            ).update({"provider_session_id": None}, synchronize_session=False)
    invalidate_terminal_metadata_cache(terminal_id)
    return True


def _reactivate_parked_rows_in_db(
    db: Session,
    *,
    source_terminal_id: str,
    source_lifecycle_generation: int,
    target_terminal_id: str,
    target_lifecycle_generation: int,
    move_mailbox_authority: bool,
) -> list[int]:
    """Reactivate only mail owned by the exact resumed source incarnation.

    Returns list of reactivated inbox row IDs (F136-D3: callers use this for replay enqueue).
    """
    reactivated_ids: list[int] = []
    incarnation = (
        db.query(MailboxIncarnationModel).filter_by(terminal_id=source_terminal_id).one_or_none()
    )
    if incarnation is not None:
        mailbox = db.query(MailboxModel).filter_by(id=incarnation.mailbox_id).one_or_none()
        if (
            mailbox is not None
            and mailbox.current_terminal_id == source_terminal_id
            and int(mailbox.generation) == int(incarnation.generation)
        ):
            if move_mailbox_authority:
                mailbox.current_terminal_id = target_terminal_id
                mailbox.updated_at = _utcnow()
            # Collect IDs before bulk update
            logical_ids = [
                row_id
                for (row_id,) in db.query(InboxModel.id)
                .filter(
                    InboxModel.status == MessageStatus.PARKED.value,
                    InboxModel.logical_receiver_id == incarnation.mailbox_id,
                    InboxModel.owner_receiver_id == source_terminal_id,
                    InboxModel.owner_generation == int(incarnation.generation),
                )
                .all()
            ]
            if logical_ids:
                db.query(InboxModel).filter(InboxModel.id.in_(logical_ids)).update(
                    {
                        InboxModel.status: MessageStatus.PENDING.value,
                        InboxModel.receiver_id: target_terminal_id,
                        InboxModel.enqueue_generation: int(mailbox.generation),
                    },
                    synchronize_session=False,
                )
                reactivated_ids.extend(logical_ids)
    # Also reactivate non-logical (direct) parked rows
    direct_ids = [
        row_id
        for (row_id,) in db.query(InboxModel.id)
        .filter(
            InboxModel.status == MessageStatus.PARKED.value,
            InboxModel.logical_receiver_id.is_(None),
            InboxModel.owner_receiver_id == source_terminal_id,
            InboxModel.owner_generation == source_lifecycle_generation,
        )
        .all()
    ]
    if direct_ids:
        db.query(InboxModel).filter(InboxModel.id.in_(direct_ids)).update(
            {
                InboxModel.status: MessageStatus.PENDING.value,
                InboxModel.receiver_id: target_terminal_id,
                InboxModel.enqueue_generation: target_lifecycle_generation,
            },
            synchronize_session=False,
        )
        reactivated_ids.extend(direct_ids)
    return reactivated_ids


def settle_terminal_rebound(
    terminal_id: str,
    session_uuid: str,
    shell_command: str,
) -> int:
    """Atomically persist proven runtime identity and the healthy projection."""
    with SessionLocal.begin() as db:
        row = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
        if row is None:
            return 0
        source_generation = int(row.lifecycle_generation)
        # F138: Queue old active incarnation for reconciliation before replacement.
        _old_inc = (
            db.query(ProcessIncarnationModel)
            .filter_by(terminal_id=terminal_id, state="active")
            .one_or_none()
        )
        if _old_inc is not None:
            _old_inc.state = "reconcile_pending"
            # Job insertion deferred to after commit (below)
        row.provider_session_id = session_uuid
        row.shell_command = shell_command
        row.recovery_state = "rebound"
        row.recovery_error = None
        row.recovery_updated_at = _utcnow()
        row.lifecycle_generation = source_generation + 1
        db.flush()
        reactivated_ids = _reactivate_parked_rows_in_db(
            db,
            source_terminal_id=terminal_id,
            source_lifecycle_generation=source_generation,
            target_terminal_id=terminal_id,
            target_lifecycle_generation=int(row.lifecycle_generation),
            move_mailbox_authority=False,
        )
        # F136-D3: enqueue replay for reactivated rows below cursor
        if reactivated_ids:
            inc = db.query(MailboxIncarnationModel).filter_by(terminal_id=terminal_id).one_or_none()
            if inc:
                mb: Any = db.query(MailboxModel).filter_by(id=inc.mailbox_id).one_or_none()
                if mb and mb.callback_notified_through_id is not None:
                    cursor_val = int(mb.callback_notified_through_id)
                    below = [rid for rid in reactivated_ids if rid <= cursor_val]
                    if below:
                        enqueue_callback_replay(
                            db, mailbox_id=str(inc.mailbox_id), inbox_row_ids=below
                        )
        _f138_old_inc_id = _old_inc.id if _old_inc is not None else None
        _result_generation = int(row.lifecycle_generation)
    # F138: Queue reconciliation job for old incarnation after DB commit
    if _f138_old_inc_id is not None:
        try:
            f138_request_reconciliation(incarnation_id=_f138_old_inc_id, source="settle_rebound")
        except Exception:
            logger.warning(
                "f138_rebound_reconcile_queue_failed terminal=%s incarnation=%s",
                terminal_id,
                _f138_old_inc_id,
            )
    # S3: evict from metadata cache after rebound settle
    invalidate_terminal_metadata_cache(terminal_id)
    return _result_generation


def fail_terminal_rebound(
    terminal_id: str,
    lifecycle_generation: int,
    error: str,
) -> int:
    """Atomically fail a proven rebound and re-park its reactivated mail."""
    with SessionLocal.begin() as db:
        terminal = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
        if terminal is None:
            raise RuntimeError("terminal_missing_after_rebound")
        if int(terminal.lifecycle_generation) != lifecycle_generation:
            raise RuntimeError("rebound_generation_changed")

        terminal.recovery_state = "rebind_failed"
        terminal.recovery_error = error[:2048]
        terminal.recovery_updated_at = _utcnow()

        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.logical_receiver_id.is_(None),
                InboxModel.receiver_id == terminal_id,
                InboxModel.enqueue_generation == lifecycle_generation,
                InboxModel.owner_receiver_id == terminal_id,
                InboxModel.owner_generation == lifecycle_generation - 1,
            )
            .update(
                {
                    InboxModel.status: MessageStatus.PARKED.value,
                    InboxModel.digested_into: None,
                },
                synchronize_session=False,
            )
        )

        incarnation = (
            db.query(MailboxIncarnationModel).filter_by(terminal_id=terminal_id).one_or_none()
        )
        if incarnation is not None:
            mailbox_generation = (
                db.query(MailboxModel.generation).filter_by(id=incarnation.mailbox_id).scalar()
            )
            if type(mailbox_generation) is int:
                changed += (
                    db.query(InboxModel)
                    .filter(
                        InboxModel.status == MessageStatus.PENDING.value,
                        InboxModel.logical_receiver_id == incarnation.mailbox_id,
                        InboxModel.receiver_id == terminal_id,
                        InboxModel.enqueue_generation == int(mailbox_generation),
                        InboxModel.owner_receiver_id == terminal_id,
                        InboxModel.owner_generation == int(incarnation.generation),
                    )
                    .update(
                        {
                            InboxModel.status: MessageStatus.PARKED.value,
                            InboxModel.digested_into: None,
                        },
                        synchronize_session=False,
                    )
                )
        _changed = int(changed)
    # S3: evict from metadata cache after rebound failure
    invalidate_terminal_metadata_cache(terminal_id)
    return _changed


def set_terminal_recovery_state(
    terminal_id: str,
    state: RecoveryState | None,
    error: str | None = None,
    fallback_terminal_id: str | None = None,
) -> bool:
    """Atomically set the durable recovery projection for one terminal."""
    with SessionLocal.begin() as db:
        values = {
            "recovery_state": state,
            "recovery_error": error[:2048] if error else None,
            "recovery_updated_at": _utcnow(),
        }
        if fallback_terminal_id is not None:
            values["fallback_terminal_id"] = fallback_terminal_id
        updated = (
            db.query(TerminalModel)
            .filter_by(id=terminal_id)
            .update(values, synchronize_session=False)
            > 0
        )
    # S3: evict from metadata cache after recovery state write
    invalidate_terminal_metadata_cache(terminal_id)
    return updated


def quarantine_terminal_owner(
    terminal_id: str,
    session_uuid: str | None,
    error: str,
) -> str:
    """Atomically retain attempted native ownership and quarantine projection."""
    with SessionLocal.begin() as db:
        row = db.query(TerminalModel).filter_by(id=terminal_id).first()
        if row is None:
            return ""
        association = "skipped_existing_owner"
        if row.provider_session_id is None and session_uuid:
            associated = (
                db.query(TerminalModel)
                .filter(
                    TerminalModel.id == terminal_id,
                    TerminalModel.provider_session_id.is_(None),
                    ~db.query(TerminalModel.id)
                    .filter(
                        TerminalModel.provider_session_id == session_uuid,
                        TerminalModel.id != terminal_id,
                    )
                    .exists(),
                )
                .update({"provider_session_id": session_uuid}, synchronize_session=False)
            )
            association = "associated" if associated == 1 else "skipped_existing_owner"
        row.recovery_state = "rebind_failed"
        row.recovery_error = error[:2048]
        row.recovery_updated_at = _utcnow()
        _association = association
    # S3: evict from metadata cache after quarantine write
    invalidate_terminal_metadata_cache(terminal_id)
    return _association


def settle_terminal_fallback(old_terminal_id: str, new_terminal_id: str) -> int:
    """Commit fallback pointer, PENDING rewrites, and ready state together."""
    with SessionLocal.begin() as db:
        old = db.query(TerminalModel).filter_by(id=old_terminal_id).one()
        if old.recovery_state != "fallback_starting":
            raise RuntimeError("fallback_state_changed")
        new = db.query(TerminalModel).filter_by(id=new_terminal_id).first()
        if new is None:
            raise RuntimeError("fallback_terminal_missing")
        if not new.provider_session_id:
            raise RuntimeError("fallback_terminal_identity_missing")
        changed = 0
        pending_rows = (
            db.query(InboxModel)
            .filter(
                InboxModel.receiver_id == old_terminal_id,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .all()
        )
        for pending in pending_rows:
            pending.receiver_id = new_terminal_id
            if pending.logical_receiver_id:
                mailbox_generation = (
                    db.query(MailboxModel.generation)
                    .filter_by(id=pending.logical_receiver_id)
                    .scalar()
                )
                pending.enqueue_generation = cast(int | None, mailbox_generation)
            else:
                pending.enqueue_generation = int(new.lifecycle_generation)
            changed += 1
        reactivated_ids = _reactivate_parked_rows_in_db(
            db,
            source_terminal_id=old_terminal_id,
            source_lifecycle_generation=int(old.lifecycle_generation),
            target_terminal_id=new_terminal_id,
            target_lifecycle_generation=int(new.lifecycle_generation),
            move_mailbox_authority=True,
        )
        changed += len(reactivated_ids)
        # F136-D3: enqueue replay for reactivated rows below cursor
        if reactivated_ids:
            inc = (
                db.query(MailboxIncarnationModel)
                .filter_by(terminal_id=new_terminal_id)
                .one_or_none()
            )
            if inc:
                mb_row: Any = db.query(MailboxModel).filter_by(id=inc.mailbox_id).one_or_none()
                if mb_row and mb_row.callback_notified_through_id is not None:
                    cursor_val = int(mb_row.callback_notified_through_id)
                    below = [rid for rid in reactivated_ids if rid <= cursor_val]
                    if below:
                        enqueue_callback_replay(
                            db, mailbox_id=str(inc.mailbox_id), inbox_row_ids=below
                        )
        # S4: query child IDs before bulk update so we can invalidate their caches
        child_ids = [
            row.id
            for row in db.query(TerminalModel.id)
            .filter(TerminalModel.caller_id == old_terminal_id)
            .all()
        ]
        changed += (
            db.query(TerminalModel)
            .filter(TerminalModel.caller_id == old_terminal_id)
            .update(
                {
                    TerminalModel.caller_id: new_terminal_id,
                    TerminalModel.caller_mailbox_id: _mailbox_id_for_terminal(db, new_terminal_id),
                    TerminalModel.reparented_from: old_terminal_id,
                },
                synchronize_session=False,
            )
        )
        old.fallback_terminal_id = new_terminal_id
        old.recovery_state = "fallback_ready"
        old.recovery_error = None
        old.recovery_updated_at = _utcnow()
        db.query(TerminalModel).filter(
            TerminalModel.provider_session_id == new.provider_session_id,
            TerminalModel.id != new_terminal_id,
        ).update({"provider_session_id": None}, synchronize_session=False)
    invalidate_terminal_metadata_cache(old_terminal_id)
    invalidate_terminal_metadata_cache(new_terminal_id)
    # S4: invalidate reparented children so their cached caller_id is refreshed
    for child_id in child_ids:
        invalidate_terminal_metadata_cache(child_id)
    return changed


def has_unsettled_delivery_attempt(terminal_id: str) -> bool:
    with SessionLocal() as db:
        return (
            db.query(InboxDeliveryAttemptModel)
            .filter_by(receiver_terminal_id=terminal_id, settled_at=None)
            .first()
            is not None
        )


def create_transcript_binding(
    terminal_id: str,
    session_id: str,
    transcript_path: str,
    inode: int | None,
    source: str,
) -> Dict[str, Any]:
    """Append a server-timestamped transcript binding epoch."""
    with SessionLocal() as db:
        row = TranscriptBindingModel(
            terminal_id=terminal_id,
            session_id=session_id,
            transcript_path=transcript_path,
            inode=inode,
            source=source,
            received_at=_utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def recover_transcript_binding_if_current(
    terminal_id: str, stale_binding_id: int, transcript_path: str
) -> str:
    """CAS-mint a server recovery epoch against the still-current stale binding."""

    class _ObjectPairs(list[tuple[str, Any]]):
        pass

    try:
        with Path(transcript_path).open(encoding="utf-8") as stream:
            pairs = json.loads(stream.readline(), object_pairs_hook=_ObjectPairs)
        if not isinstance(pairs, _ObjectPairs):
            return "invalid_session_id"
        session_values = [value for key, value in pairs if key == "sessionId"]
        if (
            len(session_values) != 1
            or not isinstance(session_values[0], str)
            or not session_values[0]
        ):
            return "invalid_session_id"
        session_id = session_values[0]
        inode = Path(transcript_path).stat().st_ino
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "invalid_session_id"

    with SessionLocal() as db:
        current = (
            db.query(TranscriptBindingModel)
            .filter_by(terminal_id=terminal_id)
            .order_by(
                TranscriptBindingModel.received_at.desc(),
                TranscriptBindingModel.id.desc(),
            )
            .first()
        )
        if current is None or current.id != stale_binding_id:
            db.rollback()
            return "authority_changed"
        db.add(
            TranscriptBindingModel(
                terminal_id=terminal_id,
                session_id=session_id,
                transcript_path=transcript_path,
                inode=inode,
                source="server_recovery",
                received_at=_utcnow(),
            )
        )
        db.commit()
    from cli_agent_orchestrator.services.inbox_service import inbox_service

    inbox_service.reset_binding_episodes(terminal_id)
    return "inserted"


def get_current_transcript_binding(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest binding epoch using the deterministic epoch ordering."""
    if not terminal_id:
        return None
    try:
        with SessionLocal() as db:
            row = (
                db.query(TranscriptBindingModel)
                .filter_by(terminal_id=terminal_id)
                .order_by(
                    TranscriptBindingModel.received_at.desc(),
                    TranscriptBindingModel.id.desc(),
                )
                .first()
            )
            if row is None:
                return None
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}
    except Exception as exc:
        # Direct library consumers can resolve transcripts before init_db has
        # created the additive table. Server startup always initializes first.
        if "no such table: transcript_bindings" in str(exc):
            return None
        raise


def get_latest_compact_transcript_binding(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest compact binding epoch using deterministic ordering."""
    if not terminal_id:
        return None
    try:
        with SessionLocal() as db:
            row = (
                db.query(TranscriptBindingModel)
                .filter_by(terminal_id=terminal_id, source="compact")
                .order_by(
                    TranscriptBindingModel.received_at.desc(),
                    TranscriptBindingModel.id.desc(),
                )
                .first()
            )
            if row is None:
                return None
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}
    except Exception as exc:
        if "no such table: transcript_bindings" in str(exc):
            return None
        raise


def get_latest_hook_transcript_binding(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest provider-hook binding (startup/resume/clear/compact)."""
    if not terminal_id:
        return None
    try:
        with SessionLocal() as db:
            row = (
                db.query(TranscriptBindingModel)
                .filter(
                    TranscriptBindingModel.terminal_id == terminal_id,
                    TranscriptBindingModel.source.in_(TRANSCRIPT_HOOK_BINDING_SOURCES),
                )
                .order_by(
                    TranscriptBindingModel.received_at.desc(),
                    TranscriptBindingModel.id.desc(),
                )
                .first()
            )
            if row is None:
                return None
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}
    except Exception as exc:
        if "no such table: transcript_bindings" in str(exc):
            return None
        raise


def register_provider_session(*, include_superseded: bool = False, **values: Any) -> Dict[str, Any]:
    """Atomically supersede a ready name and register its replacement."""
    if values.get("name") == "cold":
        raise ValueError("base_name_reserved:cold")
    if values.get("kind", "base") not in {"base", "anchor"}:
        raise ValueError("invalid_provider_session_kind")
    old_uuid: str | None = None
    cleanup_uuid: str | None = None
    cleanup_path: str | None = None
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        now = _utcnow()
        previous = (
            db.query(ProviderSessionModel)
            .filter_by(name=values["name"], status="ready")
            .with_for_update()
            .first()
        )
        old_uuid = previous.session_uuid if previous is not None else None
        inherited_claim = None
        if previous is not None and previous.session_uuid == values.get("session_uuid"):
            inherited_claim = previous.retained_persona_home
        if inherited_claim is None and values.get("session_uuid"):
            source = (
                db.query(ProviderSessionModel)
                .filter(
                    ProviderSessionModel.session_uuid == values["session_uuid"],
                    ProviderSessionModel.status == "ready",
                    ProviderSessionModel.retained_persona_home.isnot(None),
                )
                .order_by(ProviderSessionModel.id)
                .first()
            )
            inherited_claim = source.retained_persona_home if source is not None else None
        superseded_count = 0
        if previous is not None:
            previous_claim = previous.retained_persona_home
            previous.status = "superseded"
            previous.updated_at = now
            previous.retained_persona_home = None
            superseded_count = 1
            if old_uuid != values.get("session_uuid"):
                cleanup_uuid = old_uuid
                cleanup_path = previous_claim
        row_values = dict(values)
        row_values["retained_persona_home"] = inherited_claim
        row = ProviderSessionModel(**row_values, status="ready", created_at=now, updated_at=now)
        db.add(row)
        db.commit()
        db.refresh(row)
        result = provider_session_to_dict(row)
        if include_superseded:
            result["superseded"] = superseded_count > 0
    if cleanup_uuid is not None:
        from cli_agent_orchestrator.utils.persona_context import persona_cleanup

        persona_cleanup(cleanup_uuid, candidate_path=cleanup_path)
    return result


def get_provider_session_history(name: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = (
            db.query(ProviderSessionModel)
            .filter_by(name=name)
            .order_by(ProviderSessionModel.updated_at.desc(), ProviderSessionModel.id.desc())
            .first()
        )
        return provider_session_to_dict(row) if row else None


def list_ready_provider_sessions_for_session(session_name: str) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = (
            db.query(ProviderSessionModel)
            .filter_by(status="ready", session_name=session_name)
            .order_by(ProviderSessionModel.name)
            .all()
        )
        return [provider_session_to_dict(row) for row in rows]


def delete_terminal_and_warm_intent(
    terminal_id: str,
    *,
    preserve_warm_intent: bool = False,
    reparent_target_id: str | None = None,
) -> Dict[str, bool]:
    """Settle terminal-owned state and delete the row in one transaction."""
    with SessionLocal.begin() as db:
        terminal = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
        profile = terminal.agent_profile if terminal is not None else None
        target_candidate = (
            terminal.caller_id if reparent_target_id is None and terminal else reparent_target_id
        )
        target = (
            db.query(TerminalModel).filter_by(id=target_candidate).one_or_none()
            if target_candidate
            else None
        )
        target_id: str | None = None
        target_mailbox_id: str | None = None
        target_generation: int | None = None
        if target is not None:
            target_id, target_mailbox_id, target_generation = resolve_inbox_receiver(db, target.id)

        held = db.query(InboxModel).filter(
            InboxModel.receiver_id == terminal_id,
            InboxModel.status == MessageStatus.HELD.value,
        )
        prefix = f"[released from {terminal_id} ({profile or 'unknown'}) — terminal reaped]\n"
        _reap_flipped_rows: list[tuple[str, int, str | None]] = []
        for row in held.all():
            row.message = prefix + row.message
            if target_id is None:
                row.status = MessageStatus.CANCELLED.value
                row.failure_reason = "terminal_reaped_no_surviving_ancestor"
            else:
                row.receiver_id = target_id
                row.logical_receiver_id = target_mailbox_id
                row.enqueue_generation = target_generation or int(target.lifecycle_generation)
                row.status = MessageStatus.PENDING.value
                row.failure_reason = None
                _reap_flipped_rows.append(
                    (MessageStatus.PENDING.value, int(row.id), target_mailbox_id)
                )
        # F413 D7b: create obligations for flipped rows
        if _reap_flipped_rows:
            db.flush()
            _f413_qualify_and_create(db, _reap_flipped_rows)

        now = _barrier_now()
        owned_barriers = (
            db.query(CallbackBarrierModel)
            .filter(
                CallbackBarrierModel.state == "OPEN",
                CallbackBarrierModel.owner_terminal_id == terminal_id,
            )
            .all()
        )
        for barrier in owned_barriers:
            # F413 D7b: collect rows before bulk flip so we can create obligations
            _barrier_flip_rows = [
                (MessageStatus.PENDING.value, int(r.id), r.logical_receiver_id)
                for r in db.query(InboxModel)
                .filter(
                    InboxModel.barrier_id == barrier.id,
                    InboxModel.status == MessageStatus.HELD.value,
                )
                .all()
            ]
            db.query(InboxModel).filter(
                InboxModel.barrier_id == barrier.id,
                InboxModel.status == MessageStatus.HELD.value,
            ).update(
                {InboxModel.status: MessageStatus.PENDING.value},
                synchronize_session=False,
            )
            barrier.state = "CANCELLED"
            barrier.close_reason = "owner_gone"
            barrier.fired_at = now
            # F413 D7b: create obligations for qualifying flipped rows
            if _barrier_flip_rows:
                _f413_qualify_and_create(db, _barrier_flip_rows)

        # FAM-1: nullify mailbox authority when this terminal is the current incarnation
        db.query(MailboxModel).filter(MailboxModel.current_terminal_id == terminal_id).update(
            {MailboxModel.current_terminal_id: None},
            synchronize_session=False,
        )

        _mark_barrier_member_gone_in_db(db, terminal_id)
        # B3: query child IDs before bulk update so we can invalidate their caches
        child_ids = [
            row.id
            for row in db.query(TerminalModel.id)
            .filter(TerminalModel.caller_id == terminal_id)
            .all()
        ]
        child_values = {
            TerminalModel.caller_id: target.id if target is not None else None,
            TerminalModel.caller_mailbox_id: (
                _mailbox_id_for_terminal(db, target.id) if target is not None else None
            ),
            TerminalModel.reparented_from: terminal_id,
        }
        db.query(TerminalModel).filter(TerminalModel.caller_id == terminal_id).update(
            child_values,
            synchronize_session=False,
        )
        intent_deleted = False
        if not preserve_warm_intent:
            intent_deleted = (
                db.query(WarmIntentModel).filter_by(worker_terminal_id=terminal_id).delete() > 0
            )
        terminal_deleted = db.query(TerminalModel).filter_by(id=terminal_id).delete() > 0
    # F351: evict from metadata cache after deletion
    invalidate_terminal_metadata_cache(terminal_id)
    # B3: invalidate reparented children so their cached caller_id is refreshed
    for child_id in child_ids:
        invalidate_terminal_metadata_cache(child_id)
    return {
        "terminal_deleted": terminal_deleted,
        "intent_deleted": intent_deleted,
    }


def list_warm_intents(session_name: str) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = (
            db.query(WarmIntentModel)
            .filter_by(session_name=session_name)
            .order_by(WarmIntentModel.created_at, WarmIntentModel.intent_id)
            .all()
        )
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows]


def delete_warm_intents_for_session(session_name: str) -> int:
    with SessionLocal.begin() as db:
        return db.query(WarmIntentModel).filter_by(session_name=session_name).delete()


def increment_session_epoch(session_name: str) -> Dict[str, Any]:
    from sqlalchemy.dialects.sqlite import insert

    now = _utcnow()
    with SessionLocal.begin() as db:
        statement = (
            insert(SessionEpochModel)
            .values(
                session_name=session_name,
                count=1,
                last_epoch_at=now,
            )
            .on_conflict_do_update(
                index_elements=[SessionEpochModel.session_name],
                set_={"count": SessionEpochModel.count + 1, "last_epoch_at": now},
            )
            .returning(SessionEpochModel.count, SessionEpochModel.last_epoch_at)
        )
        count, last_epoch_at = db.execute(statement).one()
        return {"count": count, "last_epoch_at": last_epoch_at}


def get_session_epoch(session_name: str) -> Optional[Dict[str, Any]]:
    try:
        with SessionLocal() as db:
            row = db.query(SessionEpochModel).filter_by(session_name=session_name).first()
            return {"count": row.count, "last_epoch_at": row.last_epoch_at} if row else None
    except Exception as exc:
        if "no such table: session_epochs" in str(exc):
            return None
        raise


def delete_session_epoch(session_name: str) -> bool:
    with SessionLocal.begin() as db:
        return db.query(SessionEpochModel).filter_by(session_name=session_name).delete() > 0


def retire_provider_session(name: str) -> Optional[Dict[str, Any]]:
    """Atomically retire the current ready registration for ``name``."""
    cleanup_uuid: str | None = None
    cleanup_path: str | None = None
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.query(ProviderSessionModel).filter_by(name=name, status="ready").first()
        if row is None:
            return None
        cleanup_uuid = row.session_uuid
        cleanup_path = row.retained_persona_home
        row.status = "retired"
        row.retained_persona_home = None
        row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)
        result = provider_session_to_dict(row)
    if cleanup_uuid is not None:
        from cli_agent_orchestrator.utils.persona_context import persona_cleanup

        persona_cleanup(cleanup_uuid, candidate_path=cleanup_path)
    return result


def provider_session_to_dict(row: ProviderSessionModel) -> Dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def get_ready_provider_session(name: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = db.query(ProviderSessionModel).filter_by(name=name, status="ready").first()
        return provider_session_to_dict(row) if row else None


def update_provider_session_snapshot(
    row_id: int,
    *,
    git_sha: Optional[str],
    dirty_hashes: str,
    digest_head: str | None = None,
) -> Optional[Dict[str, Any]]:
    """CAS-refresh the snapshot for the same still-ready registry row."""
    with SessionLocal() as db:
        row = db.query(ProviderSessionModel).filter_by(id=row_id, status="ready").first()
        if row is None:
            return None
        row.git_sha = git_sha
        row.dirty_hashes = dirty_hashes
        row.digest_head = digest_head
        row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)
        return provider_session_to_dict(row)


def get_ready_provider_session_by_source_terminal(
    terminal_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the ready base owned by ``terminal_id``, if any."""
    with SessionLocal() as db:
        row = (
            db.query(ProviderSessionModel)
            .filter_by(source_terminal_id=terminal_id, status="ready")
            .first()
        )
        return provider_session_to_dict(row) if row else None


def get_retained_persona_home_for_terminal(terminal_id: str) -> Optional[str]:
    """Return a claimed retained home for any provider row owned by a terminal."""
    with SessionLocal() as db:
        row = (
            db.query(ProviderSessionModel)
            .filter(
                ProviderSessionModel.source_terminal_id == terminal_id,
                ProviderSessionModel.status == "ready",
                ProviderSessionModel.retained_persona_home.isnot(None),
            )
            .order_by(ProviderSessionModel.id.desc())
            .first()
        )
        return cast(Optional[str], row.retained_persona_home) if row is not None else None


def claim_retained_persona_home(session_uuid: str, destination: str) -> int:
    """Claim all currently-ready rows for a UUID with a guarded CAS update."""
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        count = (
            db.query(ProviderSessionModel)
            .filter(
                ProviderSessionModel.session_uuid == session_uuid,
                ProviderSessionModel.status == "ready",
                ProviderSessionModel.retained_persona_home.is_(None),
            )
            .update(
                {"retained_persona_home": destination, "updated_at": _utcnow()},
                synchronize_session=False,
            )
        )
        db.commit()
        return int(count)


def verify_retained_persona_claim(session_uuid: str, destination: str) -> int:
    with SessionLocal() as db:
        return int(
            db.query(ProviderSessionModel)
            .filter_by(
                session_uuid=session_uuid,
                status="ready",
                retained_persona_home=destination,
            )
            .count()
        )


def unclaim_retained_persona_home(session_uuid: str, destination: str) -> int:
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        count = (
            db.query(ProviderSessionModel)
            .filter_by(
                session_uuid=session_uuid,
                retained_persona_home=destination,
            )
            .update(
                {"retained_persona_home": None, "updated_at": _utcnow()}, synchronize_session=False
            )
        )
        db.commit()
        return int(count)


def persona_cleanup_claim(session_uuid: str) -> tuple[bool, Optional[str]]:
    """Clear a UUID's claim when no ready row remains; return its old path."""
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        ready = (
            db.query(ProviderSessionModel)
            .filter_by(session_uuid=session_uuid, status="ready")
            .count()
        )
        if ready:
            return False, None
        rows = (
            db.query(ProviderSessionModel)
            .filter(
                ProviderSessionModel.session_uuid == session_uuid,
                ProviderSessionModel.retained_persona_home.isnot(None),
            )
            .all()
        )
        path = next(
            (cast(str, row.retained_persona_home) for row in rows if row.retained_persona_home),
            None,
        )
        for row in rows:
            row.retained_persona_home = None
            row.updated_at = _utcnow()
        db.commit()
        return True, path


def list_retained_persona_claims() -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = (
            db.query(ProviderSessionModel)
            .filter(
                ProviderSessionModel.status == "ready",
                ProviderSessionModel.retained_persona_home.isnot(None),
            )
            .all()
        )
        return [provider_session_to_dict(row) for row in rows]


def clear_missing_retained_persona_claim(session_uuid: str, destination: str) -> int:
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        count = (
            db.query(ProviderSessionModel)
            .filter_by(session_uuid=session_uuid, retained_persona_home=destination)
            .update(
                {"retained_persona_home": None, "updated_at": _utcnow()}, synchronize_session=False
            )
        )
        db.commit()
        return int(count)


def get_provider_session_by_uuid(session_uuid: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = (
            db.query(ProviderSessionModel)
            .filter_by(session_uuid=session_uuid)
            .order_by(
                (ProviderSessionModel.status == "ready").desc(),
                ProviderSessionModel.updated_at.desc(),
                ProviderSessionModel.id.desc(),
            )
            .first()
        )
        if row is None or row.status == "retired":
            return None
        return provider_session_to_dict(row)


def list_ready_provider_sessions() -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        return [
            provider_session_to_dict(r)
            for r in db.query(ProviderSessionModel)
            .filter_by(status="ready")
            .order_by(ProviderSessionModel.name)
            .all()
        ]


def list_terminals_by_provider_session_id(session_uuid: str) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(TerminalModel).filter_by(provider_session_id=session_uuid).all()
        return [
            {
                "id": r.id,
                "tmux_session": r.tmux_session,
                "tmux_window": r.tmux_window,
                "provider": r.provider,
            }
            for r in rows
        ]


def list_all_terminals() -> List[Dict[str, Any]]:
    """List all terminals."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "working_directory": t.working_directory,
                "last_active": t.last_active,
                "caller_id": t.caller_id,
                "provider_session_id": t.provider_session_id,
                "recovery_state": t.recovery_state,
                "init_state": t.init_state,
                "init_started_at": t.init_started_at,
                "init_owner_epoch": t.init_owner_epoch,
                "init_failure_token": t.init_failure_token,
                "init_deadline_s": t.init_deadline_s,
            }
            for t in terminals
        ]


class ReadyCommitInvariantBreach(RuntimeError):
    """A ready commit failed after in-memory ownership became irrevocable."""


class _ReadyCommitVeto(RuntimeError):
    pass


def _clear_progress_fence(fence_fn, terminal_id: str) -> None:
    """Clear a SQLite progress handler, tolerating an already-closed handle.

    A closed DBAPI connection cannot retain a callback, so clearing is a
    no-op when the handle is already gone.  Only sqlite3.ProgrammingError
    whose message proves the database/connection is closed is suppressed;
    any other cleanup failure is re-raised so it stays visible.
    """
    import sqlite3

    try:
        fence_fn(None, 0)
    except sqlite3.ProgrammingError as pe:
        if "closed" not in str(pe).lower():
            raise
        logger.debug(
            "progress_fence_clear_benign_closed terminal=%s",
            terminal_id,
        )


def mark_terminal_init_ready(
    terminal_id: str,
    *,
    should_commit: Optional[Callable[[], bool]] = None,
    decide_commit: Optional[Callable[[], bool]] = None,
    commit_is_decided: Optional[Callable[[], bool]] = None,
    on_committed: Optional[Callable[[], None]] = None,
) -> bool:
    """Commit pending-to-ready only while the abandonment fence owns it.

    SQLite's progress handler is part of the transaction execution path.  It
    closes the interval between the last Python guard and the real COMMIT: a
    quiesce winner interrupts and rolls back that transaction instead of
    allowing a late ready write to become durable.
    """
    with SessionLocal() as db:
        if should_commit is not None and not should_commit():
            return False
        connection = db.connection()
        driver_connection = connection.connection.driver_connection
        progress_fence = getattr(driver_connection, "set_progress_handler", None)
        if should_commit is not None and progress_fence is not None:
            progress_fence(lambda: int(not should_commit()), 1)
        try:
            changed = (
                db.query(TerminalModel)
                .filter(
                    TerminalModel.id == terminal_id,
                    TerminalModel.init_state == "init_pending",
                )
                .update(
                    {
                        "init_state": "ready",
                        "init_owner_epoch": None,
                        "init_failure_token": None,
                    },
                    synchronize_session=False,
                )
                == 1
            )
            if should_commit is not None and not should_commit():
                if progress_fence is not None:
                    _clear_progress_fence(progress_fence, terminal_id)
                    progress_fence = None
                db.rollback()
                return False

            def resolve_commit_winner(_connection) -> None:
                if decide_commit is not None and not decide_commit():
                    raise _ReadyCommitVeto("ready_commit_timeout_won")

            if changed:
                event.listen(connection, "commit", resolve_commit_winner, once=True)
            if changed and on_committed is not None:
                db.info[_READY_COMMIT_CALLBACK] = on_committed
            db.commit()
        except _ReadyCommitVeto:
            db.info.pop(_READY_COMMIT_CALLBACK, None)
            if progress_fence is not None:
                _clear_progress_fence(progress_fence, terminal_id)
                progress_fence = None
            db.rollback()
            return False
        except Exception as exc:
            decided = commit_is_decided is not None and commit_is_decided()
            abandoned = should_commit is not None and not should_commit()
            if decided:
                logger.critical(
                    "ready_commit_invariant_breach terminal=%s",
                    terminal_id,
                    exc_info=True,
                )
            db.info.pop(_READY_COMMIT_CALLBACK, None)
            if progress_fence is not None:
                if decided:
                    try:
                        _clear_progress_fence(progress_fence, terminal_id)
                    except Exception:
                        logger.error(
                            "ready_commit_fence_cleanup_failed terminal=%s",
                            terminal_id,
                            exc_info=True,
                        )
                    progress_fence = None
                else:
                    _clear_progress_fence(progress_fence, terminal_id)
                    progress_fence = None
            if decided:
                try:
                    db.rollback()
                except Exception:
                    logger.error(
                        "ready_commit_rollback_failed terminal=%s",
                        terminal_id,
                        exc_info=True,
                    )
                raise ReadyCommitInvariantBreach("ready_commit_failed_after_decision") from exc
            db.rollback()
            if (
                isinstance(exc, OperationalError)
                and abandoned
                and "interrupted" in str(exc).lower()
            ):
                return False
            raise
        finally:
            if progress_fence is not None:
                _clear_progress_fence(progress_fence, terminal_id)
                progress_fence = None
            db.info.pop(_READY_COMMIT_CALLBACK, None)
        invalidate_terminal_metadata_cache(terminal_id)
        return changed


def claim_deferred_init_failure(
    terminal_id: str,
    *,
    caller_id: Optional[str],
    failure_token: str,
    notice: str,
    busy_attempts: int = 4,
    busy_delay_s: float = 0.025,
) -> Dict[str, Any]:
    """Atomically claim a pending init and, when possible, enqueue its notice.

    The immediate write lock is the first database observation.  A present
    receiver gets the notice and terminal state in the same transaction; any
    insertion/flush/commit error rolls the whole claim back.
    """
    for attempt in range(busy_attempts):
        with SessionLocal() as db:
            dbapi_identity: int | None = None
            try:
                connection = db.connection()
                fairy = connection.connection
                dbapi_connection = getattr(fairy, "driver_connection", fairy)
                dbapi_identity = id(dbapi_connection)
                logger.info(
                    "deferred_init_claim_connection terminal=%s attempt=%s engine=%s "
                    "dbapi=%s pool=%s",
                    terminal_id,
                    attempt + 1,
                    id(engine),
                    dbapi_identity,
                    engine.pool.status(),
                )
                db.execute(text("BEGIN IMMEDIATE"))
                row = (
                    db.query(TerminalModel)
                    .filter(
                        TerminalModel.id == terminal_id,
                        TerminalModel.init_state == "init_pending",
                    )
                    .first()
                )
                if row is None:
                    existing = db.query(TerminalModel).filter_by(id=terminal_id).first()
                    db.rollback()
                    return {
                        "status": "row_missing" if existing is None else "already_claimed",
                        "init_state": existing.init_state if existing is not None else None,
                        "token": existing.init_failure_token if existing is not None else None,
                    }
                receiver_exists = _receiver_is_terminal_or_mailbox_address(db, caller_id)
                row.init_failure_token = failure_token
                # Mark barrier member GONE before inbox insert: matching must not
                # see AWAITING for a failed deferred-init member.
                _mark_barrier_member_gone_in_db(db, terminal_id)
                if receiver_exists:
                    receiver_cache, logical_receiver_id, enqueue_generation = (
                        resolve_inbox_receiver(db, cast(str, caller_id))
                    )
                    row.init_state = "init_failed_notified"
                    _insert_routed_inbox_row(
                        db,
                        sender_id=terminal_id,
                        receiver_id=receiver_cache,
                        logical_receiver_id=logical_receiver_id,
                        message=notice,
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                    )
                    status = "claimed_notified"
                else:
                    row.init_state = "init_failed_caller_gone"
                    status = "claimed_caller_gone"
                db.flush()
                db.commit()
                invalidate_terminal_metadata_cache(terminal_id)
                return {"status": status, "init_state": row.init_state, "token": failure_token}
            except OperationalError as exc:
                db.rollback()
                logger.exception(
                    "deferred_init_claim_db_error terminal=%s attempt=%s engine=%s "
                    "dbapi=%s pool=%s",
                    terminal_id,
                    attempt + 1,
                    id(engine),
                    dbapi_identity,
                    engine.pool.status(),
                )
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt + 1 >= busy_attempts:
                    raise RuntimeError("deferred_init_claim_busy_exhausted") from exc
            except Exception:
                db.rollback()
                logger.exception(
                    "deferred_init_claim_db_error terminal=%s attempt=%s engine=%s "
                    "dbapi=%s pool=%s",
                    terminal_id,
                    attempt + 1,
                    id(engine),
                    dbapi_identity,
                    engine.pool.status(),
                )
                raise
        time.sleep(busy_delay_s)
    raise RuntimeError("deferred_init_claim_busy_exhausted")


def list_deferred_init_recovery_rows(current_owner_epoch: str) -> List[Dict[str, Any]]:
    """Return only durable init rows owned by the H5 startup sweep."""
    with SessionLocal() as db:
        rows = (
            db.query(TerminalModel)
            .filter(
                (
                    TerminalModel.recovery_state.is_(None)
                    | (TerminalModel.recovery_state != "rollback_kill_uncertain")
                ),
                (
                    (
                        (TerminalModel.init_state == "init_pending")
                        & (
                            (TerminalModel.init_owner_epoch != current_owner_epoch)
                            | TerminalModel.init_owner_epoch.is_(None)
                        )
                    )
                    | TerminalModel.init_state.in_(
                        ("init_failed_notified", "init_failed_caller_gone")
                    )
                ),
            )
            .all()
        )
        return [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in rows
        ]


def list_deferred_init_overdue_pending_rows(now: datetime) -> List[Dict[str, Any]]:
    """Return init_pending rows whose deadline has elapsed (per-row arithmetic).

    Unlike list_deferred_init_recovery_rows this does NOT filter on
    init_owner_epoch — a stuck row is stuck regardless of which server epoch
    stamped it.
    """
    with SessionLocal() as db:
        rows = (
            db.query(TerminalModel)
            .filter(
                (
                    TerminalModel.recovery_state.is_(None)
                    | (TerminalModel.recovery_state != "rollback_kill_uncertain")
                ),
                TerminalModel.init_state == "init_pending",
                TerminalModel.init_started_at.isnot(None),
                TerminalModel.init_deadline_s.isnot(None),
                text(
                    "julianday(init_started_at) + (init_deadline_s / 86400.0)" " < julianday(:now)"
                ),
            )
            .params(now=now.isoformat())
            .all()
        )
        return [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in rows
        ]


def f160_rearm_deferred_init(terminal_id: str, expected_started_at: datetime | None) -> bool:
    """F160-a: grant an overdue init_pending row one more deadline window.

    Restamps ``init_started_at`` to now, keeping ``init_deadline_s``. The write
    is conditional on the row still being ``init_pending`` with the exact
    ``init_started_at`` the caller observed, so a concurrent settle (or a second
    sweep) can never re-arm a row that has already moved on.
    """
    with SessionLocal.begin() as db:
        row = db.get(TerminalModel, terminal_id)
        if row is None or row.init_state != "init_pending":
            return False
        current = _as_utc(row.init_started_at)
        expected = _as_utc(expected_started_at)
        if current is None or expected is None:
            return False
        if abs((current - expected).total_seconds()) > 0.001:
            return False
        row.init_started_at = _utcnow()
    invalidate_terminal_metadata_cache(terminal_id)
    return True


def begin_teardown_intent(workspace_id: str, session_name: str) -> Dict[str, Any]:
    """Create or supersede the single active close authority for a workspace."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        row = db.get(TeardownIntentModel, workspace_id)
        if row is None:
            row = TeardownIntentModel(
                workspace_id=workspace_id,
                session_name=session_name,
                created_at=now,
                state="issuing",
                generation=1,
            )
            db.add(row)
        else:
            row.session_name = session_name
            row.created_at = now
            row.state = "issuing"
            row.generation += 1
        db.flush()
        return {
            "workspace_id": workspace_id,
            "session_name": session_name,
            "state": row.state,
            "generation": row.generation,
            "created_at": row.created_at,
        }


def settle_teardown_intent(workspace_id: str, generation: int, *, issued: bool) -> bool:
    with SessionLocal.begin() as db:
        return (
            db.query(TeardownIntentModel)
            .filter(
                TeardownIntentModel.workspace_id == workspace_id,
                TeardownIntentModel.generation == generation,
                TeardownIntentModel.state == "issuing",
            )
            .update({"state": "issued_ok" if issued else "void"}, synchronize_session=False)
            == 1
        )


def get_teardown_intent(workspace_id: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = db.get(TeardownIntentModel, workspace_id)
        if row is None:
            return None
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def consume_current_teardown_intent(
    workspace_id: str,
    *,
    ttl_s: float,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Consume only the current issued, unexpired generation exactly once."""
    cutoff = (now or _utcnow()) - timedelta(seconds=ttl_s)
    with SessionLocal.begin() as db:
        row = (
            db.query(TeardownIntentModel)
            .filter(
                TeardownIntentModel.workspace_id == workspace_id,
                TeardownIntentModel.state == "issued_ok",
                TeardownIntentModel.created_at >= cutoff,
            )
            .first()
        )
        if row is None:
            return None
        result = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        row.state = "consumed"
        return result


def record_workspace_mapping(workspace_id: str, session_name: str) -> None:
    """Make a workspace current and retire older generations for its session."""
    with SessionLocal.begin() as db:
        db.query(WorkspaceMapModel).filter(
            WorkspaceMapModel.session_name == session_name,
            WorkspaceMapModel.workspace_id != workspace_id,
            WorkspaceMapModel.active.is_(True),
        ).update({"active": False, "updated_at": _utcnow()}, synchronize_session=False)
        row = db.get(WorkspaceMapModel, workspace_id)
        if row is None:
            db.add(
                WorkspaceMapModel(
                    workspace_id=workspace_id,
                    session_name=session_name,
                    active=True,
                    updated_at=_utcnow(),
                )
            )
        else:
            row.session_name = session_name
            row.active = True
            row.updated_at = _utcnow()


def resolve_workspace_mapping(workspace_id: str) -> Optional[str]:
    with SessionLocal() as db:
        row = db.query(WorkspaceMapModel).filter_by(workspace_id=workspace_id, active=True).first()
        return cast(Optional[str], row.session_name) if row else None


def current_workspace_for_session(session_name: str) -> Optional[str]:
    with SessionLocal() as db:
        row = (
            db.query(WorkspaceMapModel)
            .filter_by(session_name=session_name, active=True)
            .order_by(WorkspaceMapModel.updated_at.desc())
            .first()
        )
        return cast(Optional[str], row.workspace_id) if row else None


def retire_workspace_mapping(workspace_id: str) -> bool:
    with SessionLocal.begin() as db:
        return (
            db.query(WorkspaceMapModel)
            .filter_by(workspace_id=workspace_id, active=True)
            .update({"active": False, "updated_at": _utcnow()}, synchronize_session=False)
            == 1
        )


def list_pending_receiver_ids_by_provider(provider: str) -> List[str]:
    """List receiver terminal IDs with pending messages for a specific provider."""
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel.receiver_id)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(
                TerminalModel.provider == provider,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .distinct()
            .all()
        )
        return [row[0] for row in rows]


def list_pending_receiver_ids_older_than(min_age_seconds: int) -> List[str]:
    """List receiver terminal IDs whose messages have been PENDING too long.

    Returns the distinct receivers of any message still PENDING for longer than
    ``min_age_seconds``. Used by the inbox reconciliation sweep to find messages
    the immediate and watchdog delivery paths missed, without competing with
    them for freshly queued ones (issue #131).

    The join on ``terminals`` drops messages whose receiver terminal no longer
    exists, so the sweep does not keep retrying deliveries to deleted agents.

    ``created_at`` is stored as naive-UTC-at-rest (post-hotfix 5959fc82,
    2026-08-10). Pre-hotfix rows (before 2026-08-10) are naive-local-at-rest
    (UTC-4); treating them as UTC yields <=4h over-collection at this site
    (harmless: idempotent downstream). All pre-hotfix rows age out by 2026-08-24
    (14-day retention). The cutoff uses ``_utcnow()`` to match the post-hotfix
    convention.
    """
    cutoff = _utcnow() - timedelta(seconds=min_age_seconds)
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel.receiver_id)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.created_at < cutoff,
            )
            .distinct()
            .all()
        )
        return [row[0] for row in rows]


def list_pending_receiver_ids_with_terminal() -> List[str]:
    """List live terminal rows that still own at least one PENDING message."""
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel.receiver_id)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(InboxModel.status == MessageStatus.PENDING.value)
            .distinct()
            .all()
        )
        return [str(row[0]) for row in rows]


def list_pending_receiver_ids() -> List[str]:
    """List terminal IDs having at least one pending inbox message."""
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel.receiver_id)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(InboxModel.status == MessageStatus.PENDING.value)
            .distinct()
            .all()
        )
        return [row[0] for row in rows]


def list_ready_backlog_observations() -> list[ReadyBacklogObservation]:
    """Snapshot pending backlogs and delivery-attempt progress in one DB session."""
    with SessionLocal() as db:
        pending = (
            db.query(InboxModel)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(InboxModel.status == MessageStatus.PENDING.value)
            .order_by(InboxModel.receiver_id, InboxModel.created_at, InboxModel.id)
            .all()
        )
        oldest_by_receiver: dict[str, InboxModel] = {}
        for message in pending:
            oldest_by_receiver.setdefault(message.receiver_id, message)
        if not oldest_by_receiver:
            return []

        receiver_ids = list(oldest_by_receiver)
        attempts = (
            db.query(InboxDeliveryAttemptModel)
            .filter(InboxDeliveryAttemptModel.receiver_terminal_id.in_(receiver_ids))
            .all()
        )
        progress: dict[str, tuple[int, datetime | None, datetime | None, datetime | None, bool]] = (
            {}
        )

        def latest(left: datetime | None, right: datetime | None) -> datetime | None:
            if left is None:
                return right
            if right is None:
                return left
            return max(left, right)

        for attempt in attempts:
            receiver_id = cast(str, attempt.receiver_terminal_id)
            count, started, settled, last, has_open = progress.get(
                receiver_id, (0, None, None, None, False)
            )
            attempt_started = cast(datetime, attempt.started_at)
            attempt_settled = cast(datetime | None, attempt.settled_at)
            attempt_last = cast(datetime, attempt.last_at)
            progress[receiver_id] = (
                count + 1,
                latest(started, attempt_started),
                latest(settled, attempt_settled),
                latest(last, attempt_last),
                has_open or attempt_settled is None,
            )

        now = _utcnow()
        result = []
        for receiver_id, oldest in oldest_by_receiver.items():
            count, started, settled, last, has_open = progress.get(
                receiver_id, (0, None, None, None, False)
            )
            result.append(
                ReadyBacklogObservation(
                    receiver_id=receiver_id,
                    oldest_message_id=oldest.id,
                    oldest_pending_age_seconds=max(
                        0.0, (now - _as_utc(oldest.created_at)).total_seconds()
                    ),
                    has_open_delivering_attempt=has_open,
                    attempt_fingerprint=(count, started, settled, last),
                )
            )
        return result


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal metadata and its warm intent through the universal seam."""
    return delete_terminal_and_warm_intent(
        terminal_id,
        preserve_warm_intent=False,
    )["terminal_deleted"]


def delete_terminals_by_session(tmux_session: str) -> int:
    """Delete all session terminals and their warm intents through the universal seam."""
    with SessionLocal() as db:
        terminal_ids = [
            terminal_id
            for terminal_id, in db.query(TerminalModel.id)
            .filter(TerminalModel.tmux_session == tmux_session)
            .all()
        ]
    return sum(
        delete_terminal_and_warm_intent(
            terminal_id,
            preserve_warm_intent=False,
        )["terminal_deleted"]
        for terminal_id in terminal_ids
    )


_BARRIER_INTERNAL_PREFIXES = (
    "watchdog:",
    "message-trace:",
    "mailbox-digest",
    "compact-digest",
    "cao-digest:",
    "barrier:",
    "barrier-alert:",
)


def _stamp_enqueue_generation(db: Any, row_fields: dict[str, Any]) -> dict[str, Any]:
    """Stamp every PENDING writer on the mailbox-or-lifecycle generation axis."""
    fields = dict(row_fields)
    logical_receiver_id = fields.get("logical_receiver_id")
    if logical_receiver_id:
        generation = (
            db.query(MailboxModel.generation)
            .filter(MailboxModel.id == logical_receiver_id)
            .scalar()
        )
    else:
        generation = (
            db.query(TerminalModel.lifecycle_generation)
            .filter(TerminalModel.id == fields.get("receiver_id"))
            .scalar()
        )
    if fields.get("status") == MessageStatus.PENDING.value and type(generation) is not int:
        raise ValueError("pending_receiver_generation_unavailable")
    fields["enqueue_generation"] = cast(int | None, generation)
    return fields


def delete_terminals_by_ids(terminal_ids: List[str]) -> int:
    """Delete specific terminal rows by id. Returns the number deleted.

    Unlike ``delete_terminals_by_session`` (which deletes EVERY row for a
    session name), this deletes only the given ids. Session teardown uses it to
    scope its reconciliation sweep to the incarnation it started tearing down,
    so a concurrent same-name recreate — whose rows carry freshly generated ids
    — is never swept (#498).
    """
    if not terminal_ids:
        return 0
    with SessionLocal() as db:
        deleted = (
            db.query(TerminalModel)
            .filter(TerminalModel.id.in_(terminal_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted


def _inbox_message_from_row(row: Any) -> InboxMessage:
    return InboxMessage(
        id=row.id,
        sender_id=row.sender_id,
        receiver_id=row.receiver_id,
        logical_receiver_id=row.logical_receiver_id,
        message=row.message,
        orchestration_type=OrchestrationType(row.orchestration_type),
        status=MessageStatus(row.status),
        park_warm=bool(getattr(row, "park_warm", False)),
        failure_reason=row.failure_reason,
        digested_into=row.digested_into,
        enqueue_generation=row.enqueue_generation,
        owner_receiver_id=row.owner_receiver_id,
        owner_generation=row.owner_generation,
        barrier_id=row.barrier_id,
        barrier_member_key=row.barrier_member_key,
        created_at=row.created_at,
    )


def _validate_dispatch_barrier(dispatch_barrier: dict[str, Any]) -> tuple[str, int, str | None]:
    from cli_agent_orchestrator.constants import (
        CALLBACK_BARRIER_TIMEOUT_MAX_SECONDS,
        CALLBACK_BARRIER_TIMEOUT_SECONDS,
    )

    label = dispatch_barrier.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("invalid_barrier_label")
    timeout = dispatch_barrier.get("timeout_seconds")
    if timeout is None:
        timeout = CALLBACK_BARRIER_TIMEOUT_SECONDS
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
        or timeout > CALLBACK_BARRIER_TIMEOUT_MAX_SECONDS
    ):
        raise ValueError("invalid_barrier_timeout")
    member_key = dispatch_barrier.get("member_key")
    if member_key is not None and (not isinstance(member_key, str) or not member_key.strip()):
        raise ValueError("invalid_barrier_member_key")
    return label, timeout, cast(str | None, member_key)


def _barrier_now() -> datetime:
    return _utcnow().replace(tzinfo=None)


def _barrier_owner_values(db: Any, sender_id: str) -> dict[str, Any]:
    terminal = db.query(TerminalModel).filter(TerminalModel.id == sender_id).one_or_none()
    if terminal is None:
        raise ValueError("barrier_owner_not_found")
    mailbox_id = _mailbox_id_for_terminal(db, sender_id)
    if mailbox_id is not None:
        generation = db.query(MailboxModel.generation).filter_by(id=mailbox_id).scalar()
        return {
            "owner_mailbox_id": mailbox_id,
            "owner_terminal_id": None,
            "owner_generation": int(generation),
        }
    return {
        "owner_mailbox_id": None,
        "owner_terminal_id": sender_id,
        "owner_generation": int(terminal.lifecycle_generation),
    }


def _open_barrier_query(db: Any, owner: dict[str, Any], label: str) -> Any:
    query = db.query(CallbackBarrierModel).filter(
        CallbackBarrierModel.state == "OPEN",
        CallbackBarrierModel.owner_generation == owner["owner_generation"],
        CallbackBarrierModel.label == label,
    )
    if owner["owner_mailbox_id"] is not None:
        return query.filter(CallbackBarrierModel.owner_mailbox_id == owner["owner_mailbox_id"])
    return query.filter(CallbackBarrierModel.owner_terminal_id == owner["owner_terminal_id"])


def _attach_dispatch_barrier_in_db(
    db: Any,
    *,
    sender_id: str,
    terminal_id: str,
    dispatch_barrier: dict[str, Any],
    profile_name: str | None = None,
) -> CallbackBarrierMemberModel:
    """Create/reuse a barrier and attach or re-arm one terminal incarnation."""
    label, timeout_seconds, explicit_key = _validate_dispatch_barrier(dispatch_barrier)
    owner = _barrier_owner_values(db, sender_id)
    barrier = _open_barrier_query(db, owner, label).one_or_none()
    if barrier is None:
        from sqlalchemy.dialects.sqlite import insert

        db.execute(
            insert(CallbackBarrierModel)
            .values(
                **owner,
                label=label,
                state="OPEN",
                timeout_at=_barrier_now() + timedelta(seconds=timeout_seconds),
            )
            .on_conflict_do_nothing()
        )
        db.flush()
        barrier = _open_barrier_query(db, owner, label).one()
    terminal = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
    if terminal is None:
        raise ValueError("barrier_member_terminal_not_found")

    failed_query = db.query(CallbackBarrierMemberModel).filter_by(
        barrier_id=barrier.id, state="FAILED"
    )
    if explicit_key is not None:
        member = failed_query.filter_by(member_key=explicit_key).one_or_none()
        existing = (
            db.query(CallbackBarrierMemberModel)
            .filter_by(barrier_id=barrier.id, member_key=explicit_key)
            .one_or_none()
        )
        if existing is not None and member is None:
            raise ValueError("barrier_member_key_in_use")
    else:
        failed = failed_query.order_by(CallbackBarrierMemberModel.position).all()
        if len(failed) > 1:
            raise ValueError("ambiguous_barrier_member")
        member = failed[0] if failed else None
    if member is not None:
        member.terminal_id = terminal_id
        member.lifecycle_generation = int(terminal.lifecycle_generation)
        member.state = "AWAITING"
        member.failure_class = None
        member.message_id = None
        member.arrived_at = None
        return cast(CallbackBarrierMemberModel, member)

    if explicit_key is None:
        base = (profile_name or terminal.agent_profile or terminal_id).strip() or terminal_id
        keys = {
            key
            for key, in db.query(CallbackBarrierMemberModel.member_key)
            .filter_by(barrier_id=barrier.id)
            .all()
        }
        explicit_key = base
        ordinal = 2
        while explicit_key in keys:
            explicit_key = f"{base}-{ordinal}"
            ordinal += 1
    position = (
        db.query(func.max(CallbackBarrierMemberModel.position))
        .filter_by(barrier_id=barrier.id)
        .scalar()
    )
    member = CallbackBarrierMemberModel(
        barrier_id=barrier.id,
        member_key=explicit_key,
        position=int(position if position is not None else -1) + 1,
        terminal_id=terminal_id,
        lifecycle_generation=int(terminal.lifecycle_generation),
        state="AWAITING",
    )
    db.add(member)
    db.flush()
    return member


def _owner_matches_receiver(
    barrier: Any, receiver_id: str, logical_receiver_id: str | None
) -> bool:
    if barrier.owner_mailbox_id is not None:
        return bool(barrier.owner_mailbox_id == logical_receiver_id)
    return bool(barrier.owner_terminal_id == receiver_id and logical_receiver_id is None)


def _barrier_member_for_callback(
    db: Any,
    *,
    sender_id: str,
    receiver_id: str,
    logical_receiver_id: str | None,
    open_only: bool,
) -> tuple[Any, Any] | None:
    terminal = db.query(TerminalModel).filter_by(id=sender_id).one_or_none()
    if terminal is None:
        return None
    query = (
        db.query(CallbackBarrierModel, CallbackBarrierMemberModel)
        .join(
            CallbackBarrierMemberModel,
            CallbackBarrierMemberModel.barrier_id == CallbackBarrierModel.id,
        )
        .filter(
            CallbackBarrierMemberModel.terminal_id == sender_id,
            CallbackBarrierMemberModel.lifecycle_generation == int(terminal.lifecycle_generation),
        )
    )
    query = query.filter(
        CallbackBarrierModel.state == "OPEN" if open_only else CallbackBarrierModel.state != "OPEN"
    )
    rows = query.order_by(CallbackBarrierModel.created_at.desc()).all()
    return next(
        (
            (barrier, member)
            for barrier, member in rows
            if _owner_matches_receiver(barrier, receiver_id, logical_receiver_id)
        ),
        None,
    )


def _truncate_barrier_message(body: str, message_ids: list[int], limit: int = 16 * 1024) -> str:
    encoded = body.encode("utf-8")
    if len(encoded) <= limit:
        return body
    marker = (
        "\n[truncated; full callback bodies remain in DIGESTED message ids "
        + ",".join(str(value) for value in message_ids)
        + "; use list_messages/message trace]\n"
    ).encode("utf-8")
    prefix = encoded[: max(0, limit - len(marker))]
    while True:
        try:
            return prefix.decode("utf-8") + marker.decode("utf-8")
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]


def _render_callback_barrier(db: Any, barrier: Any, members: list[Any], fired_at: datetime) -> str:
    partial = any(member.state != "ARRIVED" for member in members)
    arrived = sum(member.state == "ARRIVED" for member in members)
    elapsed = max(0, int((fired_at - barrier.created_at).total_seconds()))
    lines = [
        f"[callback barrier {'PARTIAL' if partial else 'COMPLETE'}] "
        f"{barrier.label} — {arrived}/{len(members)} in {elapsed}s"
    ]
    message_ids: list[int] = []
    for member in members:
        rows = (
            db.query(InboxModel)
            .filter(
                InboxModel.barrier_id == barrier.id,
                InboxModel.barrier_member_key == member.member_key,
            )
            .order_by(InboxModel.created_at, InboxModel.id)
            .all()
        )
        message_ids.extend(int(row.id) for row in rows)
        received = max(
            0,
            int(((member.arrived_at or fired_at) - barrier.created_at).total_seconds()),
        )
        lines.append(
            f"--- BEGIN {member.member_key} (terminal {member.terminal_id}, "
            f"received +{received}s) ---"
        )
        if rows:
            lines.extend(row.message for row in rows)
        elif member.state == "FAILED":
            lines.append(f"[FAILED: {member.failure_class or 'unknown'}]")
        elif member.state == "GONE":
            lines.append("[GONE: terminal unavailable]")
        else:
            lines.append("[MISSING: no callback before barrier close]")
        lines.append(f"--- END {member.member_key} ---")
    return _truncate_barrier_message("\n".join(lines), message_ids)


def _resolve_barrier_owner_or_none(db: Any, barrier: Any) -> tuple[str, str | None] | None:
    """Address the barrier owner, or None when the owner no longer exists.

    A barrier outlives the terminal that opened it, so the owner address is a
    claim about a row that may already be gone. Returning None is the only
    honest answer for a barrier whose combined callback has nowhere to land.
    """
    if barrier.owner_mailbox_id is not None:
        mailbox = db.query(MailboxModel).filter_by(id=barrier.owner_mailbox_id).one_or_none()
        if mailbox is None:
            return None
        # FAM-2: verify the cached terminal still exists
        if mailbox.current_terminal_id is not None:
            exists = (
                db.query(TerminalModel.id)
                .filter(TerminalModel.id == mailbox.current_terminal_id)
                .scalar()
            )
            if exists is None:
                return None  # owner's terminal is dead
        else:
            return None  # no current incarnation
        return (str(mailbox.current_terminal_id), str(mailbox.id))
    owner_terminal_id = barrier.owner_terminal_id
    exists = db.query(TerminalModel.id).filter(TerminalModel.id == owner_terminal_id).scalar()
    if exists is None:
        return None
    return (str(owner_terminal_id), None)


def _close_barrier_owner_gone_in_db(db: Any, barrier: Any, now: datetime) -> None:
    """Close an OPEN barrier whose owner is gone, without minting a dead PENDING row.

    Firing it would enqueue a combined callback addressed to a deleted receiver,
    which `_stamp_enqueue_generation` refuses (correctly) by raising. The refusal
    is not the bug; opening a barrier whose owner then vanished is.
    """
    changed = (
        db.query(CallbackBarrierModel)
        .filter(CallbackBarrierModel.id == barrier.id, CallbackBarrierModel.state == "OPEN")
        .update(
            {
                CallbackBarrierModel.state: "CANCELLED",
                CallbackBarrierModel.close_reason: "owner_gone",
                CallbackBarrierModel.fired_at: now,
            },
            synchronize_session=False,
        )
    )
    if changed != 1:
        return
    db.query(InboxModel).filter(
        InboxModel.barrier_id == barrier.id,
        InboxModel.status == MessageStatus.HELD.value,
    ).update(
        {
            InboxModel.status: MessageStatus.CANCELLED.value,
            InboxModel.failure_reason: "barrier_owner_gone",
        },
        synchronize_session=False,
    )
    # FAM-3: terminalize AWAITING members on owner-gone close
    db.query(CallbackBarrierMemberModel).filter(
        CallbackBarrierMemberModel.barrier_id == barrier.id,
        CallbackBarrierMemberModel.state == "AWAITING",
    ).update(
        {
            CallbackBarrierMemberModel.state: "FAILED",
            CallbackBarrierMemberModel.failure_class: "barrier_owner_gone",
        },
        synchronize_session=False,
    )
    logger.warning(
        "callback_barrier_owner_gone barrier=%s label=%s owner_terminal=%s owner_mailbox=%s",
        barrier.id,
        barrier.label,
        barrier.owner_terminal_id,
        barrier.owner_mailbox_id,
    )


def _close_owned_barriers_for_gone_terminal_in_db(db: Any, terminal_id: str) -> list[int]:
    """Close every OPEN barrier owned by a terminal that is being deleted."""
    now = _barrier_now()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    barriers = (
        db.query(CallbackBarrierModel)
        .filter(
            CallbackBarrierModel.state == "OPEN",
            CallbackBarrierModel.owner_terminal_id == terminal_id,
        )
        .all()
    )
    for barrier in barriers:
        _close_barrier_owner_gone_in_db(db, barrier, now)
    return [int(barrier.id) for barrier in barriers]


def _fire_open_barrier_in_db(
    db: Any,
    barrier: Any,
    *,
    state: str,
    close_reason: str,
    now: datetime | None = None,
) -> int | None:
    """CAS one OPEN barrier to a single combined PENDING message."""
    now = now or _barrier_now()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    owner = _resolve_barrier_owner_or_none(db, barrier)
    if owner is None:
        _close_barrier_owner_gone_in_db(db, barrier, now)
        return None
    changed = (
        db.query(CallbackBarrierModel)
        .filter(CallbackBarrierModel.id == barrier.id, CallbackBarrierModel.state == "OPEN")
        .update(
            {
                CallbackBarrierModel.state: state,
                CallbackBarrierModel.close_reason: close_reason,
                CallbackBarrierModel.fired_at: now,
            },
            synchronize_session=False,
        )
    )
    if changed != 1:
        return None
    # FAM-3: terminalize AWAITING members that didn't arrive before close
    db.query(CallbackBarrierMemberModel).filter(
        CallbackBarrierMemberModel.barrier_id == barrier.id,
        CallbackBarrierMemberModel.state == "AWAITING",
    ).update(
        {
            CallbackBarrierMemberModel.state: "FAILED",
            CallbackBarrierMemberModel.failure_class: f"barrier_closed_{close_reason}",
        },
        synchronize_session=False,
    )
    members = (
        db.query(CallbackBarrierMemberModel)
        .filter_by(barrier_id=barrier.id)
        .order_by(CallbackBarrierMemberModel.position)
        .all()
    )
    receiver_id, logical_receiver_id = owner
    combined = _insert_routed_inbox_row(
        db,
        sender_id=f"barrier:{barrier.id}",
        receiver_id=receiver_id,
        logical_receiver_id=logical_receiver_id,
        message=_render_callback_barrier(db, barrier, members, now),
        orchestration_type=OrchestrationType.SEND_MESSAGE,
    )
    db.query(InboxModel).filter(
        InboxModel.barrier_id == barrier.id,
        InboxModel.status == MessageStatus.HELD.value,
    ).update(
        {
            InboxModel.status: MessageStatus.DIGESTED.value,
            InboxModel.digested_into: combined.id,
        },
        synchronize_session=False,
    )
    db.query(CallbackBarrierModel).filter_by(id=barrier.id).update(
        {CallbackBarrierModel.combined_message_id: combined.id},
        synchronize_session=False,
    )
    return int(combined.id)


def _maybe_fire_completed_barrier(db: Any, barrier: Any) -> int | None:
    db.flush()
    states = [
        state
        for state, in db.query(CallbackBarrierMemberModel.state)
        .filter_by(barrier_id=barrier.id)
        .all()
    ]
    if states and all(state in {"ARRIVED", "GONE"} for state in states):
        return _fire_open_barrier_in_db(
            db,
            barrier,
            state="FIRED_COMPLETE",
            close_reason="complete",
        )
    return None


def _is_supervisor_mailbox_id(db: Any, mailbox_id: str) -> bool:
    """Return True if mailbox_id belongs to a supervisor-role mailbox (F123)."""
    row = db.query(MailboxModel).filter_by(id=mailbox_id, role="supervisor").first()
    return row is not None


def _create_obligation_inline(db: Any, inbox_row_id: int, mailbox_id: str) -> None:
    """F192: create delivery obligation in the same transaction as the inbox row.

    This is the single-site obligation creation — called from the choke point
    (_create_inbox_message_unfenced) so every producer gets the obligation
    atomically.  No external imports needed — both models live in this module.
    """
    now = _utcnow()
    db.add(
        DeliveryObligationModel(
            inbox_row_id=inbox_row_id,
            mailbox_id=mailbox_id,
            state="OPEN",
            accepted_at=now,
            next_attempt_at=now,
        )
    )
    db.add(
        InboxMessageTraceEventModel(
            message_id=inbox_row_id,
            kind="fx191.accept",
            phase="accept",
            decision="proceed",
            reason=None,
            payload={},
        )
    )


def _touch_supervisor_pending_flag() -> None:
    """Create the supervisor-pending sentinel for the PostToolUse hook fast-path (F123)."""
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    flag = CAO_HOME_DIR / "supervisor-pending.flag"
    try:
        flag.touch(exist_ok=True)
    except OSError:
        pass  # Best-effort; staleness is safe in both directions.


def _remove_supervisor_pending_flag_if_drained() -> None:
    """Remove sentinel when no supervisor-bound PENDING rows remain (F123).

    Called after settlement paths. Opens its own read-only session for the
    existence check. Safe to call outside a transaction.
    """
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    flag = CAO_HOME_DIR / "supervisor-pending.flag"
    if not flag.exists():
        return
    try:
        with SessionLocal() as db:
            sup_mbox = (
                db.query(MailboxModel)
                .filter(
                    MailboxModel.role == "supervisor",
                    MailboxModel.current_terminal_id.isnot(None),
                )
                .join(
                    TerminalModel,
                    TerminalModel.id == MailboxModel.current_terminal_id,
                )
                .order_by(MailboxModel.updated_at.desc())
                .first()
            )
            if sup_mbox is None:
                # Fallback without terminal-existence join
                sup_mbox = (
                    db.query(MailboxModel)
                    .filter(
                        MailboxModel.role == "supervisor",
                        MailboxModel.current_terminal_id.isnot(None),
                    )
                    .order_by(MailboxModel.updated_at.desc())
                    .first()
                )
            if sup_mbox is None:
                flag.unlink(missing_ok=True)
                return
            has_pending = db.query(
                db.query(InboxModel)
                .filter(
                    InboxModel.status == MessageStatus.PENDING.value,
                    InboxModel.logical_receiver_id == str(sup_mbox.id),
                )
                .exists()
            ).scalar()
            if not has_pending:
                flag.unlink(missing_ok=True)
    except Exception:
        pass  # Best-effort; stale flag just costs one empty drain.


def _insert_routed_inbox_row(
    db: Any,
    *,
    sender_id: str,
    receiver_id: str,
    logical_receiver_id: str | None,
    message: str,
    orchestration_type: OrchestrationType,
    park_warm: bool = False,
    dispatch_barrier: dict[str, Any] | None = None,
    profile_name: str | None = None,
    created_at: datetime | None = None,
) -> Any:
    """The single raw/logical insert choke point for WPQ7 routing."""
    if dispatch_barrier is not None:
        _attach_dispatch_barrier_in_db(
            db,
            sender_id=sender_id,
            terminal_id=receiver_id,
            dispatch_barrier=dispatch_barrier,
            profile_name=profile_name,
        )
    status = MessageStatus.PENDING
    barrier_id = None
    barrier_member_key = None
    routed_message = message
    match = None
    if dispatch_barrier is None and not sender_id.startswith(_BARRIER_INTERNAL_PREFIXES):
        match = _barrier_member_for_callback(
            db,
            sender_id=sender_id,
            receiver_id=receiver_id,
            logical_receiver_id=logical_receiver_id,
            open_only=True,
        )
        if match is not None:
            barrier, member = match
            status = MessageStatus.HELD
            barrier_id = int(barrier.id)
            barrier_member_key = str(member.member_key)
        else:
            closed = _barrier_member_for_callback(
                db,
                sender_id=sender_id,
                receiver_id=receiver_id,
                logical_receiver_id=logical_receiver_id,
                open_only=False,
            )
            if closed is not None:
                routed_message = f"[late callback after barrier {closed[0].label}]\n{message}"
    fields = _stamp_enqueue_generation(
        db,
        {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "logical_receiver_id": logical_receiver_id,
            "message": routed_message,
            "orchestration_type": orchestration_type.value,
            "status": status.value,
            "park_warm": bool(park_warm),
            "barrier_id": barrier_id,
            "barrier_member_key": barrier_member_key,
            **({"created_at": created_at} if created_at is not None else {}),
        },
    )
    row = InboxModel(**fields)
    db.add(row)
    db.flush()
    if match is not None:
        barrier, member = match
        if member.state == "AWAITING":
            member.state = "ARRIVED"
            member.failure_class = None
            member.message_id = int(row.id)
            member.arrived_at = _barrier_now()
        _maybe_fire_completed_barrier(db, barrier)
    # F413: obligation + sentinel + doorbell now handled by the after_insert ORM listener.
    return row


def attach_terminal_dispatch_barrier(
    db: Any,
    *,
    sender_id: str,
    terminal_id: str,
    profile_name: str | None,
    dispatch_barrier: dict[str, Any],
) -> None:
    _attach_dispatch_barrier_in_db(
        db,
        sender_id=sender_id,
        terminal_id=terminal_id,
        profile_name=profile_name,
        dispatch_barrier=dispatch_barrier,
    )


def _select_callback_barrier(
    db: Any,
    *,
    barrier_id: int | None,
    barrier_label: str | None,
    owner_id: str | None,
) -> Any:
    if (barrier_id is None) == (barrier_label is None):
        raise ValueError("barrier_selector_requires_exactly_one")
    if barrier_id is not None:
        row = db.query(CallbackBarrierModel).filter_by(id=barrier_id).one_or_none()
        if row is not None and owner_id is not None:
            owner = _barrier_owner_values(db, owner_id)
            owner_matches = (
                row.owner_generation == owner["owner_generation"]
                and row.owner_mailbox_id == owner["owner_mailbox_id"]
                and row.owner_terminal_id == owner["owner_terminal_id"]
            )
            if not owner_matches:
                row = None
    else:
        if owner_id is None:
            raise ValueError("barrier_owner_required")
        owner = _barrier_owner_values(db, owner_id)
        query = db.query(CallbackBarrierModel).filter(
            CallbackBarrierModel.label == barrier_label,
            CallbackBarrierModel.owner_generation == owner["owner_generation"],
        )
        if owner["owner_mailbox_id"] is not None:
            query = query.filter(CallbackBarrierModel.owner_mailbox_id == owner["owner_mailbox_id"])
        else:
            query = query.filter(
                CallbackBarrierModel.owner_terminal_id == owner["owner_terminal_id"]
            )
        row = query.order_by(CallbackBarrierModel.created_at.desc()).first()
    if row is None:
        raise ValueError("barrier_not_found")
    return row


def callback_barrier_status(
    *,
    barrier_id: int | None = None,
    barrier_label: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    with SessionLocal() as db:
        barrier = _select_callback_barrier(
            db,
            barrier_id=barrier_id,
            barrier_label=barrier_label,
            owner_id=owner_id,
        )
        members = (
            db.query(CallbackBarrierMemberModel)
            .filter_by(barrier_id=barrier.id)
            .order_by(CallbackBarrierMemberModel.position)
            .all()
        )
        return {
            "id": int(barrier.id),
            "label": barrier.label,
            "state": barrier.state,
            "close_reason": barrier.close_reason,
            "owner_mailbox_id": barrier.owner_mailbox_id,
            "owner_terminal_id": barrier.owner_terminal_id,
            "owner_generation": int(barrier.owner_generation),
            "timeout_at": barrier.timeout_at,
            "created_at": barrier.created_at,
            "fired_at": barrier.fired_at,
            "combined_message_id": barrier.combined_message_id,
            "members": [
                {
                    "member_key": member.member_key,
                    "position": int(member.position),
                    "terminal_id": member.terminal_id,
                    "lifecycle_generation": int(member.lifecycle_generation),
                    "state": member.state,
                    "failure_class": member.failure_class,
                    "message_id": member.message_id,
                    "arrived_at": member.arrived_at,
                    "held_message_ids": [
                        int(value)
                        for value, in db.query(InboxModel.id)
                        .filter(
                            InboxModel.barrier_id == barrier.id,
                            InboxModel.barrier_member_key == member.member_key,
                        )
                        .order_by(InboxModel.id)
                        .all()
                    ],
                }
                for member in members
            ],
        }


def cancel_callback_barrier(
    *,
    barrier_id: int | None = None,
    barrier_label: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    with SessionLocal.begin() as db:
        barrier = _select_callback_barrier(
            db,
            barrier_id=barrier_id,
            barrier_label=barrier_label,
            owner_id=owner_id,
        )
        if barrier.state != "OPEN":
            return {
                "id": int(barrier.id),
                "state": barrier.state,
                "released": 0,
                "receiver_ids": [],
            }
        receiver_ids = [
            value
            for value, in db.query(InboxModel.receiver_id)
            .filter(
                InboxModel.barrier_id == barrier.id,
                InboxModel.status == MessageStatus.HELD.value,
            )
            .distinct()
            .all()
        ]
        # F413 D7b: collect qualifying rows before bulk flip
        _cancel_flip_rows = [
            (MessageStatus.PENDING.value, int(r.id), r.logical_receiver_id)
            for r in db.query(InboxModel)
            .filter(
                InboxModel.barrier_id == barrier.id,
                InboxModel.status == MessageStatus.HELD.value,
            )
            .all()
        ]
        released = (
            db.query(InboxModel)
            .filter(
                InboxModel.barrier_id == barrier.id,
                InboxModel.status == MessageStatus.HELD.value,
            )
            .update({InboxModel.status: MessageStatus.PENDING.value}, synchronize_session=False)
        )
        barrier.state = "CANCELLED"
        barrier.close_reason = "cancelled"
        barrier.fired_at = _barrier_now()
        # F413 D7b: create obligations for qualifying flipped rows
        if _cancel_flip_rows:
            _f413_qualify_and_create(db, _cancel_flip_rows)
        # FAM-3: terminalize AWAITING members on cancel
        db.query(CallbackBarrierMemberModel).filter(
            CallbackBarrierMemberModel.barrier_id == barrier.id,
            CallbackBarrierMemberModel.state == "AWAITING",
        ).update(
            {
                CallbackBarrierMemberModel.state: "FAILED",
                CallbackBarrierMemberModel.failure_class: "barrier_closed_cancel",
            },
            synchronize_session=False,
        )

        # F136-D3: enqueue replay for released rows below cursor
        released_rows = (
            db.query(InboxModel.id, InboxModel.logical_receiver_id)
            .filter(
                InboxModel.barrier_id == barrier.id,
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.logical_receiver_id.isnot(None),
            )
            .all()
        )
        for row_id, logical_id in released_rows:
            mb: Any = db.query(MailboxModel).filter_by(id=logical_id).one_or_none()
            if mb and mb.callback_notified_through_id is not None:
                if int(row_id) <= int(mb.callback_notified_through_id):
                    db.execute(
                        text(
                            "INSERT OR IGNORE INTO callback_replay_queue "
                            "(mailbox_id, inbox_row_id, queued_at) VALUES (:mb, :rid, :now)"
                        ),
                        {"mb": logical_id, "rid": row_id, "now": _utcnow()},
                    )

        return {
            "id": int(barrier.id),
            "state": barrier.state,
            "released": int(released),
            "receiver_ids": receiver_ids,
        }


def fire_due_barriers(now: datetime | None = None) -> list[int]:
    now = now or _barrier_now()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    fired: list[int] = []
    with SessionLocal.begin() as db:
        due = (
            db.query(CallbackBarrierModel)
            .filter(CallbackBarrierModel.state == "OPEN")
            .order_by(CallbackBarrierModel.timeout_at, CallbackBarrierModel.id)
            .all()
        )
        for barrier in due:
            # One unfireable barrier must not wedge every other barrier forever.
            # Without the savepoint a single raise aborts the whole sweep, and
            # since the sweep is retried unchanged it would never fire again.
            try:
                with db.begin_nested():
                    # Completion outranks timeout (COMPLETION LAW): an all-ARRIVED
                    # barrier closes FIRED_COMPLETE even if it is also past its
                    # deadline. None covers BOTH "not all members arrived" AND the
                    # owner-gone check closing the barrier internally.
                    message_id = _maybe_fire_completed_barrier(db, barrier)
                    if message_id is None and barrier.timeout_at <= now:
                        message_id = _fire_open_barrier_in_db(
                            db,
                            barrier,
                            state="FIRED_TIMEOUT",
                            close_reason="timeout",
                            now=now,
                        )
            except Exception:
                logger.exception(
                    "callback_barrier_fire_failed barrier=%s label=%s",
                    barrier.id,
                    barrier.label,
                )
                continue
            if message_id is not None:
                fired.append(message_id)
    return fired


def _mark_barrier_member_gone_in_db(db: Any, terminal_id: str) -> list[int]:
    fired: list[int] = []
    rows = (
        db.query(CallbackBarrierModel, CallbackBarrierMemberModel)
        .join(
            CallbackBarrierMemberModel,
            CallbackBarrierMemberModel.barrier_id == CallbackBarrierModel.id,
        )
        .filter(
            CallbackBarrierModel.state == "OPEN",
            CallbackBarrierMemberModel.terminal_id == terminal_id,
            CallbackBarrierMemberModel.state.in_(("AWAITING", "FAILED")),
        )
        .all()
    )
    for barrier, member in rows:
        member.state = "GONE"
        member.failure_class = "terminal_gone"
        message_id = _maybe_fire_completed_barrier(db, barrier)
        if message_id is not None:
            fired.append(message_id)
    return fired


def insert_barrier_escalation_message(
    terminal_id: str,
    caller_id: str,
    message: str,
    idle_reason: str | None,
) -> WatchdogInsertResult | None:
    """Atomically persist member failure state and its single owner notification."""
    with SessionLocal.begin() as db:
        receiver_cache, logical_receiver_id, _ = resolve_inbox_receiver(db, caller_id)
        match = _barrier_member_for_callback(
            db,
            sender_id=terminal_id,
            receiver_id=receiver_cache,
            logical_receiver_id=logical_receiver_id,
            open_only=True,
        )
        if match is None:
            return None
        barrier, member = match
        quota = idle_reason == "quota_or_auth"
        if quota and member.state == "FAILED" and member.failure_class == idle_reason:
            return WatchdogInsertResult("inserted", None)
        member.failure_class = idle_reason
        if quota:
            member.state = "FAILED"
            sender = f"barrier-alert:{barrier.id}"
            body = (
                f"[callback barrier {barrier.label}] member {member.member_key} "
                f"({terminal_id}) requires recovery: quota_or_auth"
            )
        else:
            sender = f"watchdog:{terminal_id}"
            body = message
        row = _insert_routed_inbox_row(
            db,
            sender_id=sender,
            receiver_id=receiver_cache,
            logical_receiver_id=logical_receiver_id,
            message=body,
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        )
        return WatchdogInsertResult("inserted", int(row.id))


def create_inbox_message(
    sender_id: str,
    receiver_id: str,
    message: str,
    orchestration_type: OrchestrationType = OrchestrationType.SEND_MESSAGE,
    dispatch_barrier: dict[str, Any] | None = None,
    park_warm: bool = False,
) -> InboxMessage:
    from cli_agent_orchestrator.services.stalled_callback_watchdog import (
        stalled_callback_watchdog,
    )

    with stalled_callback_watchdog.callback_insert_guard(sender_id):
        return _create_inbox_message_unfenced(
            sender_id,
            receiver_id,
            message,
            orchestration_type,
            dispatch_barrier=dispatch_barrier,
            park_warm=park_warm,
        )


def create_digest_pending_notice(
    receiver_id: str,
    base: str,
    state_key: str,
    body: str,
    *,
    genesis: bool = False,
) -> InboxMessage | None:
    """Insert one parked digest-pending notice, deduplicated by its first line.

    The immediate transaction makes the dedup observation and insert one atomic
    operation.  Digest notices use an internal sender so they never become
    callback-barrier members.
    """
    sender_id = f"cao-digest:{base}"
    qualifier = "genesis" if genesis else ""
    tag = f": {qualifier}" if qualifier else ""
    header = f"[CAO DIGEST-PENDING{tag}] base={base} key={state_key}\n"
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            receiver_cache, logical_receiver_id, _ = resolve_inbox_receiver(db, receiver_id)
            if (
                logical_receiver_id is None
                and not db.query(TerminalModel).filter(TerminalModel.id == receiver_cache).first()
            ):
                raise ValueError(f"Terminal '{receiver_id}' not found")
            receiver_filter = (
                InboxModel.logical_receiver_id == logical_receiver_id
                if logical_receiver_id is not None
                else InboxModel.receiver_id == receiver_cache
            )
            existing = (
                db.query(InboxModel)
                .filter(
                    InboxModel.sender_id == sender_id,
                    receiver_filter,
                    text("substr(message, 1, :n) = :header").bindparams(
                        n=len(header), header=header
                    ),
                )
                .order_by(InboxModel.id)
                .first()
            )
            if existing is not None:
                db.commit()
                return None
            row = _insert_routed_inbox_row(
                db,
                sender_id=sender_id,
                receiver_id=receiver_cache,
                logical_receiver_id=logical_receiver_id,
                message=header + body,
                orchestration_type=OrchestrationType.SEND_MESSAGE,
                park_warm=True,
            )
            db.commit()
            db.refresh(row)
            return _inbox_message_from_row(row)
        except Exception:
            db.rollback()
            raise


def _create_inbox_message_unfenced(
    sender_id: str,
    receiver_id: str,
    message: str,
    orchestration_type: OrchestrationType = OrchestrationType.SEND_MESSAGE,
    *,
    dispatch_barrier: dict[str, Any] | None = None,
    park_warm: bool = False,
) -> InboxMessage:
    """Create inbox message with status=MessageStatus.PENDING.

    Raises:
        ValueError: If the receiver terminal does not exist.
    """
    with SessionLocal() as db:
        mailbox_schema = _mailbox_schema_available(db)
        receiver_cache, logical_receiver_id, enqueue_generation = resolve_inbox_receiver(
            db, receiver_id
        )
        if (
            logical_receiver_id is None
            and not db.query(TerminalModel).filter(TerminalModel.id == receiver_cache).first()
        ):
            raise ValueError(f"Terminal '{receiver_id}' not found")
        inbox_msg = _insert_routed_inbox_row(
            db,
            sender_id=sender_id,
            receiver_id=receiver_cache,
            logical_receiver_id=logical_receiver_id if mailbox_schema else None,
            message=message,
            orchestration_type=orchestration_type,
            dispatch_barrier=dispatch_barrier,
            park_warm=park_warm,
        )
        db.commit()
        db.refresh(inbox_msg)
        result = _inbox_message_from_row(inbox_msg)
        if result.barrier_id is not None and result.barrier_member_key is not None:
            from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                stalled_callback_watchdog,
            )

            stalled_callback_watchdog.record_callback_if_to_caller(
                sender_id,
                logical_receiver_id or receiver_cache,
            )
        return result


def insert_watchdog_auto_resume_message(terminal_id: str, message: str) -> WatchdogInsertResult:
    """Insert one resume row while preserving commit-phase uncertainty."""
    try:
        db = SessionLocal()
    except Exception:
        return WatchdogInsertResult("failed_before_commit")
    commit_started = False
    try:
        if db.query(TerminalModel.id).filter(TerminalModel.id == terminal_id).first() is None:
            return WatchdogInsertResult("failed_before_commit")
        row = InboxModel(
            **_stamp_enqueue_generation(
                db,
                {
                    "sender_id": f"watchdog:{terminal_id}",
                    "receiver_id": terminal_id,
                    "message": message,
                    "orchestration_type": OrchestrationType.SEND_MESSAGE.value,
                    "status": MessageStatus.PENDING.value,
                },
            )
        )
        db.add(row)
        db.flush()
        message_id = int(row.id)
        commit_started = True
        db.commit()
        return WatchdogInsertResult("inserted", message_id)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return WatchdogInsertResult("uncertain" if commit_started else "failed_before_commit")
    finally:
        db.close()


def cancel_pending_watchdog_message(message_id: int, terminal_id: str) -> bool:
    """Cancel exactly one still-pending auto-resume row by guarded CAS."""
    with SessionLocal.begin() as db:
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id == message_id,
                InboxModel.sender_id == f"watchdog:{terminal_id}",
                InboxModel.receiver_id == terminal_id,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .update(
                {
                    InboxModel.status: MessageStatus.CANCELLED.value,
                    InboxModel.failure_reason: "auto_resume_superseded",
                },
                synchronize_session=False,
            )
        )
        return changed == 1


def insert_identity_authority_notice(
    sender_id: str, receiver_id: str, message: str
) -> NoticeInsertOutcome:
    """Insert one generation-stamped authority notice with honest commit phases."""
    try:
        db = SessionLocal()
    except Exception:
        return NoticeInsertOutcome.FAILED_BEFORE_COMMIT
    committed = False
    try:
        try:
            receiver_cache, logical_receiver_id, enqueue_generation = resolve_inbox_receiver(
                db, receiver_id
            )
            if (
                logical_receiver_id is None
                and not db.query(TerminalModel).filter(TerminalModel.id == receiver_cache).first()
            ):
                db.rollback()
                return NoticeInsertOutcome.FAILED_BEFORE_COMMIT
            row = _insert_routed_inbox_row(
                db,
                sender_id=sender_id,
                receiver_id=receiver_cache,
                logical_receiver_id=logical_receiver_id,
                message=message,
                orchestration_type=OrchestrationType.SEND_MESSAGE,
            )
        except Exception:
            db.rollback()
            return NoticeInsertOutcome.FAILED_BEFORE_COMMIT
        try:
            db.commit()
            committed = True
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            return NoticeInsertOutcome.UNCERTAIN_COMMIT
        try:
            db.refresh(row)
            int(row.id)
        except Exception:
            return NoticeInsertOutcome.FAILED_AFTER_COMMIT
        return NoticeInsertOutcome.INSERTED
    finally:
        try:
            db.close()
        except Exception:
            if committed:
                logger.warning("identity_authority_notice_close_failed", exc_info=True)


def _pending_receiver_predicate(receiver_id: str, mailbox_schema: bool):
    """Select raw rows by cache and logical rows by the mailbox's live authority."""
    if not mailbox_schema:
        return InboxModel.receiver_id == receiver_id
    current_logical_receiver = exists().where(
        and_(
            MailboxModel.id == InboxModel.logical_receiver_id,
            MailboxModel.current_terminal_id == receiver_id,
        )
    )
    return or_(
        and_(
            InboxModel.logical_receiver_id.is_(None),
            InboxModel.receiver_id == receiver_id,
        ),
        current_logical_receiver,
    )


def get_pending_messages(
    receiver_id: str,
    limit: int = 100,
    excluded_message_ids: set[int] | None = None,
) -> List[InboxMessage]:
    """Get pending messages ordered by id ASC (F276: strict FIFO by creation id).

    WPDT W6 (F276): Changed primary sort from created_at to id. This ensures
    assign-brief messages (created atomically with the terminal) are always
    delivered before later send_message calls, even when timestamps collide.
    """
    excluded = set(excluded_message_ids or ())
    with SessionLocal() as db:
        mailbox_schema = _mailbox_schema_available(db)
        query = db.query(InboxModel).filter(
            _pending_receiver_predicate(receiver_id, mailbox_schema),
            InboxModel.status == MessageStatus.PENDING.value,
        )
        if excluded:
            query = query.filter(~InboxModel.id.in_(excluded))
        # F276: enforce per-terminal FIFO by creation id at delivery time
        rows = query.order_by(InboxModel.id.asc()).limit(limit).all()
        return [
            InboxMessage(
                id=row.id,
                sender_id=row.sender_id,
                receiver_id=row.receiver_id,
                logical_receiver_id=row.logical_receiver_id if mailbox_schema else None,
                digested_into=row.digested_into if mailbox_schema else None,
                enqueue_generation=row.enqueue_generation if mailbox_schema else None,
                barrier_id=row.barrier_id,
                barrier_member_key=row.barrier_member_key,
                message=row.message,
                orchestration_type=OrchestrationType(row.orchestration_type),
                status=MessageStatus(row.status),
                park_warm=bool(getattr(row, "park_warm", False)),
                created_at=row.created_at,
            )
            for row in rows
        ]


def get_pending_messages_by_ids(receiver_id: str, message_ids: list[int]) -> List[InboxMessage]:
    ids = sorted(set(message_ids))
    if not ids:
        return []
    with SessionLocal() as db:
        mailbox_schema = _mailbox_schema_available(db)
        rows = (
            db.query(InboxModel)
            .filter(
                _pending_receiver_predicate(receiver_id, mailbox_schema),
                InboxModel.id.in_(ids),
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .order_by(InboxModel.created_at, InboxModel.id)
            .all()
        )
        return [
            InboxMessage(
                id=row.id,
                sender_id=row.sender_id,
                receiver_id=row.receiver_id,
                logical_receiver_id=row.logical_receiver_id if mailbox_schema else None,
                digested_into=row.digested_into if mailbox_schema else None,
                enqueue_generation=row.enqueue_generation if mailbox_schema else None,
                message=row.message,
                orchestration_type=OrchestrationType(row.orchestration_type),
                status=MessageStatus(row.status),
                park_warm=bool(getattr(row, "park_warm", False)),
                created_at=row.created_at,
            )
            for row in rows
        ]


def get_park_warm_for_message_ids(message_ids: list[int]) -> bool:
    """Re-read per-dispatch intent for a recovered delivery batch."""
    ids = sorted(set(message_ids))
    if not ids:
        return False
    with SessionLocal() as db:
        value = (
            db.query(InboxModel.park_warm)
            .filter(InboxModel.id.in_(ids))
            .order_by(InboxModel.id)
            .first()
        )
        return bool(value[0]) if value is not None else False


def get_owned_legacy_parked_messages(receiver_id: str, limit: int = 100) -> List[InboxMessage]:
    """Return null-generation logical rows parked for this still-current incarnation."""
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel)
            .filter(
                InboxModel.status == MessageStatus.PARKED.value,
                InboxModel.owner_receiver_id == receiver_id,
                InboxModel.logical_receiver_id.is_not(None),
                InboxModel.enqueue_generation.is_(None),
                exists().where(
                    and_(
                        MailboxModel.id == InboxModel.logical_receiver_id,
                        MailboxModel.current_terminal_id == receiver_id,
                    )
                ),
            )
            .order_by(InboxModel.created_at.asc(), InboxModel.id.asc())
            .limit(limit)
            .all()
        )
        return [_inbox_message_from_row(row) for row in rows]


def get_inbox_messages(
    receiver_id: str,
    limit: int = 10,
    status: Optional[MessageStatus] = None,
    *,
    generation: int | None = None,
    original_receiver_id: str | None = None,
    audit_browse: bool = False,
) -> List[InboxMessage]:
    """Get inbox messages with optional status filter ordered by created_at ASC (oldest first).

    Args:
        receiver_id: Terminal ID to get messages for
        limit: Maximum number of messages to return (default: 10)
        status: Optional filter by message status (None = all statuses)

    Returns:
        List of inbox messages ordered by creation time (oldest first)
    """
    with SessionLocal() as db:
        mailbox_schema = _mailbox_schema_available(db)
        query = db.query(InboxModel).filter(InboxModel.receiver_id == receiver_id)

        if status is not None:
            query = query.filter(InboxModel.status == status.value)
        elif not audit_browse:
            query = query.filter(InboxModel.status != MessageStatus.PARKED.value)
        if generation is not None:
            query = query.filter(InboxModel.owner_generation == generation)
        if original_receiver_id is not None:
            query = query.filter(InboxModel.owner_receiver_id == original_receiver_id)

        messages = query.order_by(InboxModel.created_at.asc()).limit(limit).all()

        result: list[InboxMessage] = []
        for msg in messages:
            dead_to_successor: bool | None = None
            if msg.status == MessageStatus.PARKED.value:
                if msg.logical_receiver_id:
                    authority = (
                        db.query(MailboxModel.generation, MailboxModel.current_terminal_id)
                        .filter(MailboxModel.id == msg.logical_receiver_id)
                        .one_or_none()
                    )
                    dead_to_successor = bool(
                        authority is not None
                        and (
                            int(authority.generation) != msg.owner_generation
                            or authority.current_terminal_id != msg.owner_receiver_id
                        )
                    )
                else:
                    current_generation = (
                        db.query(TerminalModel.lifecycle_generation)
                        .filter(TerminalModel.id == msg.owner_receiver_id)
                        .scalar()
                    )
                    dead_to_successor = bool(
                        type(current_generation) is int
                        and int(current_generation) != msg.owner_generation
                    )
            result.append(
                InboxMessage(
                    id=msg.id,
                    sender_id=msg.sender_id,
                    receiver_id=msg.receiver_id,
                    logical_receiver_id=msg.logical_receiver_id if mailbox_schema else None,
                    digested_into=msg.digested_into if mailbox_schema else None,
                    enqueue_generation=msg.enqueue_generation if mailbox_schema else None,
                    owner_receiver_id=msg.owner_receiver_id if mailbox_schema else None,
                    owner_generation=msg.owner_generation if mailbox_schema else None,
                    dead_to_successor=dead_to_successor,
                    barrier_id=msg.barrier_id,
                    barrier_member_key=msg.barrier_member_key,
                    message=msg.message,
                    orchestration_type=OrchestrationType(msg.orchestration_type),
                    status=MessageStatus(msg.status),
                    park_warm=bool(getattr(msg, "park_warm", False)),
                    created_at=msg.created_at,
                )
            )
        return result


def record_project_alias(project_id: str, alias: str, kind: str) -> None:
    """Idempotently record a project_id ↔ alias mapping (Phase 2.5 U6).

    Used opportunistically by ``resolve_project_id`` to track historical
    cwd-hash and git-remote-url aliases for a canonical project_id. Best-effort
    only — DB errors are swallowed so identity resolution is never blocked.
    """
    if not project_id or not alias or project_id == alias:
        return
    try:
        with SessionLocal() as db:
            # Upsert by alias (the primary key). If the same alias was already
            # mapped — e.g. recorded against an override id, then re-resolved
            # via git remote — repoint it to the current canonical project_id
            # so reverse lookups stay deterministic instead of duplicating.
            existing = db.query(ProjectAliasModel).filter(ProjectAliasModel.alias == alias).first()
            if existing is None:
                db.add(ProjectAliasModel(project_id=project_id, alias=alias, kind=kind))
                db.commit()
            elif existing.project_id != project_id or existing.kind != kind:
                existing.project_id = project_id
                existing.kind = kind
                db.commit()
    except Exception as e:
        logger.debug(f"record_project_alias failed (non-fatal): {e}")


def get_project_id_by_alias(alias: str) -> Optional[str]:
    """Return the canonical ``project_id`` for an alias, or None if unknown."""
    if not alias:
        return None
    try:
        with SessionLocal() as db:
            row = db.query(ProjectAliasModel).filter(ProjectAliasModel.alias == alias).first()
            return cast(Optional[str], row.project_id) if row else None
    except Exception as e:
        logger.debug(f"get_project_id_by_alias failed (non-fatal): {e}")
        return None


def list_aliases_for_project(project_id: str) -> List[Dict[str, Any]]:
    """List all aliases recorded for a canonical ``project_id``."""
    if not project_id:
        return []
    try:
        with SessionLocal() as db:
            rows = (
                db.query(ProjectAliasModel).filter(ProjectAliasModel.project_id == project_id).all()
            )
            return [{"project_id": r.project_id, "alias": r.alias, "kind": r.kind} for r in rows]
    except Exception as e:
        logger.debug(f"list_aliases_for_project failed (non-fatal): {e}")
        return []


def update_message_status(message_id: int, status: MessageStatus) -> bool:
    """Update message status to MessageStatus.DELIVERED or MessageStatus.FAILED."""
    with SessionLocal() as db:
        message = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if message:
            message.status = status.value
            db.commit()
            return True
        return False


WPM1_EVIDENCE_KEYS = frozenset(
    {
        "boundary_authorized",
        "boundary_exhausted_at",
        "idle_observed_at",
        "last_activity_at",
        "last_observed_status",
        "last_observed_ref",
        "stalled_notified_at",
        "terminal_settled_at",
        "injection_completed_seq",
        "crash_recovery",
        "boundary_snapshot",
        "queue_corroboration",
        "busy_initial_submit",
        "redelivery_tag",
    }
)

ORPHAN_RECONCILE_BATCH_LIMIT = 100

WPM2_CURSOR_VERSION = 1


@dataclass(frozen=True)
class AdmissionProof:
    kind: str
    candidate_ids: tuple[int, ...]
    fingerprint: str
    prior_attempt_uuid: str | None = None
    transcript_checks: tuple[
        tuple[str, object, tuple[tuple[str, Any], ...], tuple[tuple[str, Any], ...]], ...
    ] = ()


@dataclass(frozen=True)
class AttemptOpenResult:
    kind: str
    attempt_uuid: str | None = None

    @classmethod
    def opened(cls, attempt_uuid: str) -> "AttemptOpenResult":
        return cls("opened", attempt_uuid)


@dataclass(frozen=True)
class OrphanReconcileResult:
    settled_count: int = 0
    notification_count: int = 0
    logged_only_count: int = 0
    busy_aborted: bool = False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _attempt_history_in_db(db, message_ids: list[int]) -> list[dict[str, Any]]:
    rows = (
        db.query(InboxDeliveryAttemptModel)
        .join(
            InboxDeliveryAttemptMemberModel,
            InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid,
        )
        .filter(InboxDeliveryAttemptMemberModel.message_id.in_(message_ids))
        .order_by(InboxDeliveryAttemptModel.started_at, InboxDeliveryAttemptModel.attempt_uuid)
        .distinct()
        .all()
    )
    result = []
    for row in rows:
        members = sorted(
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=row.attempt_uuid)
            .all()
        )
        result.append(
            {
                "attempt_uuid": row.attempt_uuid,
                "members": members,
                "outcome": row.outcome,
                "reason": row.reason,
                "payload_hash": row.payload_hash,
                "prior_attempt_uuid": row.prior_attempt_uuid,
                "receiver_terminal_id": row.receiver_terminal_id,
                "started_at": row.started_at,
                "evidence": row.evidence,
                "evidence_hash": hashlib.sha256((row.evidence or "{}").encode()).hexdigest(),
            }
        )
    return result


def _history_fingerprint(history: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(history).encode()).hexdigest()


def list_overlapping_attempts(message_ids: list[int]) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        return _attempt_history_in_db(db, sorted(set(message_ids)))


def attempt_proven_pre_paste(attempt: dict[str, Any]) -> bool:
    return (attempt.get("outcome"), attempt.get("reason")) in {
        ("deferred", "delivery_deferred"),
        ("deferred", "input_blocked"),
        ("interrupted", "terminal_not_found"),
    }


def make_admission_proof(
    kind: str,
    message_ids: list[int],
    prior_attempt_uuid: str | None = None,
) -> AdmissionProof:
    ids = sorted(set(message_ids))
    history = list_overlapping_attempts(ids)
    checks = []
    for row in list_message_attempts(ids):
        if kind == "corrective" and row["attempt_uuid"] != prior_attempt_uuid:
            continue
        try:
            evidence = json.loads(row.get("evidence") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        cursor = _valid_cursor(evidence.get("last_observed_ref"))
        if cursor is not None and row.get("outcome") not in {None, "confirmed", "failed"}:
            # Same selector confirm/staleness uses — proof names the binding that was read.
            from cli_agent_orchestrator.services.message_trace_service import (
                binding_for_transcript_confirm,
            )

            binding = binding_for_transcript_confirm(row["receiver_terminal_id"])
            authority = {
                "binding_id": binding.get("id") if binding else None,
                "session_id": binding.get("session_id") if binding else None,
                "path": cursor["path"],
                "inode": cursor["inode"],
                "resolution_kind": cursor["resolution_kind"],
            }
            checks.append(
                (
                    row["payload_hash"],
                    row.get("started_at"),
                    tuple(sorted(cursor.items())),
                    tuple(sorted(authority.items())),
                )
            )
    return AdmissionProof(
        kind, tuple(ids), _history_fingerprint(history), prior_attempt_uuid, tuple(checks)
    )


def _delivering_authority_in_db(db, terminal_id: str) -> list[dict[str, Any]]:
    """Map each DELIVERING inbox row to its newest durable attempt owner."""
    mailbox_schema = _mailbox_schema_available(db)
    messages = (
        db.query(InboxModel)
        .filter(
            _pending_receiver_predicate(terminal_id, mailbox_schema),
            InboxModel.status == MessageStatus.DELIVERING.value,
        )
        .all()
    )
    owners: dict[str, set[int]] = {}
    for message in messages:
        owner = (
            db.query(InboxDeliveryAttemptModel)
            .join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid
                == InboxDeliveryAttemptModel.attempt_uuid,
            )
            .filter(InboxDeliveryAttemptMemberModel.message_id == message.id)
            .order_by(
                InboxDeliveryAttemptModel.started_at.desc(),
                InboxDeliveryAttemptModel.attempt_uuid.desc(),
            )
            .first()
        )
        if owner is not None:
            owners.setdefault(owner.attempt_uuid, set()).add(message.id)
    return [
        {"attempt_uuid": attempt_uuid, "message_ids": sorted(message_ids)}
        for attempt_uuid, message_ids in sorted(owners.items())
    ]


def list_delivering_attempts_for_terminal(terminal_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        return _delivering_authority_in_db(db, terminal_id)


def _corrective_evidence_valid(prior: dict[str, Any], candidate_ids: list[int]) -> bool:
    if prior["members"] != candidate_ids:
        return False
    evidence = _evidence_object(prior.get("evidence"))
    if _valid_cursor(evidence.get("last_observed_ref")) is None:
        return False
    anchor = evidence.get("injection_completed_seq")
    exhausted_at = evidence.get("boundary_exhausted_at")
    snapshot = evidence.get("boundary_snapshot")
    if (
        not isinstance(anchor, dict)
        or not isinstance(exhausted_at, str)
        or not exhausted_at
        or not isinstance(snapshot, dict)
    ):
        return False
    epoch, anchor_seq = anchor.get("observation_epoch"), anchor.get("seq")
    required = {
        "observation_epoch",
        "status",
        "status_gen",
        "input_gen",
        "seq",
        "last_non_ready_seq",
        "last_ready_seq",
    }
    if (
        set(snapshot) != required
        or not isinstance(epoch, str)
        or not epoch
        or type(anchor_seq) is not int
        or snapshot.get("observation_epoch") != epoch
        or snapshot.get("status") not in {TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value}
        or type(snapshot.get("input_gen")) is not int
        or (snapshot.get("status_gen") is not None and type(snapshot.get("status_gen")) is not int)
        or type(snapshot.get("seq")) is not int
        or type(snapshot.get("last_non_ready_seq")) is not int
        or type(snapshot.get("last_ready_seq")) is not int
    ):
        return False
    non_ready = snapshot["last_non_ready_seq"]
    ready = snapshot["last_ready_seq"]
    return anchor_seq < non_ready < ready <= snapshot["seq"]


def _admission_valid(
    kind: str,
    history: list[dict[str, Any]],
    prior_uuid: str | None,
    candidate_ids: list[int],
) -> bool:
    if kind == "s4_initial":
        return all(
            row["outcome"] == "deferred" and row["reason"] in {"delivery_deferred", "input_blocked"}
            for row in history
        )
    if kind == "corrective":
        prior = next((row for row in history if row["attempt_uuid"] == prior_uuid), None)
        return bool(
            prior
            and prior["outcome"] == "ambiguous"
            and prior["reason"] == "confirmation_timeout"
            and _corrective_evidence_valid(prior, candidate_ids)
            and not any(
                row["prior_attempt_uuid"] == prior_uuid and not attempt_proven_pre_paste(row)
                for row in history
            )
        )
    if kind == "tagged_replay":
        exact = [row for row in history if row["members"] == candidate_ids]
        prior = next((row for row in exact if row["attempt_uuid"] == prior_uuid), None)
        ambiguous_count = sum(row["outcome"] == "ambiguous" for row in exact)
        return bool(
            prior
            and prior["outcome"] == "ambiguous"
            and prior["reason"] == "confirmation_timeout"
            and ambiguous_count < 3
            and not any(
                row["prior_attempt_uuid"] == prior_uuid and not attempt_proven_pre_paste(row)
                for row in exact
            )
        )
    return True


def begin_delivery_attempt_if_no_other_delivering(
    messages,
    receiver_terminal_id: str,
    provider: str,
    payload_hash: str,
    payload_length: int,
    pre_input_gen=None,
    pre_status_gen=None,
    evidence: str = "{}",
    prior_attempt_uuid: str | None = None,
    challenge_sha256: str | None = None,
    admission_proof: AdmissionProof | None = None,
) -> AttemptOpenResult:
    ids = sorted({int(message.id) for message in messages})
    if challenge_sha256 is not None and len(ids) != 1:
        raise ValueError("delivery challenges require a singleton attempt")
    proof = admission_proof or make_admission_proof(
        "corrective" if prior_attempt_uuid else "ordinary", ids, prior_attempt_uuid
    )
    if tuple(ids) != proof.candidate_ids:
        return AttemptOpenResult("stale_candidate")
    attempt_uuid = str(uuid.uuid4())

    def operation(db) -> str:
        open_rows = _delivering_authority_in_db(db, receiver_terminal_id)
        if open_rows:
            db.rollback()
            return "delivering_conflict"
        history = _attempt_history_in_db(db, ids)
        if _history_fingerprint(history) != proof.fingerprint or not _admission_valid(
            proof.kind, history, proof.prior_attempt_uuid, ids
        ):
            db.rollback()
            return "stale_admission"
        if proof.transcript_checks:
            from cli_agent_orchestrator.services.message_trace_service import (
                bounded_transcript_suffix_lookup,
            )

            grouped: dict[tuple[tuple[str, Any], ...], list[tuple[str, object]]] = {}
            authority_by_cursor = {}
            for payload, started_at, cursor_items, authority_items in proof.transcript_checks:
                grouped.setdefault(cursor_items, []).append((payload, started_at))
                authority_by_cursor[cursor_items] = dict(authority_items)
            for cursor_items, payloads in grouped.items():
                authority = authority_by_cursor[cursor_items]
                cursor = dict(cursor_items)
                binding = (
                    db.query(TranscriptBindingModel)
                    .filter_by(terminal_id=receiver_terminal_id)
                    .order_by(
                        TranscriptBindingModel.received_at.desc(), TranscriptBindingModel.id.desc()
                    )
                    .first()
                )
                if authority["binding_id"] is None and binding is None:
                    current_authority = dict(authority)
                else:
                    live_path = Path(binding.transcript_path) if binding else Path(cursor["path"])
                    try:
                        live_inode = live_path.stat().st_ino
                    except OSError:
                        db.rollback()
                        return "stale_admission"
                    current_authority = {
                        "binding_id": binding.id if binding else None,
                        "session_id": binding.session_id if binding else None,
                        "path": str(live_path),
                        "inode": live_inode,
                        "resolution_kind": "binding" if binding else cursor["resolution_kind"],
                    }
                if current_authority != authority:
                    db.rollback()
                    return "stale_admission"
                outcome, _ = bounded_transcript_suffix_lookup(cursor, payloads)
                if outcome != "absent":
                    db.rollback()
                    return "stale_admission"
        mailbox_schema = _mailbox_schema_available(db)
        candidates = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(ids),
                _pending_receiver_predicate(receiver_terminal_id, mailbox_schema),
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .all()
        )
        if sorted(row.id for row in candidates) != ids:
            db.rollback()
            return "stale_candidate"
        attempt_evidence = _evidence_object(evidence)
        logical_ids = (
            {row.logical_receiver_id for row in candidates if row.logical_receiver_id}
            if _mailbox_schema_available(db)
            else set()
        )
        if logical_ids:
            if len(logical_ids) != 1:
                db.rollback()
                return "stale_candidate"
            mailbox = db.query(MailboxModel).filter_by(id=next(iter(logical_ids))).one_or_none()
            if mailbox is None or mailbox.current_terminal_id != receiver_terminal_id:
                db.rollback()
                return "stale_candidate"
            attempt_evidence["mailbox_authority"] = {
                "mailbox_id": mailbox.id,
                "generation": mailbox.generation,
                "current_terminal_id": mailbox.current_terminal_id,
            }
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(ids),
                _pending_receiver_predicate(receiver_terminal_id, mailbox_schema),
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .update({InboxModel.status: MessageStatus.DELIVERING.value}, synchronize_session=False)
        )
        if changed != len(ids):
            db.rollback()
            return "stale_candidate"
        first = sorted(messages, key=lambda item: item.id)[0]
        row = InboxDeliveryAttemptModel(
            attempt_uuid=attempt_uuid,
            receiver_terminal_id=receiver_terminal_id,
            provider=provider,
            payload_hash=payload_hash,
            payload_length=payload_length,
            pre_input_gen=pre_input_gen,
            pre_status_gen=pre_status_gen,
            prior_attempt_uuid=prior_attempt_uuid,
            sender_id=first.sender_id,
            orchestration_type=first.orchestration_type.value,
            evidence=_canonical_json(attempt_evidence)[:2048],
        )
        db.add(row)
        for position, message_id in enumerate(ids):
            db.add(
                InboxDeliveryAttemptMemberModel(
                    attempt_uuid=attempt_uuid, message_id=message_id, position=position
                )
            )
        if challenge_sha256 is not None:
            db.add(
                InboxMessageTraceEventModel(
                    message_id=ids[0],
                    kind="attempt_challenge",
                    payload={
                        "attempt_uuid": attempt_uuid,
                        "challenge_sha256": challenge_sha256,
                    },
                )
            )
        db.flush()
        terminal_open = _delivering_authority_in_db(db, receiver_terminal_id)
        if {row["attempt_uuid"] for row in terminal_open} != {attempt_uuid}:
            db.rollback()
            return "delivering_conflict"
        self_members = sorted(
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        )
        if self_members != ids:
            db.rollback()
            return "stale_candidate"
        return "opened"

    result = _run_wpm1_immediate(operation)
    if result == "opened":
        return AttemptOpenResult.opened(attempt_uuid)
    if result == "busy_aborted":
        return AttemptOpenResult("busy_aborted")
    return AttemptOpenResult(result)


def _is_wpm1_evidence(value: str | None) -> bool:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and bool(WPM1_EVIDENCE_KEYS.intersection(parsed))


def _has_valid_redelivery_tag(value: str | None, prior_attempt_uuid: str | None) -> bool:
    if prior_attempt_uuid is None:
        return False
    parsed = _evidence_object(value)
    tag = parsed.get("redelivery_tag")
    if not isinstance(tag, dict):
        return False
    if tag.get("version") != 1 or tag.get("prior_attempt_uuid") != prior_attempt_uuid:
        return False
    try:
        return str(uuid.UUID(prior_attempt_uuid)) == prior_attempt_uuid
    except (ValueError, AttributeError):
        return False


def _valid_cursor(value: Any, *, versioned: bool = True) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if versioned and (
        type(value.get("cursor_version")) is not int
        or value.get("cursor_version") != WPM2_CURSOR_VERSION
    ):
        return None
    required = ("path", "inode", "size", "resolution_kind")
    if (
        not all(key in value for key in required)
        or not isinstance(value["path"], str)
        or not value["path"]
        or type(value["size"]) is not int
        or value["size"] < 0
        or not isinstance(value["resolution_kind"], str)
        or (value["inode"] is not None and type(value["inode"]) is not int)
    ):
        return None
    return {key: value[key] for key in required} | ({"cursor_version": 1} if versioned else {})


def _initialize_wpm2_cursor(evidence: dict[str, Any]) -> dict[str, Any]:
    nested = _valid_cursor(evidence.get("last_observed_ref"))
    if nested is not None:
        evidence["last_observed_ref"] = nested
        return evidence
    if "last_observed_ref" in evidence:
        return evidence
    legacy = _valid_cursor(evidence, versioned=False)
    if legacy is not None:
        evidence["last_observed_ref"] = {**legacy, "cursor_version": 1}
    return evidence


def begin_delivery_attempt(
    messages,
    receiver_terminal_id: str,
    provider: str,
    payload_hash: str,
    payload_length: int,
    pre_input_gen=None,
    pre_status_gen=None,
    evidence: str = "{}",
    prior_attempt_uuid: str | None = None,
    challenge_sha256: str | None = None,
) -> str:
    if challenge_sha256 is not None and len(messages) != 1:
        raise ValueError("delivery challenges require a singleton attempt")
    attempt_uuid = str(uuid.uuid4())
    with SessionLocal.begin() as db:
        attempt_evidence = _evidence_object(evidence)
        logical_ids = {getattr(message, "logical_receiver_id", None) for message in messages}
        logical_ids.discard(None)
        if logical_ids:
            if len(logical_ids) != 1:
                raise ValueError("logical delivery batch spans mailboxes")
            mailbox = db.query(MailboxModel).filter_by(id=next(iter(logical_ids))).one_or_none()
            if mailbox is None or mailbox.current_terminal_id != receiver_terminal_id:
                raise ValueError("logical delivery incarnation changed")
            attempt_evidence["mailbox_authority"] = {
                "mailbox_id": mailbox.id,
                "generation": mailbox.generation,
                "current_terminal_id": mailbox.current_terminal_id,
            }
        if prior_attempt_uuid is not None:
            prior = (
                db.query(InboxDeliveryAttemptModel)
                .filter_by(attempt_uuid=prior_attempt_uuid)
                .one_or_none()
            )
            prior_members = {
                member.message_id
                for member in db.query(InboxDeliveryAttemptMemberModel)
                .filter_by(attempt_uuid=prior_attempt_uuid)
                .all()
            }
            if prior is None or prior_members != {message.id for message in messages}:
                raise ValueError("WPM1 successor prior attempt does not match exact batch")
        else:
            prior = (
                db.query(InboxDeliveryAttemptModel)
                .join(
                    InboxDeliveryAttemptMemberModel,
                    InboxDeliveryAttemptMemberModel.attempt_uuid
                    == InboxDeliveryAttemptModel.attempt_uuid,
                )
                .filter(InboxDeliveryAttemptMemberModel.message_id == messages[0].id)
                .order_by(InboxDeliveryAttemptModel.started_at.desc())
                .first()
            )
        row = InboxDeliveryAttemptModel(
            attempt_uuid=attempt_uuid,
            receiver_terminal_id=receiver_terminal_id,
            provider=provider,
            payload_hash=payload_hash,
            payload_length=payload_length,
            pre_input_gen=pre_input_gen,
            pre_status_gen=pre_status_gen,
            prior_attempt_uuid=prior.attempt_uuid if prior else None,
            sender_id=messages[0].sender_id,
            orchestration_type=messages[0].orchestration_type.value,
            evidence=_canonical_json(attempt_evidence)[:2048],
        )
        db.add(row)
        for position, message in enumerate(messages):
            current = db.query(InboxModel).filter_by(id=message.id).one()
            current.status = MessageStatus.DELIVERING.value
            db.add(
                InboxDeliveryAttemptMemberModel(
                    attempt_uuid=attempt_uuid, message_id=message.id, position=position
                )
            )
        if challenge_sha256 is not None:
            db.add(
                InboxMessageTraceEventModel(
                    message_id=messages[0].id,
                    kind="attempt_challenge",
                    payload={
                        "attempt_uuid": attempt_uuid,
                        "challenge_sha256": challenge_sha256,
                    },
                )
            )
    return attempt_uuid


def settle_delivery_attempt(
    attempt_uuid: str,
    status: MessageStatus,
    outcome: str,
    reason: str | None = None,
    error: str | None = None,
    evidence: str = "{}",
    settled_status_gen=None,
    on_confirmed: Callable[[], None] | None = None,
) -> bool:
    with SessionLocal.begin() as db:
        row = db.query(InboxDeliveryAttemptModel).filter_by(attempt_uuid=attempt_uuid).one()
        if row.settled_at is not None:
            return False
        if outcome == "deferred":
            existing = (
                db.query(InboxDeliveryAttemptModel)
                .filter(
                    InboxDeliveryAttemptModel.attempt_uuid != attempt_uuid,
                    InboxDeliveryAttemptModel.receiver_terminal_id == row.receiver_terminal_id,
                    InboxDeliveryAttemptModel.payload_hash == row.payload_hash,
                    InboxDeliveryAttemptModel.reason == reason,
                    InboxDeliveryAttemptModel.outcome == "deferred",
                )
                .first()
            )
            if existing is not None:
                existing.count += 1
                existing.last_at = _utcnow()
                members = (
                    db.query(InboxDeliveryAttemptMemberModel)
                    .filter_by(attempt_uuid=attempt_uuid)
                    .all()
                )
                existing_ids = {
                    x.message_id
                    for x in db.query(InboxDeliveryAttemptMemberModel)
                    .filter_by(attempt_uuid=existing.attempt_uuid)
                    .all()
                }
                for member in members:
                    if member.message_id not in existing_ids:
                        member.attempt_uuid = existing.attempt_uuid
                    else:
                        db.delete(member)
                db.delete(row)
                ids = [m.message_id for m in members]
                db.query(InboxModel).filter(InboxModel.id.in_(ids)).update(
                    {InboxModel.status: status.value}, synchronize_session=False
                )
                return True
        row.outcome, row.reason, row.error = outcome, reason, error
        evidence_value = evidence
        if row.provider == "claude_code" and outcome not in {"confirmed", "failed"}:
            evidence_value = _canonical_json(_initialize_wpm2_cursor(_evidence_object(evidence)))
        preserve_evidence = (
            outcome == "ambiguous"
            and reason == "confirmation_timeout"
            and _is_wpm1_evidence(evidence_value)
        ) or _has_valid_redelivery_tag(evidence_value, row.prior_attempt_uuid)
        row.evidence = (
            _canonical_json(_evidence_object(evidence_value))
            if preserve_evidence
            else evidence_value[:2048]
        )
        row.settled_at = row.last_at = _utcnow()
        row.settled_status_gen = settled_status_gen
        ids = [
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        ]
        query = db.query(InboxModel).filter(InboxModel.id.in_(ids))
        if status == MessageStatus.DELIVERED:
            query = query.filter(InboxModel.status == MessageStatus.DELIVERING.value)
        changed = query.update({InboxModel.status: status.value}, synchronize_session=False)
        if status == MessageStatus.DELIVERED and changed != len(ids):
            raise RuntimeError("delivery confirmation compare-and-set lost")
        if status == MessageStatus.DELIVERED and on_confirmed is not None:
            on_confirmed()
        return True


def get_attempt_mailbox_authority(attempt_uuid: str) -> dict[str, Any] | None:
    """Return the immutable logical authority captured by an attempt opener."""
    with SessionLocal() as db:
        row = (
            db.query(InboxDeliveryAttemptModel.evidence)
            .filter_by(attempt_uuid=attempt_uuid)
            .one_or_none()
        )
        if row is None:
            return None
        authority = _evidence_object(row[0]).get("mailbox_authority")
        if not isinstance(authority, dict):
            return None
        if (
            not isinstance(authority.get("mailbox_id"), str)
            or type(authority.get("generation")) is not int
            or not isinstance(authority.get("current_terminal_id"), str)
        ):
            return None
        return authority


def get_current_mailbox_terminal(mailbox_id: str) -> str | None:
    """Resolve the current delivery target without rewriting any inbox cache."""
    with SessionLocal() as db:
        row = db.query(MailboxModel.current_terminal_id).filter_by(id=mailbox_id).first()
        return cast(str | None, row[0]) if row is not None else None


def settle_delivery_attempt_proof_safe(
    attempt_uuid: str,
    evidence: dict[str, Any],
    settled_status_gen=None,
    *,
    outcome: str = "ambiguous",
    reason: str = "confirmation_timeout",
) -> str:
    """Compensating post-submit settlement; never leaks into generic failure."""

    def operation(db) -> str:
        row = (
            db.query(InboxDeliveryAttemptModel)
            .filter_by(attempt_uuid=attempt_uuid, settled_at=None)
            .one_or_none()
        )
        if row is None:
            return "stale"
        members = (
            db.query(InboxDeliveryAttemptMemberModel).filter_by(attempt_uuid=attempt_uuid).all()
        )
        ids = [member.message_id for member in members]
        delivering = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(ids),
                InboxModel.status == MessageStatus.DELIVERING.value,
            )
            .count()
        )
        if delivering != len(ids):
            return "stale"
        row.outcome = outcome
        row.reason = reason
        safe_evidence = dict(evidence)
        if row.provider == "claude_code" and reason == "confirmation_timeout":
            safe_evidence = _initialize_wpm2_cursor(safe_evidence)
        row.evidence = _canonical_json(safe_evidence)
        row.settled_at = row.last_at = _utcnow()
        row.settled_status_gen = settled_status_gen
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(ids),
                InboxModel.status == MessageStatus.DELIVERING.value,
            )
            .update({InboxModel.status: MessageStatus.PENDING.value}, synchronize_session=False)
        )
        if changed != len(ids):
            raise RuntimeError("proof-safe settlement compare-and-set lost")
        return "settled"

    try:
        result = _run_wpm1_immediate(operation)
    except Exception:
        logger.exception("WPM2 proof-safe settlement failed for %s", attempt_uuid)
        return "settlement_pending_recovery"
    return result if result != "busy_aborted" else "settlement_pending_recovery"


def confirm_batch_from_prior_attempt(
    message_ids: list[int],
    prior_attempt_uuid: str,
    on_confirmed: Callable[[], None] | None = None,
) -> bool:
    """Atomically confirm a pending batch by an existing authoritative attempt."""
    with SessionLocal.begin() as db:
        referenced_ids = {
            row.message_id
            for row in db.query(InboxDeliveryAttemptMemberModel)
            .filter(
                InboxDeliveryAttemptMemberModel.attempt_uuid == prior_attempt_uuid,
                InboxDeliveryAttemptMemberModel.message_id.in_(message_ids),
            )
            .all()
        }
        if referenced_ids != set(message_ids):
            return False
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(message_ids),
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .update({InboxModel.status: MessageStatus.DELIVERED.value}, synchronize_session=False)
        )
        if changed != len(message_ids):
            return False
        if on_confirmed is not None:
            on_confirmed()
        return True


def _reply_created_at_utc(value: datetime) -> datetime:
    """Normalize local-naive inbox clocks with the pinned fold=0 policy."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=get_localzone(), fold=0).astimezone(timezone.utc)


def _attempt_started_at_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def find_inferred_delivery_evidence(
    message_id: int,
    attempted_receiver: str,
) -> dict[str, Any] | None:
    """Find a reply quoting a wire-only challenge for this message's exact attempt."""
    pattern = re.compile(rf"(?<![0-9a-f])mid {message_id}:([0-9a-f]{{32}})(?![0-9a-f])")
    with SessionLocal() as db:
        events = (
            db.query(InboxMessageTraceEventModel)
            .filter_by(message_id=message_id, kind="attempt_challenge")
            .order_by(InboxMessageTraceEventModel.created_at.asc(), InboxMessageTraceEventModel.id)
            .all()
        )
        if not events:
            return None
        replies = (
            db.query(InboxModel)
            .filter(InboxModel.sender_id == attempted_receiver)
            .order_by(InboxModel.created_at.asc(), InboxModel.id.asc())
            .all()
        )
        for event_row in events:
            payload: dict[str, Any] = (
                event_row.payload if isinstance(event_row.payload, dict) else {}
            )
            attempt_uuid = payload.get("attempt_uuid")
            challenge_sha256 = payload.get("challenge_sha256")
            if not isinstance(attempt_uuid, str) or not isinstance(challenge_sha256, str):
                continue
            attempt = (
                db.query(InboxDeliveryAttemptModel)
                .join(
                    InboxDeliveryAttemptMemberModel,
                    InboxDeliveryAttemptMemberModel.attempt_uuid
                    == InboxDeliveryAttemptModel.attempt_uuid,
                )
                .filter(
                    InboxDeliveryAttemptModel.attempt_uuid == attempt_uuid,
                    InboxDeliveryAttemptMemberModel.message_id == message_id,
                )
                .one_or_none()
            )
            if attempt is None:
                continue
            anchor = _attempt_started_at_utc(attempt.started_at)
            for reply in replies:
                normalized_reply = _reply_created_at_utc(reply.created_at)
                if normalized_reply <= anchor:
                    continue
                for raw_token in pattern.findall(str(reply.message)):
                    if hashlib.sha256(raw_token.encode()).hexdigest() != challenge_sha256:
                        continue
                    return {
                        "reply_message_id": reply.id,
                        "challenge_sha256": challenge_sha256,
                        "anchor_attempt_uuid": attempt_uuid,
                        "normalized_reply_at": normalized_reply.isoformat().replace("+00:00", "Z"),
                    }
    return None


def settle_open_attempt_inferred_delivered(
    attempt_uuid: str,
    evidence: dict[str, Any],
    on_confirmed: Callable[[], None] | None = None,
) -> bool:
    """Atomically settle one open attempt and its DELIVERING parent from challenge proof."""
    with SessionLocal.begin() as db:
        row = (
            db.query(InboxDeliveryAttemptModel)
            .filter_by(attempt_uuid=attempt_uuid, settled_at=None)
            .one_or_none()
        )
        if row is None:
            return False
        message_ids = [
            member.message_id
            for member in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        ]
        if len(message_ids) != 1:
            return False
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id == message_ids[0],
                InboxModel.status == MessageStatus.DELIVERING.value,
            )
            .update({InboxModel.status: MessageStatus.DELIVERED.value}, synchronize_session=False)
        )
        if changed != 1:
            return False
        now = _utcnow()
        row.outcome = "confirmed"
        row.reason = "inferred_by_reply"
        row.evidence = _canonical_json(dict(evidence))
        row.settled_at = row.last_at = now
        db.add(
            InboxMessageTraceEventModel(
                message_id=message_ids[0],
                kind="inferred_delivered",
                payload=dict(evidence),
                created_at=now,
            )
        )
        if on_confirmed is not None:
            on_confirmed()
        return True


def settle_attempt_inferred_delivered_batch(
    attempt_uuid: str,
    message_ids: list[int],
    evidence: dict[str, Any],
    on_confirmed: Callable[[], None] | None = None,
) -> bool:
    """Atomically settle one open attempt and its exact DELIVERING member set."""

    class _StaleBatchSettlement(Exception):
        pass

    expected_ids = set(message_ids)
    if not expected_ids or len(expected_ids) != len(message_ids):
        return False
    try:
        with SessionLocal.begin() as db:
            row = (
                db.query(InboxDeliveryAttemptModel)
                .filter_by(attempt_uuid=attempt_uuid, settled_at=None)
                .one_or_none()
            )
            if row is None:
                raise _StaleBatchSettlement
            member_ids = [
                member.message_id
                for member in db.query(InboxDeliveryAttemptMemberModel)
                .filter_by(attempt_uuid=attempt_uuid)
                .all()
            ]
            if len(member_ids) != len(expected_ids) or set(member_ids) != expected_ids:
                raise _StaleBatchSettlement
            changed = (
                db.query(InboxModel)
                .filter(
                    InboxModel.id.in_(expected_ids),
                    InboxModel.status == MessageStatus.DELIVERING.value,
                )
                .update(
                    {InboxModel.status: MessageStatus.DELIVERED.value},
                    synchronize_session=False,
                )
            )
            if changed != len(expected_ids):
                raise _StaleBatchSettlement
            now = _utcnow()
            row.outcome = "confirmed"
            row.reason = "inferred_by_execution"
            row.evidence = _canonical_json(dict(evidence))
            row.settled_at = row.last_at = now
            for message_id in sorted(expected_ids):
                db.add(
                    InboxMessageTraceEventModel(
                        message_id=message_id,
                        kind="inferred_delivered",
                        payload=dict(evidence),
                        created_at=now,
                    )
                )
    except _StaleBatchSettlement:
        return False
    if on_confirmed is not None:
        on_confirmed()
    return True


def transition_pending_to_inferred_delivered(
    message_id: int,
    evidence: dict[str, Any],
    on_confirmed: Callable[[], None] | None = None,
) -> bool:
    """Cap seam: atomically CAS PENDING to DELIVERED and append inferred evidence."""
    with SessionLocal.begin() as db:
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id == message_id,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .update({InboxModel.status: MessageStatus.DELIVERED.value}, synchronize_session=False)
        )
        if changed != 1:
            return False
        db.add(
            InboxMessageTraceEventModel(
                message_id=message_id,
                kind="inferred_delivered",
                payload=dict(evidence),
            )
        )
        if on_confirmed is not None:
            on_confirmed()
        return True


def get_message_trace(message_id: int) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        msg = db.query(InboxModel).filter_by(id=message_id).first()
        if not msg:
            return None
        rows = (
            db.query(InboxDeliveryAttemptModel, InboxDeliveryAttemptMemberModel.position)
            .join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid
                == InboxDeliveryAttemptModel.attempt_uuid,
            )
            .filter(InboxDeliveryAttemptMemberModel.message_id == message_id)
            .order_by(InboxDeliveryAttemptModel.started_at)
            .all()
        )
        attempts = []
        for row, position in rows:
            item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            for key in ("started_at", "settled_at", "last_at"):
                item[key] = item[key].isoformat() if item[key] else None
            item["position"] = position
            try:
                item["evidence"] = __import__("json").loads(item["evidence"])
            except Exception:
                item["evidence"] = {}
            attempts.append(item)
        event_rows = (
            db.query(InboxMessageTraceEventModel)
            .filter_by(message_id=message_id)
            .order_by(InboxMessageTraceEventModel.created_at, InboxMessageTraceEventModel.id)
            .all()
        )
        events: list[dict[str, Any]] = [
            {
                "id": row.id,
                "message_id": row.message_id,
                "kind": row.kind,
                "payload": row.payload if isinstance(row.payload, dict) else {},
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in event_rows
        ]
        return {
            "message": {
                "id": msg.id,
                "sender_id": msg.sender_id,
                "receiver_id": msg.receiver_id,
                "status": msg.status,
                "failure_reason": msg.failure_reason,
                "digested_into": msg.digested_into,
                "enqueue_generation": msg.enqueue_generation,
                "barrier_id": msg.barrier_id,
                "barrier_member_key": msg.barrier_member_key,
                "created_at": msg.created_at.isoformat(),
            },
            "attempts": attempts,
            "events": events,
        }


def count_ambiguous_attempts(message_ids: list[int]) -> int:
    with SessionLocal() as db:
        return (
            db.query(InboxDeliveryAttemptModel)
            .join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid
                == InboxDeliveryAttemptModel.attempt_uuid,
            )
            .filter(
                InboxDeliveryAttemptMemberModel.message_id.in_(message_ids),
                InboxDeliveryAttemptModel.outcome == "ambiguous",
                or_(
                    InboxDeliveryAttemptModel.reason.is_(None),
                    ~InboxDeliveryAttemptModel.reason.like("pane_identity_mismatch:%"),
                ),
            )
            .distinct()
            .count()
        )


def list_message_attempts(message_ids: list[int]) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = (
            db.query(InboxDeliveryAttemptModel)
            .join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid
                == InboxDeliveryAttemptModel.attempt_uuid,
            )
            .filter(InboxDeliveryAttemptMemberModel.message_id.in_(message_ids))
            .order_by(InboxDeliveryAttemptModel.started_at)
            .all()
        )
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows]


def list_attempt_member_ids(attempt_uuid: str) -> list[int]:
    with SessionLocal() as db:
        rows = (
            db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .order_by(InboxDeliveryAttemptMemberModel.position)
            .all()
        )
        return [row.message_id for row in rows]


def _evidence_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _run_wpm1_immediate(
    operation: Callable[[Any], _ImmediateResult],
) -> _ImmediateResult | str:
    """Run a paired WPM1 write with the frozen 3x1s busy policy."""
    for _ in range(3):
        db = SessionLocal()
        prior_timeout = None
        try:
            prior_timeout = int(db.execute(text("PRAGMA busy_timeout")).scalar() or 0)
            db.execute(text("PRAGMA busy_timeout=1000"))
            db.execute(text("BEGIN IMMEDIATE"))
            result = operation(db)
            db.commit()
            return result
        except Exception as exc:
            db.rollback()
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
        finally:
            try:
                if prior_timeout is not None:
                    db.execute(text(f"PRAGMA busy_timeout={prior_timeout}"))
            finally:
                db.close()
    return "busy_aborted"


def _p5_batch_key(message_ids: list[int]) -> str:
    return ",".join(str(value) for value in sorted(set(message_ids)))


def adopt_mailbox_rows_at_startup() -> int:
    """Tag legacy PENDING rows addressed to a known mailbox incarnation."""

    def operation(db: Any) -> int:
        if not _mailbox_schema_available(db):
            return 0
        rows = (
            db.query(InboxModel)
            .filter(
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.logical_receiver_id.is_(None),
            )
            .all()
        )
        changed = 0
        for row in rows:
            mailbox_id = _mailbox_id_for_terminal(db, row.receiver_id)
            if mailbox_id is not None:
                row.logical_receiver_id = mailbox_id
                changed += 1
        return changed

    result = _run_wpm1_immediate(operation)
    if result == "busy_aborted":
        raise RuntimeError("mailbox_startup_adoption_busy")
    return cast(int, result)


def _p5_orphan_predicate():
    logical_exists = exists().where(MailboxModel.id == InboxModel.logical_receiver_id)
    address_exists = exists().where(MailboxIncarnationModel.terminal_id == InboxModel.receiver_id)
    terminal_exists = exists().where(TerminalModel.id == InboxModel.receiver_id)
    return or_(
        and_(InboxModel.logical_receiver_id.is_not(None), ~logical_exists),
        and_(InboxModel.logical_receiver_id.is_(None), ~terminal_exists, ~address_exists),
    )


def _record_p5_orphan_notices(db: Any, rows: list[InboxModel]) -> tuple[int, int]:
    """Insert deterministic sender notices inside the owning settlement transaction."""
    batches: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        batches.setdefault((row.sender_id, row.receiver_id), []).append(row.id)

    notification_count = 0
    logged_only_count = 0
    for (sender_id, receiver_id), message_ids in sorted(batches.items()):
        ids = sorted(message_ids)
        if not _receiver_is_terminal_or_mailbox_address(db, sender_id):
            logged_only_count += 1
            logger.warning(
                "P5 orphan settlement has no live sender %s for receiver %s batch %s",
                sender_id,
                receiver_id,
                ids,
            )
            continue
        header = f"p5-orphan receiver={receiver_id} batch={_p5_batch_key(ids)}\n"
        notice_sender = f"message-trace:{receiver_id}"
        notice_receiver, logical_receiver_id, enqueue_generation = resolve_inbox_receiver(
            db, sender_id
        )
        existing = (
            db.query(InboxModel)
            .filter(
                InboxModel.sender_id == notice_sender,
                InboxModel.receiver_id == notice_receiver,
                text("substr(message, 1, :n) = :header").bindparams(n=len(header), header=header),
            )
            .first()
        )
        if existing is None:
            _insert_routed_inbox_row(
                db,
                sender_id=notice_sender,
                receiver_id=notice_receiver,
                logical_receiver_id=logical_receiver_id,
                message=(
                    header + f"[message-trace] delivery to terminal {receiver_id} failed "
                    f"because the receiver terminal no longer exists for "
                    f"message(s) {ids}."
                ),
                orchestration_type=OrchestrationType.SEND_MESSAGE,
            )
            notification_count += 1
    return notification_count, logged_only_count


def settle_pending_orphan_messages(
    limit: int = ORPHAN_RECONCILE_BATCH_LIMIT,
    receiver_ids: list[str] | None = None,
) -> OrphanReconcileResult:
    """Settle the oldest PENDING messages whose receiver row is absent."""
    if limit <= 0:
        raise ValueError("orphan reconcile limit must be positive")

    def operation(db: Any) -> OrphanReconcileResult:
        mailbox_schema = (
            db.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mailboxes'")
            ).first()
            is not None
        )
        if not mailbox_schema:
            # Unit seams may exercise the reconciler against a deliberately
            # pre-init legacy schema; production startup installs D5 first.
            return OrphanReconcileResult()
        orphan_predicate = _p5_orphan_predicate()
        candidate_query = db.query(InboxModel)
        if receiver_ids is not None:
            candidate_query = candidate_query.filter(InboxModel.receiver_id.in_(receiver_ids))
        candidates = (
            candidate_query.filter(
                InboxModel.status == MessageStatus.PENDING.value,
                orphan_predicate,
            )
            .order_by(InboxModel.created_at.asc(), InboxModel.id.asc())
            .limit(limit)
            .all()
        )
        settled: list[InboxModel] = []
        for candidate in candidates:
            changed = (
                db.query(InboxModel)
                .filter(
                    InboxModel.id == candidate.id,
                    InboxModel.status == MessageStatus.PENDING.value,
                    orphan_predicate,
                )
                .update(
                    {
                        InboxModel.status: MessageStatus.DELIVERY_FAILED.value,
                        InboxModel.failure_reason: (
                            "mailbox_deleted" if candidate.logical_receiver_id else "receiver_gone"
                        ),
                    },
                    synchronize_session=False,
                )
            )
            if changed == 1:
                settled.append(candidate)
        notification_count, logged_only_count = _record_p5_orphan_notices(db, settled)
        return OrphanReconcileResult(
            settled_count=len(settled),
            notification_count=notification_count,
            logged_only_count=logged_only_count,
        )

    result = _run_wpm1_immediate(operation)
    if isinstance(result, OrphanReconcileResult):
        if result.settled_count > 0:
            _remove_supervisor_pending_flag_if_drained()
        return result
    return OrphanReconcileResult(busy_aborted=True)


def settle_pending_receiver_gone_if_generation(
    receiver_id: str, lifecycle_generation: int
) -> OrphanReconcileResult:
    """P5 CAS settlement for a present row whose pane stayed gone under locks."""

    def operation(db: Any) -> OrphanReconcileResult:
        terminal = (
            db.query(TerminalModel.id)
            .filter(
                TerminalModel.id == receiver_id,
                TerminalModel.lifecycle_generation == lifecycle_generation,
            )
            .one_or_none()
        )
        if terminal is None:
            return OrphanReconcileResult()
        candidates = (
            db.query(InboxModel)
            .filter(
                InboxModel.receiver_id == receiver_id,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .order_by(InboxModel.created_at.asc(), InboxModel.id.asc())
            .limit(ORPHAN_RECONCILE_BATCH_LIMIT)
            .all()
        )
        candidate_ids = [candidate.id for candidate in candidates]
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(candidate_ids),
                InboxModel.status == MessageStatus.PENDING.value,
                exists().where(
                    and_(
                        TerminalModel.id == receiver_id,
                        TerminalModel.lifecycle_generation == lifecycle_generation,
                    )
                ),
            )
            .update(
                {
                    InboxModel.status: MessageStatus.DELIVERY_FAILED.value,
                    InboxModel.failure_reason: "receiver_gone",
                },
                synchronize_session=False,
            )
        )
        if changed != len(candidate_ids):
            db.rollback()
            return OrphanReconcileResult()
        notifications, logged_only = _record_p5_orphan_notices(db, candidates)
        return OrphanReconcileResult(
            settled_count=changed,
            notification_count=notifications,
            logged_only_count=logged_only,
        )

    result = _run_wpm1_immediate(operation)
    return (
        result
        if isinstance(result, OrphanReconcileResult)
        else OrphanReconcileResult(busy_aborted=True)
    )


def advance_wpm2_continuity_cursor(
    attempt_uuid: str,
    exact_message_ids: list[int],
    expected_ref: dict[str, Any],
    observed_ref: dict[str, Any],
) -> str:
    expected = _valid_cursor(expected_ref) or _valid_cursor(expected_ref, versioned=False)
    observed = _valid_cursor(observed_ref) or _valid_cursor(observed_ref, versioned=False)
    if expected is None or observed is None:
        return "stale"
    identity = ("path", "inode", "resolution_kind")
    if (
        any(expected[key] != observed[key] for key in identity)
        or observed["size"] < expected["size"]
    ):
        return "stale"
    ids = sorted(set(exact_message_ids))

    def operation(db) -> str:
        row = db.query(InboxDeliveryAttemptModel).filter_by(attempt_uuid=attempt_uuid).one_or_none()
        if (
            row is None
            or row.settled_at is None
            or row.outcome not in {"ambiguous", "interrupted", "deferred"}
        ):
            return "stale"
        if row.outcome == "deferred" and row.reason not in {"delivery_deferred", "input_blocked"}:
            return "stale"
        members = sorted(
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        )
        pending = (
            db.query(InboxModel)
            .filter(InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value)
            .count()
        )
        if members != ids or pending != len(ids):
            return "stale"
        evidence = _evidence_object(row.evidence)
        stored_raw = evidence.get("last_observed_ref")
        stored = _valid_cursor(stored_raw)
        upgrade = (
            stored is None and isinstance(stored_raw, dict) and "cursor_version" not in stored_raw
        )
        if stored is not None:
            if any(stored[key] != expected[key] for key in identity):
                return "stale"
            if stored["size"] > expected["size"]:
                if stored["size"] >= observed["size"]:
                    return "already_advanced"
            if stored["size"] < expected["size"]:
                return "stale"
        elif not upgrade and stored_raw is not None:
            return "stale"
        evidence["last_observed_ref"] = {
            **{key: observed[key] for key in identity},
            "size": observed["size"],
            "cursor_version": WPM2_CURSOR_VERSION,
        }
        row.evidence = _canonical_json(evidence)
        return "advanced"

    return _run_wpm1_immediate(operation)


def merge_wpm1_attempt_evidence(
    attempt_uuid: str, message_ids: list[int], updates: dict[str, Any]
) -> bool | str:
    """Conditionally merge WPM1 evidence; contention is a closed retry stop."""
    if not set(updates) <= WPM1_EVIDENCE_KEYS:
        raise ValueError("non-WPM1 evidence key")
    if "boundary_exhausted_at" in updates and "boundary_snapshot" not in updates:
        raise ValueError("boundary exhaustion requires atomic snapshot")

    def operation(db) -> str:
        row = (
            db.query(InboxDeliveryAttemptModel)
            .filter_by(
                attempt_uuid=attempt_uuid, outcome="ambiguous", reason="confirmation_timeout"
            )
            .first()
        )
        if row is None:
            return "stale"
        members = [
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        ]
        if set(members) != set(message_ids):
            return "stale"
        pending = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(message_ids), InboxModel.status == MessageStatus.PENDING.value
            )
            .count()
        )
        if pending != len(message_ids):
            return "stale"
        evidence = _evidence_object(row.evidence)
        evidence.update(updates)
        row.evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        return "merged"

    result = _run_wpm1_immediate(operation)
    if result == "merged":
        return True
    if result == "stale":
        return False
    return result


def _wpm1_batch_key(message_ids: list[int]) -> str:
    return ",".join(str(value) for value in sorted(set(message_ids)))


def _resolve_wpm1_recipient(db, sender_id: str, receiver_terminal_id: str) -> str | None:
    if _receiver_is_terminal_or_mailbox_address(db, sender_id):
        return sender_id
    receiver = db.query(TerminalModel).filter_by(id=receiver_terminal_id).first()
    caller_id = receiver.caller_id if receiver is not None else None
    if _receiver_is_terminal_or_mailbox_address(db, caller_id):
        return cast(str, caller_id)
    return None


def record_wpm1_stalled_notice(
    attempt_uuid: str,
    message_ids: list[int],
    receiver_terminal_id: str,
    notified_at: str,
) -> str:
    """Atomically mark a stalled batch and enqueue its exactly-once notice."""
    ids = sorted(set(message_ids))
    header = f"wpm1-notice kind=stalled batch={_wpm1_batch_key(ids)}\n"

    def operation(db) -> str:
        row = (
            db.query(InboxDeliveryAttemptModel)
            .filter_by(
                attempt_uuid=attempt_uuid, outcome="ambiguous", reason="confirmation_timeout"
            )
            .first()
        )
        if row is None:
            db.rollback()
            return "stale"
        members = {
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        }
        pending = (
            db.query(InboxModel)
            .filter(InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value)
            .count()
        )
        if members != set(ids) or pending != len(ids):
            db.rollback()
            return "stale"
        evidence = _evidence_object(row.evidence)
        if evidence.get("stalled_notified_at"):
            return "already_recorded"
        original = db.query(InboxModel).filter_by(id=ids[0]).one()
        evidence["stalled_notified_at"] = notified_at
        row.evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        recipient = _resolve_wpm1_recipient(db, original.sender_id, receiver_terminal_id)
        if recipient is None:
            logger.warning("WPM1 stalled notice has no live recipient for batch %s", ids)
            return "logged_only"
        sender = f"message-trace:{receiver_terminal_id}"
        existing = (
            db.query(InboxModel)
            .filter(
                InboxModel.sender_id == sender,
                InboxModel.receiver_id == recipient,
                text("substr(message, 1, :n) = :header").bindparams(n=len(header), header=header),
            )
            .first()
        )
        if existing is None:
            notice_receiver, logical_receiver_id, enqueue_generation = resolve_inbox_receiver(
                db, recipient
            )
            _insert_routed_inbox_row(
                db,
                sender_id=sender,
                receiver_id=notice_receiver,
                logical_receiver_id=logical_receiver_id,
                message=header + "delivery stalled: receiver shows no progress / payload not yet "
                "confirmed; no reinjection will occur while unproven; will confirm "
                "if consumed",
                orchestration_type=OrchestrationType.SEND_MESSAGE,
            )
        return "recorded"

    return _run_wpm1_immediate(operation)


def settle_wpm1_terminal_batch(
    message_ids: list[int],
    status: MessageStatus,
    receiver_terminal_id: str,
    *,
    reason: str | None = None,
    on_confirmed: Callable[[], None] | None = None,
    confirmation_evidence: tuple[str, dict[str, Any]] | None = None,
) -> str:
    """Merge the terminal clock before the exact-batch CAS, with corrective notice."""
    ids = sorted(set(message_ids))
    clock = _utcnow().isoformat().replace("+00:00", "Z")
    stalled_header = f"wpm1-notice kind=stalled batch={_wpm1_batch_key(ids)}\n"
    corrective_header = f"wpm1-notice kind=corrective batch={_wpm1_batch_key(ids)}\n"

    def operation(db) -> str:
        attempts = (
            db.query(InboxDeliveryAttemptModel)
            .join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid
                == InboxDeliveryAttemptModel.attempt_uuid,
            )
            .filter(
                InboxDeliveryAttemptMemberModel.message_id.in_(ids),
                InboxDeliveryAttemptModel.outcome == "ambiguous",
                InboxDeliveryAttemptModel.reason == "confirmation_timeout",
            )
            .order_by(InboxDeliveryAttemptModel.started_at.desc())
            .all()
        )
        target = next(
            (
                row
                for row in attempts
                if {
                    x.message_id
                    for x in db.query(InboxDeliveryAttemptMemberModel)
                    .filter_by(attempt_uuid=row.attempt_uuid)
                    .all()
                }
                == set(ids)
            ),
            None,
        )
        if target is None:
            db.rollback()
            return "stale"
        pending = (
            db.query(InboxModel)
            .filter(InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value)
            .count()
        )
        if pending != len(ids):
            db.rollback()
            return "stale"
        evidence = _evidence_object(target.evidence)
        evidence["terminal_settled_at"] = clock
        target.evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        if confirmation_evidence is not None:
            hit_uuid, hit_evidence = confirmation_evidence
            hit_target = next((row for row in attempts if row.attempt_uuid == hit_uuid), None)
            if hit_target is None:
                db.rollback()
                return "stale"
            hit_value = _evidence_object(hit_target.evidence)
            hit_value.update(hit_evidence)
            if hit_target is target:
                hit_value["terminal_settled_at"] = clock
            hit_target.evidence = _canonical_json(hit_value)
        if reason == "receiver_gone":
            target.reason = reason
        changed = (
            db.query(InboxModel)
            .filter(InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value)
            .update({InboxModel.status: status.value}, synchronize_session=False)
        )
        if changed != len(ids):
            db.rollback()
            return "stale"
        if status == MessageStatus.DELIVERED:
            any_stalled = any(
                _evidence_object(row.evidence).get("stalled_notified_at") for row in attempts
            )
            if any_stalled:
                sender = f"message-trace:{receiver_terminal_id}"
                stalled = (
                    db.query(InboxModel)
                    .filter(
                        InboxModel.sender_id == sender,
                        text("substr(message, 1, :n) = :header").bindparams(
                            n=len(stalled_header), header=stalled_header
                        ),
                    )
                    .first()
                )
                if stalled is not None and _receiver_is_terminal_or_mailbox_address(
                    db, stalled.receiver_id
                ):
                    existing = (
                        db.query(InboxModel)
                        .filter(
                            InboxModel.sender_id == sender,
                            InboxModel.receiver_id == stalled.receiver_id,
                            text("substr(message, 1, :n) = :header").bindparams(
                                n=len(corrective_header), header=corrective_header
                            ),
                        )
                        .first()
                    )
                    if existing is None:
                        (
                            notice_receiver,
                            logical_receiver_id,
                            enqueue_generation,
                        ) = resolve_inbox_receiver(db, stalled.receiver_id)
                        _insert_routed_inbox_row(
                            db,
                            sender_id=sender,
                            receiver_id=notice_receiver,
                            logical_receiver_id=logical_receiver_id,
                            message=corrective_header + "previously-stalled message was delivered",
                            orchestration_type=OrchestrationType.SEND_MESSAGE,
                        )
                else:
                    logger.warning(
                        "WPM1 corrective notice recipient is no longer available for batch %s", ids
                    )
            if on_confirmed is not None:
                on_confirmed()
        return "settled"

    return _run_wpm1_immediate(operation)


def get_callback_status_since(
    sender_id: str, receiver_id: str, since: datetime
) -> MessageStatus | None:
    """Return a newer callback status that suppresses the watchdog."""
    with SessionLocal() as db:
        mailbox_id = _mailbox_id_for_terminal(db, receiver_id)
        receiver_predicate = InboxModel.receiver_id == receiver_id
        if mailbox_id is not None:
            receiver_predicate = or_(
                receiver_predicate,
                InboxModel.logical_receiver_id == mailbox_id,
            )
        row = (
            db.query(InboxModel.status)
            .filter(
                InboxModel.sender_id == sender_id,
                receiver_predicate,
                InboxModel.created_at > since,
                InboxModel.status.in_(
                    (
                        MessageStatus.PENDING.value,
                        MessageStatus.HELD.value,
                        MessageStatus.DELIVERING.value,
                        MessageStatus.DELIVERED.value,
                        MessageStatus.DIGESTED.value,
                    )
                ),
            )
            .first()
        )
        return MessageStatus(row[0]) if row is not None else None


def transition_pending_to_delivery_failed(message_ids: list[int]) -> bool:
    """Cap transition; True exactly once even across process restarts."""
    with SessionLocal.begin() as db:
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(message_ids), InboxModel.status == MessageStatus.PENDING.value
            )
            .update(
                {InboxModel.status: MessageStatus.DELIVERY_FAILED.value}, synchronize_session=False
            )
        )
        return changed > 0


def list_stale_delivering_messages() -> List[InboxMessage]:
    with SessionLocal() as db:
        rows = db.query(InboxModel).filter_by(status=MessageStatus.DELIVERING.value).all()
        return [
            InboxMessage(
                id=x.id,
                sender_id=x.sender_id,
                receiver_id=x.receiver_id,
                message=x.message,
                orchestration_type=OrchestrationType(x.orchestration_type),
                status=MessageStatus(x.status),
                park_warm=bool(getattr(x, "park_warm", False)),
                created_at=x.created_at,
            )
            for x in rows
        ]


def list_stale_open_claude_attempts(age_seconds: int) -> list[dict[str, Any]]:
    bound = _utcnow() - timedelta(seconds=age_seconds)
    with SessionLocal() as db:
        rows = (
            db.query(InboxDeliveryAttemptModel)
            .filter(
                InboxDeliveryAttemptModel.provider == "claude_code",
                InboxDeliveryAttemptModel.settled_at.is_(None),
                InboxDeliveryAttemptModel.started_at <= bound,
            )
            .order_by(InboxDeliveryAttemptModel.started_at)
            .all()
        )
        return [
            {c.name: getattr(row, c.name) for c in row.__table__.columns}
            | {
                "message_ids": sorted(
                    x.message_id
                    for x in db.query(InboxDeliveryAttemptMemberModel)
                    .filter_by(attempt_uuid=row.attempt_uuid)
                    .all()
                )
            }
            for row in rows
        ]


def recover_wpm2_stale_attempt(
    attempt_uuid: str,
    exact_message_ids: list[int],
    status: MessageStatus,
    outcome: str,
    reason: str,
    evidence: dict[str, Any],
) -> str:
    ids = sorted(set(exact_message_ids))

    def operation(db) -> str:
        row = (
            db.query(InboxDeliveryAttemptModel)
            .filter_by(attempt_uuid=attempt_uuid, settled_at=None, provider="claude_code")
            .one_or_none()
        )
        if row is None:
            return "stale"
        members = sorted(
            x.message_id
            for x in db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(attempt_uuid=attempt_uuid)
            .all()
        )
        delivering_rows = (
            db.query(InboxModel)
            .filter(InboxModel.id.in_(ids), InboxModel.status == MessageStatus.DELIVERING.value)
            .all()
        )
        if members != ids or sorted(message.id for message in delivering_rows) != ids:
            return "stale"
        row.outcome, row.reason = outcome, reason
        row.evidence = _canonical_json(_initialize_wpm2_cursor(dict(evidence)))
        row.settled_at = row.last_at = _utcnow()
        updates: dict[Any, Any] = {InboxModel.status: status.value}
        if status == MessageStatus.DELIVERY_FAILED and reason == "receiver_gone":
            updates[InboxModel.failure_reason] = "receiver_gone"
        changed = (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_(ids),
                InboxModel.status == MessageStatus.DELIVERING.value,
            )
            .update(updates, synchronize_session=False)
        )
        if changed != len(ids):
            raise RuntimeError("stale recovery compare-and-set lost")
        if status == MessageStatus.DELIVERY_FAILED and reason == "receiver_gone":
            _record_p5_orphan_notices(db, delivering_rows)
        return "settled"

    return _run_wpm1_immediate(operation)


# Flow database functions


def create_flow(
    name: str,
    file_path: str,
    schedule: str,
    agent_profile: str,
    provider: str,
    script: str,
    next_run: datetime,
) -> Flow:
    """Create flow record."""
    with SessionLocal() as db:
        flow = FlowModel(
            name=name,
            file_path=file_path,
            schedule=schedule,
            agent_profile=agent_profile,
            provider=provider,
            script=script,
            next_run=next_run,
        )
        db.add(flow)
        db.commit()
        db.refresh(flow)
        return Flow(
            name=flow.name,
            file_path=flow.file_path,
            schedule=flow.schedule,
            agent_profile=flow.agent_profile,
            provider=flow.provider,
            script=flow.script,
            last_run=flow.last_run,
            next_run=flow.next_run,
            enabled=flow.enabled,
            prompt_template=None,
        )


def get_flow(name: str) -> Optional[Flow]:
    """Get flow by name."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if not flow:
            return None
        return Flow(
            name=flow.name,
            file_path=flow.file_path,
            schedule=flow.schedule,
            agent_profile=flow.agent_profile,
            provider=flow.provider,
            script=flow.script,
            last_run=flow.last_run,
            next_run=flow.next_run,
            enabled=flow.enabled,
            prompt_template=None,
        )


def list_flows() -> List[Flow]:
    """List all flows."""
    with SessionLocal() as db:
        flows = db.query(FlowModel).order_by(FlowModel.next_run).all()
        return [
            Flow(
                name=f.name,
                file_path=f.file_path,
                schedule=f.schedule,
                agent_profile=f.agent_profile,
                provider=f.provider,
                script=f.script,
                last_run=f.last_run,
                next_run=f.next_run,
                enabled=f.enabled,
                prompt_template=None,
            )
            for f in flows
        ]


def update_flow_run_times(name: str, last_run: datetime, next_run: datetime) -> bool:
    """Update flow run times after execution."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if flow:
            flow.last_run = last_run
            flow.next_run = next_run
            db.commit()
            return True
        return False


def update_flow_enabled(name: str, enabled: bool, next_run: Optional[datetime] = None) -> bool:
    """Update flow enabled status and optionally next_run."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if flow:
            flow.enabled = enabled
            if next_run is not None:
                flow.next_run = next_run
            db.commit()
            return True
        return False


def delete_flow(name: str) -> bool:
    """Delete flow."""
    with SessionLocal() as db:
        deleted = db.query(FlowModel).filter(FlowModel.name == name).delete()
        db.commit()
        return deleted > 0


def get_flows_to_run() -> List[Flow]:
    """Get enabled flows where next_run <= now."""
    with SessionLocal() as db:
        now = _utcnow()
        flows = (
            db.query(FlowModel).filter(FlowModel.enabled == True, FlowModel.next_run <= now).all()
        )
        return [
            Flow(
                name=f.name,
                file_path=f.file_path,
                schedule=f.schedule,
                agent_profile=f.agent_profile,
                provider=f.provider,
                script=f.script,
                last_run=f.last_run,
                next_run=f.next_run,
                enabled=f.enabled,
                prompt_template=None,
            )
            for f in flows
        ]


# ---------------------------------------------------------------------------
# F136 — Callback delivery typed APIs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackBatchRow:
    """One row in a callback notification batch."""

    inbox_row_id: int
    sender_id: str
    message: str
    created_at: datetime
    tag: str  # "replay" or "forward"


@dataclass(frozen=True)
class CallbackBatchResult:
    """Result of get_supervisor_callback_batch."""

    kind: str  # "ok", "stale_authority", "retryable_failure", "no_path"
    rows: tuple[CallbackBatchRow, ...]
    has_more: bool
    cursor: int | None
    inbox_path: str | None
    path_version: int
    bootstrap_mode: str | None
    reason: str


@dataclass(frozen=True)
class CallbackProgressResult:
    """Result of commit_supervisor_callback_progress."""

    kind: str  # "advanced", "stale_authority", "path_changed", "cas_mismatch",
    #            "invalid_range", "retryable_failure"
    reason: str


def enqueue_callback_replay(
    db: Session,
    *,
    mailbox_id: str,
    inbox_row_ids: list[int],
) -> int:
    """Insert replay entries for rows that became eligible below the cursor.

    Uses INSERT OR IGNORE for idempotency. Must be called within an existing
    transaction (the caller owns commit). Returns count of inserted rows.
    """
    if not inbox_row_ids:
        return 0
    now = _utcnow()
    inserted = 0
    for row_id in inbox_row_ids:
        try:
            db.execute(
                text(
                    "INSERT OR IGNORE INTO callback_replay_queue "
                    "(mailbox_id, inbox_row_id, queued_at) VALUES (:mb, :rid, :now)"
                ),
                {"mb": mailbox_id, "rid": row_id, "now": now},
            )
            inserted += 1
        except IntegrityError:
            pass
    return inserted


def get_supervisor_callback_batch(
    *,
    mailbox_id: str,
    terminal_id: str,
    generation: int,
    limit: int = 50,
) -> CallbackBatchResult:
    """D9: Get a batch of rows needing callback notification.

    Handles bootstrap, legacy adoption, replay cleaning, and combined selection.

    F351: Cheap-emptiness fast-path — when the cursor is set and no deliverable
    rows exist beyond it, skip the expensive BEGIN IMMEDIATE transaction.
    """
    try:
        # F351: lightweight pre-check (read-only, no exclusive lock)
        with SessionLocal() as db:
            _pre = (
                db.query(
                    MailboxModel.callback_notified_through_id,
                    MailboxModel.current_terminal_id,
                    MailboxModel.generation,
                    MailboxModel.cc_inbox_path,
                )
                .filter_by(id=mailbox_id)
                .one_or_none()
            )
            if (
                _pre is not None
                and _pre.cc_inbox_path
                and _pre.callback_notified_through_id is not None
            ):
                if _pre.current_terminal_id == terminal_id and int(_pre.generation) == generation:
                    _cursor_val = int(_pre.callback_notified_through_id)
                    _has_work = db.query(
                        db.query(InboxModel.id)
                        .filter(
                            InboxModel.logical_receiver_id == mailbox_id,
                            InboxModel.enqueue_generation == generation,
                            InboxModel.id > _cursor_val,
                            InboxModel.status == MessageStatus.PENDING.value,
                        )
                        .exists()
                    ).scalar()
                    if not _has_work:
                        _has_replay = db.query(
                            db.query(CallbackReplayQueueModel.id)
                            .filter(CallbackReplayQueueModel.mailbox_id == mailbox_id)
                            .exists()
                        ).scalar()
                        if not _has_replay:
                            _has_legacy = db.query(
                                db.query(InboxModel.id)
                                .filter(
                                    InboxModel.receiver_id == terminal_id,
                                    InboxModel.logical_receiver_id.is_(None),
                                    InboxModel.status == MessageStatus.PENDING.value,
                                )
                                .exists()
                            ).scalar()
                            if not _has_legacy:
                                return CallbackBatchResult(
                                    kind="ok",
                                    rows=(),
                                    has_more=False,
                                    cursor=_cursor_val,
                                    inbox_path=str(_pre.cc_inbox_path),
                                    path_version=0,
                                    bootstrap_mode=None,
                                    reason=None,
                                )

        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))

            # Validate authority
            mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
            if mailbox is None:
                return CallbackBatchResult(
                    kind="stale_authority",
                    rows=(),
                    has_more=False,
                    cursor=None,
                    inbox_path=None,
                    path_version=0,
                    bootstrap_mode=None,
                    reason="unknown_mailbox",
                )
            if mailbox.current_terminal_id != terminal_id or int(mailbox.generation) != generation:
                return CallbackBatchResult(
                    kind="stale_authority",
                    rows=(),
                    has_more=False,
                    cursor=None,
                    inbox_path=None,
                    path_version=0,
                    bootstrap_mode=None,
                    reason="terminal_or_generation_mismatch",
                )

            inbox_path = mailbox.cc_inbox_path
            if not inbox_path:
                return CallbackBatchResult(
                    kind="no_path",
                    rows=(),
                    has_more=False,
                    cursor=mailbox.callback_notified_through_id,
                    inbox_path=None,
                    path_version=int(mailbox.cc_inbox_path_version),
                    bootstrap_mode=None,
                    reason="no_inbox_path_configured",
                )

            path_version = int(mailbox.cc_inbox_path_version)
            bootstrap_mode: str | None = None

            # D6: Adopt legacy raw PENDING rows for this supervisor terminal
            adopted_ids: list[int] = []
            legacy_rows = (
                db.query(InboxModel.id)
                .filter(
                    InboxModel.receiver_id == terminal_id,
                    InboxModel.logical_receiver_id.is_(None),
                    InboxModel.status == MessageStatus.PENDING.value,
                )
                .all()
            )
            for (row_id,) in legacy_rows:
                db.execute(
                    text(
                        "UPDATE inbox SET logical_receiver_id = :mb, "
                        "enqueue_generation = :gen "
                        "WHERE id = :rid AND logical_receiver_id IS NULL"
                    ),
                    {"mb": mailbox_id, "gen": generation, "rid": row_id},
                )
                adopted_ids.append(row_id)

            # D8: Bootstrap if cursor is NULL
            # F157 hotfix: also respect consumed_through_id so acked messages
            # are never re-pushed by the callback runner.
            notified_cursor = mailbox.callback_notified_through_id
            consumption_cursor = (
                int(mailbox.consumed_through_id) if mailbox.consumed_through_id else 0
            )
            cursor = notified_cursor
            if cursor is None:
                min_id_row = (
                    db.query(func.min(InboxModel.id))
                    .filter(
                        InboxModel.logical_receiver_id == mailbox_id,
                        InboxModel.receiver_id == terminal_id,
                        InboxModel.enqueue_generation == generation,
                        InboxModel.status == MessageStatus.PENDING.value,
                    )
                    .scalar()
                )
                cursor = max(0, (min_id_row or 1) - 1)
                mailbox.callback_notified_through_id = cursor
                bootstrap_mode = "current_generation_pending_replay"

            # F157 hotfix: effective cursor is max(notified, consumed) so that
            # messages the supervisor already acked are never re-pushed.
            cursor = max(cursor, consumption_cursor)

            # Enqueue adopted rows below cursor into replay
            for row_id in adopted_ids:
                if row_id <= cursor:
                    db.execute(
                        text(
                            "INSERT OR IGNORE INTO callback_replay_queue "
                            "(mailbox_id, inbox_row_id, queued_at) VALUES (:mb, :rid, :now)"
                        ),
                        {"mb": mailbox_id, "rid": row_id, "now": _utcnow()},
                    )

            # Clean replay entries whose inbox row is no longer current eligible PENDING
            db.execute(
                text(
                    "DELETE FROM callback_replay_queue WHERE mailbox_id = :mb "
                    "AND inbox_row_id NOT IN ("
                    "  SELECT id FROM inbox WHERE logical_receiver_id = :mb "
                    "  AND receiver_id = :tid AND enqueue_generation = :gen "
                    "  AND status = 'pending'"
                    ")"
                ),
                {"mb": mailbox_id, "tid": terminal_id, "gen": generation},
            )

            # Select replay rows first
            replay_rows_raw = db.execute(
                text(
                    "SELECT crq.inbox_row_id, i.sender_id, i.message, i.created_at "
                    "FROM callback_replay_queue crq "
                    "JOIN inbox i ON i.id = crq.inbox_row_id "
                    "WHERE crq.mailbox_id = :mb "
                    "  AND i.logical_receiver_id = :mb "
                    "  AND i.receiver_id = :tid "
                    "  AND i.enqueue_generation = :gen "
                    "  AND i.status = 'pending' "
                    "ORDER BY crq.inbox_row_id ASC "
                    "LIMIT :lim"
                ),
                {"mb": mailbox_id, "tid": terminal_id, "gen": generation, "lim": limit + 1},
            ).fetchall()

            replay_batch: list[CallbackBatchRow] = []
            for row in replay_rows_raw[:limit]:
                replay_batch.append(
                    CallbackBatchRow(
                        inbox_row_id=int(row[0]),
                        sender_id=str(row[1]),
                        message=str(row[2]),
                        created_at=row[3] if row[3] else _utcnow(),
                        tag="replay",
                    )
                )

            remaining = limit - len(replay_batch)
            forward_batch: list[CallbackBatchRow] = []
            has_more = len(replay_rows_raw) > limit

            if remaining > 0:
                forward_rows_raw = db.execute(
                    text(
                        "SELECT i.id, i.sender_id, i.message, i.created_at "
                        "FROM inbox i "
                        "WHERE i.logical_receiver_id = :mb "
                        "  AND i.receiver_id = :tid "
                        "  AND i.enqueue_generation = :gen "
                        "  AND i.status = 'pending' "
                        "  AND i.id > :cursor "
                        "ORDER BY i.id ASC "
                        "LIMIT :lim"
                    ),
                    {
                        "mb": mailbox_id,
                        "tid": terminal_id,
                        "gen": generation,
                        "cursor": cursor,
                        "lim": remaining + 1,
                    },
                ).fetchall()

                for row in forward_rows_raw[:remaining]:
                    forward_batch.append(
                        CallbackBatchRow(
                            inbox_row_id=int(row[0]),
                            sender_id=str(row[1]),
                            message=str(row[2]),
                            created_at=row[3] if row[3] else _utcnow(),
                            tag="forward",
                        )
                    )
                if len(forward_rows_raw) > remaining:
                    has_more = True

            db.commit()
            return CallbackBatchResult(
                kind="ok",
                rows=tuple(replay_batch + forward_batch),
                has_more=has_more,
                cursor=cursor,
                inbox_path=inbox_path,
                path_version=path_version,
                bootstrap_mode=bootstrap_mode,
                reason="ok",
            )
    except OperationalError as exc:
        logger.warning("f136_callback_batch_db_error: %s", exc)
        return CallbackBatchResult(
            kind="retryable_failure",
            rows=(),
            has_more=False,
            cursor=None,
            inbox_path=None,
            path_version=0,
            bootstrap_mode=None,
            reason=f"db_error: {exc}",
        )


def commit_supervisor_callback_progress(
    *,
    mailbox_id: str,
    terminal_id: str,
    generation: int,
    expected_cursor: int,
    new_cursor: int,
    expected_path_version: int,
    replay_row_ids: tuple[int, ...] = (),
) -> CallbackProgressResult:
    """D9: Atomically advance cursor and drain successful replay IDs."""
    if new_cursor < expected_cursor:
        return CallbackProgressResult(kind="invalid_range", reason="cursor_retreat")

    try:
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))

            mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
            if mailbox is None:
                return CallbackProgressResult(kind="stale_authority", reason="unknown_mailbox")

            if mailbox.current_terminal_id != terminal_id or int(mailbox.generation) != generation:
                return CallbackProgressResult(
                    kind="stale_authority", reason="terminal_or_generation_mismatch"
                )

            if int(mailbox.cc_inbox_path_version) != expected_path_version:
                return CallbackProgressResult(kind="path_changed", reason="path_version_mismatch")

            current_cursor = mailbox.callback_notified_through_id
            if current_cursor is None:
                current_cursor = 0
            if int(current_cursor) != expected_cursor:
                # F203 D14/S2: monotonic advance fallback — if new_cursor >
                # current_cursor, advance unconditionally even on CAS mismatch.
                # This prevents the stall where the CAS guard rejects stale callers
                # and no fallback writer exists.
                if new_cursor > int(current_cursor):
                    mailbox.callback_notified_through_id = new_cursor
                    mailbox.updated_at = _utcnow()
                    db.commit()
                    return CallbackProgressResult(
                        kind="advanced_monotonic", reason="cas_bypass_monotonic"
                    )
                return CallbackProgressResult(kind="cas_mismatch", reason="cursor_mismatch")

            # Advance cursor (equal is valid for replay-only progress)
            if new_cursor > expected_cursor:
                mailbox.callback_notified_through_id = new_cursor
                mailbox.updated_at = _utcnow()

            # Delete successful replay IDs
            if replay_row_ids:
                for rid in replay_row_ids:
                    db.execute(
                        text(
                            "DELETE FROM callback_replay_queue "
                            "WHERE mailbox_id = :mb AND inbox_row_id = :rid"
                        ),
                        {"mb": mailbox_id, "rid": rid},
                    )

            db.commit()
            return CallbackProgressResult(kind="advanced", reason="ok")
    except OperationalError as exc:
        logger.warning("f136_callback_progress_db_error: %s", exc)
        return CallbackProgressResult(kind="retryable_failure", reason=f"db_error: {exc}")


# --- F476: Single wake cursor (D1/D3/D4) -------------------------------------

# Constants (matching f213's proven values)
_WAKE_COOLDOWN_SECONDS = 300.0
_WAKE_STREAK_CAP = 3


@dataclass(frozen=True)
class WakeClaimResult:
    """Result of claim_unnotified_wake (D3)."""

    kind: str
    # "stale_authority", "authority_lock_contention", "wake_exhausted",
    # "lease_held", "claimed"
    rows: tuple[CallbackBatchRow, ...]
    claimed_high_water: int
    path_version: int
    reason: str
    exhausted_id: int | None = None  # only set when kind == "wake_exhausted"


@dataclass(frozen=True)
class WakeCommitResult:
    """Result of commit_wake (D3)."""

    kind: str
    # "committed", "superseded_by_ack", "stale_authority", "path_changed",
    # "lease_lost", "invalid_range"
    reason: str


def claim_unnotified_wake(
    *,
    mailbox_id: str,
    terminal_id: str,
    generation: int,
    limit: int = 50,
) -> WakeClaimResult:
    """D3/D4: Select wake-eligible rows and lease them.

    Acquires the mailbox authority lock. Returns rows above
    max(callback_notified_through_id, consumed_through_id) plus any
    replay queue entries. Stamps the D4 lease (wake_notified_at/wake_notified_id)
    WITHOUT advancing callback_notified_through_id.

    Precedence: stale_authority → authority_lock_contention → wake_exhausted
    → lease_held → claimed.
    """
    from cli_agent_orchestrator.services.mailbox_service import get_mailbox_authority_lock

    # Resolve mailbox session/role for lock
    with SessionLocal() as db:
        mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if mailbox is None:
            return WakeClaimResult(
                kind="stale_authority",
                rows=(),
                claimed_high_water=0,
                path_version=0,
                reason="unknown_mailbox",
            )
        if mailbox.current_terminal_id != terminal_id or int(mailbox.generation) != generation:
            return WakeClaimResult(
                kind="stale_authority",
                rows=(),
                claimed_high_water=0,
                path_version=0,
                reason="terminal_or_generation_mismatch",
            )
        session_name = str(mailbox.session_name)
        role = str(mailbox.role)

    # D1: acquire authority lock with 0.5s timeout
    lock = get_mailbox_authority_lock(session_name, role)
    if not lock.acquire(timeout=0.5):
        return WakeClaimResult(
            kind="authority_lock_contention",
            rows=(),
            claimed_high_water=0,
            path_version=0,
            reason="authority_lock_contention",
        )

    try:
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))

            # Re-validate authority under lock
            mailbox = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
            if mailbox is None:
                return WakeClaimResult(
                    kind="stale_authority",
                    rows=(),
                    claimed_high_water=0,
                    path_version=0,
                    reason="unknown_mailbox",
                )
            if mailbox.current_terminal_id != terminal_id or int(mailbox.generation) != generation:
                return WakeClaimResult(
                    kind="stale_authority",
                    rows=(),
                    claimed_high_water=0,
                    path_version=0,
                    reason="terminal_or_generation_mismatch",
                )

            path_version = int(mailbox.cc_inbox_path_version)
            notified_cursor = int(mailbox.callback_notified_through_id or 0)
            consumed_cursor = int(mailbox.consumed_through_id or 0)
            effective_cursor = max(notified_cursor, consumed_cursor)

            # D4: Read lease state
            wake_notified_at = mailbox.wake_notified_at
            wake_streak = int(mailbox.wake_streak or 0)
            wake_notified_id = int(mailbox.wake_notified_id or 0)
            now = _utcnow()

            # Check wake_exhausted (before lease check per D4 precedence)
            # B5: Only block if there is no strictly newer pending forward row
            if wake_streak >= _WAKE_STREAK_CAP and wake_notified_id > 0:
                # Exhaustion clears when consumed_through_id passes the stuck id
                if wake_notified_id > consumed_cursor:
                    # B5: check if newer work exists that can supersede exhaustion
                    has_newer = db.execute(
                        text(
                            "SELECT 1 FROM inbox "
                            "WHERE logical_receiver_id = :mb "
                            "  AND receiver_id = :tid "
                            "  AND enqueue_generation = :gen "
                            "  AND status = 'pending' "
                            "  AND id > :stuck_id "
                            "LIMIT 1"
                        ),
                        {
                            "mb": mailbox_id,
                            "tid": terminal_id,
                            "gen": generation,
                            "stuck_id": wake_notified_id,
                        },
                    ).fetchone()
                    if not has_newer:
                        # B5: log WARNING once per exhaustion episode
                        logger.warning(
                            "f476_wake_exhausted mailbox=%s wake_notified_id=%d streak=%d"
                            " — row stuck; supervisor may need manual intervention",
                            mailbox_id,
                            wake_notified_id,
                            wake_streak,
                        )
                        db.commit()
                        return WakeClaimResult(
                            kind="wake_exhausted",
                            rows=(),
                            claimed_high_water=notified_cursor,
                            path_version=path_version,
                            reason="streak_cap_reached",
                            exhausted_id=wake_notified_id,
                        )

            # D4: Lease visibility predicate
            # Row is leased if: wake_notified_at is NOT NULL AND
            # now - wake_notified_at <= 300s AND claimed_high_water <= wake_notified_id
            lease_held = False
            if wake_notified_at is not None:
                _wake_ts = _as_utc(wake_notified_at)
                elapsed = (now - _wake_ts).total_seconds() if _wake_ts else 999.0
                if elapsed <= _WAKE_COOLDOWN_SECONDS:
                    # Lease still valid — check if we can supersede
                    lease_held = True

            # Select replay rows
            replay_rows_raw = db.execute(
                text(
                    "SELECT crq.inbox_row_id, i.sender_id, i.message, i.created_at "
                    "FROM callback_replay_queue crq "
                    "JOIN inbox i ON i.id = crq.inbox_row_id "
                    "WHERE crq.mailbox_id = :mb "
                    "  AND i.logical_receiver_id = :mb "
                    "  AND i.receiver_id = :tid "
                    "  AND i.enqueue_generation = :gen "
                    "  AND i.status = 'pending' "
                    "ORDER BY crq.inbox_row_id ASC "
                    "LIMIT :lim"
                ),
                {"mb": mailbox_id, "tid": terminal_id, "gen": generation, "lim": limit},
            ).fetchall()

            replay_batch: list[CallbackBatchRow] = []
            for row in replay_rows_raw:
                replay_batch.append(
                    CallbackBatchRow(
                        inbox_row_id=int(row[0]),
                        sender_id=str(row[1]),
                        message=str(row[2]),
                        created_at=row[3] if row[3] else now,
                        tag="replay",
                    )
                )

            # Select forward rows above effective cursor
            remaining = limit - len(replay_batch)
            forward_batch: list[CallbackBatchRow] = []
            if remaining > 0:
                forward_rows_raw = db.execute(
                    text(
                        "SELECT i.id, i.sender_id, i.message, i.created_at "
                        "FROM inbox i "
                        "WHERE i.logical_receiver_id = :mb "
                        "  AND i.receiver_id = :tid "
                        "  AND i.enqueue_generation = :gen "
                        "  AND i.status = 'pending' "
                        "  AND i.id > :cursor "
                        "ORDER BY i.id ASC "
                        "LIMIT :lim"
                    ),
                    {
                        "mb": mailbox_id,
                        "tid": terminal_id,
                        "gen": generation,
                        "cursor": effective_cursor,
                        "lim": remaining,
                    },
                ).fetchall()

                for row in forward_rows_raw:
                    forward_batch.append(
                        CallbackBatchRow(
                            inbox_row_id=int(row[0]),
                            sender_id=str(row[1]),
                            message=str(row[2]),
                            created_at=row[3] if row[3] else now,
                            tag="forward",
                        )
                    )

            # B3: AC3 committed-pending lost-wake recovery.
            # If no forward rows AND notified_cursor > consumed_cursor, look for
            # pending rows that were committed but never consumed (still pending
            # between consumed_cursor and notified_cursor). These are recovery
            # candidates subject to the 300s cooldown (lease_held blocks them).
            recovery_batch: list[CallbackBatchRow] = []
            if not forward_batch and not replay_batch and notified_cursor > consumed_cursor:
                recovery_rows_raw = db.execute(
                    text(
                        "SELECT i.id, i.sender_id, i.message, i.created_at "
                        "FROM inbox i "
                        "WHERE i.logical_receiver_id = :mb "
                        "  AND i.receiver_id = :tid "
                        "  AND i.enqueue_generation = :gen "
                        "  AND i.status = 'pending' "
                        "  AND i.id > :consumed "
                        "  AND i.id <= :notified "
                        "ORDER BY i.id ASC "
                        "LIMIT :lim"
                    ),
                    {
                        "mb": mailbox_id,
                        "tid": terminal_id,
                        "gen": generation,
                        "consumed": consumed_cursor,
                        "notified": notified_cursor,
                        "lim": limit,
                    },
                ).fetchall()
                for row in recovery_rows_raw:
                    recovery_batch.append(
                        CallbackBatchRow(
                            inbox_row_id=int(row[0]),
                            sender_id=str(row[1]),
                            message=str(row[2]),
                            created_at=row[3] if row[3] else now,
                            tag="forward",  # treated as forward for cursor purposes
                        )
                    )

            all_rows = tuple(replay_batch + forward_batch + recovery_batch)

            # Compute claimed_high_water from forward rows only
            if forward_batch:
                claimed_high_water = max(r.inbox_row_id for r in forward_batch)
            else:
                claimed_high_water = notified_cursor

            # D4 lease check: if lease held, return lease_held (no bypass for replay — B4)
            if lease_held and claimed_high_water <= wake_notified_id:
                db.commit()
                return WakeClaimResult(
                    kind="lease_held",
                    rows=(),
                    claimed_high_water=notified_cursor,
                    path_version=path_version,
                    reason="lease_active",
                )

            # No rows at all → empty claim (still "claimed" kind)
            if not all_rows:
                db.commit()
                return WakeClaimResult(
                    kind="claimed",
                    rows=(),
                    claimed_high_water=notified_cursor,
                    path_version=path_version,
                    reason="empty",
                )

            # Stamp the lease (D4): wake_notified_at = now, wake_notified_id = claimed_high_water
            mailbox.wake_notified_at = now
            mailbox.wake_notified_id = claimed_high_water
            mailbox.updated_at = now
            db.commit()

            return WakeClaimResult(
                kind="claimed",
                rows=all_rows,
                claimed_high_water=claimed_high_water,
                path_version=path_version,
                reason="ok",
            )
    except OperationalError as exc:
        logger.warning("f476_wake_claim_db_error: %s", exc)
        return WakeClaimResult(
            kind="authority_lock_contention",
            rows=(),
            claimed_high_water=0,
            path_version=0,
            reason=f"db_error: {exc}",
        )
    finally:
        lock.release()


def commit_wake(
    *,
    mailbox_id: str,
    terminal_id: str,
    generation: int,
    through_id: int,
    claimed_high_water: int,
    expected_path_version: int,
    replay_row_ids: tuple[int, ...] = (),
) -> WakeCommitResult:
    """D3: Advance callback_notified_through_id and drain replay entries.

    Must be called AFTER claim and BEFORE emit. Returns the commit verdict.
    Runs under the mailbox authority lock (D1).
    through_id must be <= claimed_high_water (B2: bound to claim).
    """
    from cli_agent_orchestrator.services.mailbox_service import get_mailbox_authority_lock

    # B2: reject through_id beyond claimed_high_water
    if through_id > claimed_high_water:
        return WakeCommitResult(
            kind="invalid_range", reason="through_id_exceeds_claimed_high_water"
        )

    # Resolve mailbox for lock
    with SessionLocal() as db:
        mailbox: Any = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if mailbox is None:
            return WakeCommitResult(kind="stale_authority", reason="unknown_mailbox")
        if mailbox.current_terminal_id != terminal_id or int(mailbox.generation) != generation:
            return WakeCommitResult(
                kind="stale_authority", reason="terminal_or_generation_mismatch"
            )
        session_name = str(mailbox.session_name)
        role = str(mailbox.role)

    lock = get_mailbox_authority_lock(session_name, role)
    if not lock.acquire(timeout=0.5):
        return WakeCommitResult(kind="stale_authority", reason="authority_lock_contention")

    try:
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))

            mailbox = db.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
            if mailbox is None:
                return WakeCommitResult(kind="stale_authority", reason="unknown_mailbox")
            if mailbox.current_terminal_id != terminal_id or int(mailbox.generation) != generation:
                return WakeCommitResult(
                    kind="stale_authority", reason="terminal_or_generation_mismatch"
                )

            # Path guard (D3)
            if int(mailbox.cc_inbox_path_version) != expected_path_version:
                return WakeCommitResult(kind="path_changed", reason="path_version_mismatch")

            # D2: superseded_by_ack check (forward high-water only)
            current_cursor = int(mailbox.callback_notified_through_id or 0)
            consumed_cursor = int(mailbox.consumed_through_id or 0)

            if through_id > current_cursor and through_id <= consumed_cursor:
                # Forward high-water already acked — superseded
                db.commit()
                return WakeCommitResult(kind="superseded_by_ack", reason="forward_already_consumed")

            # Replay-only commit: through_id == current_cursor, non-empty replay_row_ids
            # is never superseded (its rows are pending by construction).

            # Lease check: verify the lease is still ours
            wake_notified_id = int(mailbox.wake_notified_id or 0)
            wake_notified_at = mailbox.wake_notified_at
            if wake_notified_at is None and through_id > current_cursor:
                # Another commit already cleared the lease
                return WakeCommitResult(kind="lease_lost", reason="lease_already_cleared")
            # B2: verify claimed_high_water matches what the lease stamped
            if wake_notified_id != 0 and claimed_high_water != wake_notified_id:
                # A different claim stamped a different high-water
                return WakeCommitResult(kind="lease_lost", reason="claim_mismatch")

            now = _utcnow()

            # Advance cursor
            if through_id > current_cursor:
                mailbox.callback_notified_through_id = through_id

                # D4: streak management
                # Reset streak when through_id advances the cursor (new forward content)
                if through_id > current_cursor:
                    mailbox.wake_streak = 0
                else:
                    mailbox.wake_streak = int(mailbox.wake_streak or 0) + 1
            elif replay_row_ids:
                # Replay-only: increment streak (same high-water)
                mailbox.wake_streak = int(mailbox.wake_streak or 0) + 1

            # Clear the lease
            mailbox.wake_notified_at = None
            mailbox.updated_at = now

            # Drain replay entries
            if replay_row_ids:
                for rid in replay_row_ids:
                    db.execute(
                        text(
                            "DELETE FROM callback_replay_queue "
                            "WHERE mailbox_id = :mb AND inbox_row_id = :rid"
                        ),
                        {"mb": mailbox_id, "rid": rid},
                    )

            db.commit()
            return WakeCommitResult(kind="committed", reason="ok")
    except OperationalError as exc:
        logger.warning("f476_wake_commit_db_error: %s", exc)
        return WakeCommitResult(kind="stale_authority", reason=f"db_error: {exc}")
    finally:
        lock.release()


# --- F138: Orphan reconciliation migration and DB helpers ---------------------


def _migrate_f138_orphan_reconciliation() -> None:
    """F138: Create process_incarnations and orphan_reconcile_jobs tables."""
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "process_incarnations" not in tables:
            connection.execute(
                text(
                    "CREATE TABLE process_incarnations ("
                    "  id TEXT PRIMARY KEY,"
                    "  terminal_id TEXT NOT NULL,"
                    "  terminal_generation INTEGER NOT NULL,"
                    "  token TEXT NOT NULL UNIQUE,"
                    "  token_hash TEXT NOT NULL UNIQUE,"
                    "  owner_uid INTEGER NOT NULL,"
                    "  provider TEXT NOT NULL,"
                    "  pane_pid INTEGER,"
                    "  pane_start_ticks INTEGER,"
                    "  state TEXT NOT NULL CHECK (state IN "
                    "    ('launching','active','reconcile_pending','reconciled','abandoned')),"
                    "  created_at DATETIME NOT NULL,"
                    "  activated_at DATETIME,"
                    "  reconciled_at DATETIME,"
                    "  UNIQUE(terminal_id, terminal_generation)"
                    ")"
                )
            )
        if "orphan_reconcile_jobs" not in tables:
            connection.execute(
                text(
                    "CREATE TABLE orphan_reconcile_jobs ("
                    "  id TEXT PRIMARY KEY,"
                    "  incarnation_id TEXT NOT NULL UNIQUE,"
                    "  terminal_id TEXT NOT NULL,"
                    "  terminal_generation INTEGER NOT NULL,"
                    "  state TEXT NOT NULL CHECK (state IN "
                    "    ('pending','leased','retry_wait','succeeded','attention_required')),"
                    "  attempt INTEGER NOT NULL DEFAULT 0,"
                    "  lease_owner TEXT,"
                    "  lease_expires_at DATETIME,"
                    "  next_attempt_at DATETIME,"
                    "  gone_observed_at DATETIME NOT NULL,"
                    "  source TEXT NOT NULL,"
                    "  last_result_json TEXT,"
                    "  notified_failure_code TEXT,"
                    "  created_at DATETIME NOT NULL,"
                    "  updated_at DATETIME NOT NULL"
                    ")"
                )
            )
        # D17: Issuance context columns (idempotent ALTER)
        _migrate_f138_issuance_context(connection)
        # F166: notify_count column (idempotent ALTER)
        _migrate_f166_notify_count(connection)


def _migrate_f138_issuance_context(connection) -> None:
    """D17: Add issuance_ticks and issuance_boot_id to process_incarnations if missing."""
    existing_cols = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info('process_incarnations')")).fetchall()
    }
    if "issuance_ticks" not in existing_cols:
        connection.execute(
            text("ALTER TABLE process_incarnations ADD COLUMN issuance_ticks INTEGER")
        )
    if "issuance_boot_id" not in existing_cols:
        connection.execute(
            text("ALTER TABLE process_incarnations ADD COLUMN issuance_boot_id TEXT")
        )


def _migrate_f166_notify_count(connection) -> None:
    """F166 D7: Add notify_count to orphan_reconcile_jobs if missing (idempotent)."""
    existing_cols = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info('orphan_reconcile_jobs')")).fetchall()
    }
    if "notify_count" not in existing_cols:
        connection.execute(
            text("ALTER TABLE orphan_reconcile_jobs ADD COLUMN notify_count INTEGER")
        )


# --- F138 liveness observation tracking (in-memory for confirmation count) ----

import threading as _f138_threading

_f138_observation_lock = _f138_threading.Lock()
# Key: (terminal_id, terminal_generation, incarnation_id) -> consecutive "gone" count
_f138_gone_counts: dict[tuple[str, int, str], int] = {}
_GONE_CONFIRM_THRESHOLD = 2


def f138_record_liveness_observation(
    *,
    terminal_id: str,
    terminal_generation: int,
    incarnation_id: str,
    state: str,
    source: str,
) -> "LivenessObservationResult":
    """Record liveness observation; queue job on 2 consecutive gone."""
    from cli_agent_orchestrator.services.orphan_reconcile_service import (
        LivenessObservationResult,
    )

    key = (terminal_id, terminal_generation, incarnation_id)

    if state == "error":
        return LivenessObservationResult(
            job_queued=False, confirmation_count=0, detail="error_ignored"
        )

    with _f138_observation_lock:
        if state == "live":
            _f138_gone_counts.pop(key, None)
            return LivenessObservationResult(
                job_queued=False, confirmation_count=0, detail="reset_live"
            )

        # state == "gone"
        count = _f138_gone_counts.get(key, 0) + 1
        _f138_gone_counts[key] = count

    if count >= _GONE_CONFIRM_THRESHOLD:
        result = f138_request_reconciliation(incarnation_id=incarnation_id, source=source)
        # Clean up the counter
        with _f138_observation_lock:
            _f138_gone_counts.pop(key, None)
        return LivenessObservationResult(
            job_queued=result.created,
            confirmation_count=count,
            detail=result.detail,
        )

    return LivenessObservationResult(
        job_queued=False, confirmation_count=count, detail="awaiting_confirmation"
    )


def f138_request_reconciliation(*, incarnation_id: str, source: str) -> "JobRequestResult":
    """Insert a unique reconciliation job for an incarnation."""
    import uuid as _uuid

    from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult

    now = _utcnow()
    with SessionLocal.begin() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is None:
            return JobRequestResult(created=False, job_id=None, detail="incarnation_not_found")
        if inc.state not in ("active", "reconcile_pending"):
            return JobRequestResult(
                created=False, job_id=None, detail=f"incarnation_state={inc.state}"
            )

        # Check if job already exists
        existing = (
            db.query(OrphanReconcileJobModel).filter_by(incarnation_id=incarnation_id).one_or_none()
        )
        if existing is not None:
            return JobRequestResult(created=False, job_id=existing.id, detail="job_already_exists")

        # Mark incarnation reconcile_pending
        if inc.state == "active":
            inc.state = "reconcile_pending"

        job_id = str(_uuid.uuid4())
        job = OrphanReconcileJobModel(
            id=job_id,
            incarnation_id=incarnation_id,
            terminal_id=inc.terminal_id,
            terminal_generation=inc.terminal_generation,
            state="pending",
            attempt=0,
            gone_observed_at=now,
            source=source,
            created_at=now,
            updated_at=now,
        )
        db.add(job)

    # Signal dispatcher after DB commit
    from cli_agent_orchestrator.services.orphan_reconcile_service import orphan_reconcile_service

    orphan_reconcile_service.signal_dirty()
    return JobRequestResult(created=True, job_id=job_id, detail=None)


def f138_claim_jobs(*, limit: int, lease_duration_s: float) -> list[dict[str, str]]:
    """Claim due pending/retry_wait jobs with a lease."""
    import uuid as _uuid

    now = _utcnow()
    lease_owner = str(_uuid.uuid4())
    expires = now + __import__("datetime").timedelta(seconds=lease_duration_s)

    with SessionLocal.begin() as db:
        due_jobs = (
            db.query(OrphanReconcileJobModel)
            .filter(
                OrphanReconcileJobModel.state.in_(("pending", "retry_wait")),
                or_(
                    OrphanReconcileJobModel.next_attempt_at.is_(None),
                    OrphanReconcileJobModel.next_attempt_at <= now,
                ),
            )
            .limit(limit)
            .all()
        )
        claimed = []
        for job in due_jobs:
            job.state = "leased"
            job.lease_owner = lease_owner
            job.lease_expires_at = expires
            job.attempt = (job.attempt or 0) + 1
            job.updated_at = now
            claimed.append({"id": job.id, "incarnation_id": job.incarnation_id})
        return claimed


def f138_get_incarnation_for_job(incarnation_id: str) -> dict[str, Any] | None:
    """Fetch incarnation data needed for reconciliation."""
    with SessionLocal() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is None:
            return None
        return {
            "id": inc.id,
            "terminal_id": inc.terminal_id,
            "terminal_generation": inc.terminal_generation,
            "token": inc.token,
            "token_hash": inc.token_hash,
            "owner_uid": inc.owner_uid,
            "provider": inc.provider,
            "state": inc.state,
            "issuance_ticks": inc.issuance_ticks,
            "issuance_boot_id": inc.issuance_boot_id,
        }


def f138_get_job_attempt(job_id: str) -> int:
    """Get current attempt number for a job."""
    with SessionLocal() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        return job.attempt if job else 0


def f138_complete_job(job_id: str, state: str, *, detail: str | None = None) -> None:
    """Mark a job as succeeded."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        if job is None:
            return
        job.state = state
        job.last_result_json = detail
        job.updated_at = now
        job.lease_owner = None
        job.lease_expires_at = None


def f138_retry_job(job_id: str, delay_s: float) -> None:
    """Move job to retry_wait with a scheduled next_attempt_at."""
    import datetime as _dt

    now = _utcnow()
    with SessionLocal.begin() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        if job is None:
            return
        job.state = "retry_wait"
        job.next_attempt_at = now + _dt.timedelta(seconds=delay_s)
        job.updated_at = now
        job.lease_owner = None
        job.lease_expires_at = None


def f138_mark_attention_required(job_id: str, failure_code: str) -> None:
    """Mark job as attention_required with failure code."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        if job is None:
            return
        job.state = "attention_required"
        # F166: notified_failure_code is set by the notify helper on successful send,
        # not here — setting it here would dedup the first notification.
        job.updated_at = now
        job.lease_owner = None
        job.lease_expires_at = None


def f138_renew_lease(job_id: str, duration_s: float) -> None:
    """Renew an active lease."""
    import datetime as _dt

    now = _utcnow()
    with SessionLocal.begin() as db:
        job = db.query(OrphanReconcileJobModel).filter_by(id=job_id).one_or_none()
        if job and job.state == "leased":
            job.lease_expires_at = now + _dt.timedelta(seconds=duration_s)
            job.updated_at = now


def f138_mark_incarnation_reconciled(incarnation_id: str) -> None:
    """Mark incarnation state as reconciled."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is not None:
            inc.state = "reconciled"
            inc.reconciled_at = now


def f138_startup_recovery() -> None:
    """At startup: expire stale leases, schedule due work, sweep stale launching."""
    now = _utcnow()
    _force_queue_ids: list[tuple[str, str]] = []
    with SessionLocal.begin() as db:
        # Expire stale leased jobs (server crashed mid-execution)
        stale_leased = (
            db.query(OrphanReconcileJobModel)
            .filter(
                OrphanReconcileJobModel.state == "leased",
                OrphanReconcileJobModel.lease_expires_at < now,
            )
            .all()
        )
        for job in stale_leased:
            job.state = "pending"
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            logger.info("f138_startup_recover_lease job=%s", job.id)

        # Sweep stale 'launching' incarnations
        import datetime as _dt

        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            INCARNATION_LAUNCH_STALE_SECONDS,
        )

        stale_cutoff = now - _dt.timedelta(seconds=INCARNATION_LAUNCH_STALE_SECONDS)
        stale_launching = (
            db.query(ProcessIncarnationModel)
            .filter(
                ProcessIncarnationModel.state == "launching",
                ProcessIncarnationModel.created_at < stale_cutoff,
            )
            .all()
        )
        for inc in stale_launching:
            # D10/D24: force-queue gone windows; abandon only truly deleted terminals
            from cli_agent_orchestrator.backends.registry import get_backend

            try:
                metadata = db.query(TerminalModel).filter_by(id=inc.terminal_id).one_or_none()
                if metadata is None:
                    # D24: missing terminal row is NOT proof of safe abandon —
                    # force-queue for post-transaction reconciliation instead.
                    _force_queue_ids.append((inc.id, "startup_stale_missing_terminal"))
                    logger.info(
                        "f138_startup_stale_missing_terminal incarnation=%s terminal=%s",
                        inc.id,
                        inc.terminal_id,
                    )
                    continue
                liveness = get_backend().window_liveness(
                    metadata.tmux_session, metadata.tmux_window
                )
                if liveness == "gone":
                    # D24: collect for force-queue after transaction commits
                    _force_queue_ids.append((inc.id, "startup_stale_gone"))
                # error/inconclusive → leave unchanged
            except Exception:
                # Leave unchanged for next recovery pass
                pass

    # D24: Force-queue collected incarnations outside the main transaction
    for inc_id, src in _force_queue_ids:
        try:
            result = f138_force_reconcile_incarnation(inc_id, source=src)
            if result.outcome in ("created", "job_already_exists", "reconciled_proven"):
                logger.info(
                    "f138_startup_force_queue incarnation=%s outcome=%s (durable)",
                    inc_id,
                    result.outcome,
                )
            else:
                # non_durable_invariant / non_durable_missing — log warning, do not
                # treat as successfully enqueued
                logger.warning(
                    "f138_startup_force_queue_non_durable incarnation=%s outcome=%s detail=%s",
                    inc_id,
                    result.outcome,
                    result.detail,
                )
        except Exception:
            # DB error propagates as warning — no teardown authority granted
            logger.warning("f138_startup_force_queue_failed incarnation=%s", inc_id, exc_info=True)


# --- F138 incarnation reservation/activation helpers --------------------------


def f138_reserve_incarnation(
    *,
    terminal_id: str,
    terminal_generation: int,
    token: str,
    token_hash: str,
    owner_uid: int,
    provider: str,
    pane_pid: int | None = None,
    pane_start_ticks: int | None = None,
    issuance_ticks: int | None = None,
    issuance_boot_id: str | None = None,
) -> str:
    """Reserve a process incarnation row. Returns the incarnation ID."""
    import uuid as _uuid

    now = _utcnow()
    incarnation_id = str(_uuid.uuid4())
    with SessionLocal.begin() as db:
        inc = ProcessIncarnationModel(
            id=incarnation_id,
            terminal_id=terminal_id,
            terminal_generation=terminal_generation,
            token=token,
            token_hash=token_hash,
            owner_uid=owner_uid,
            provider=provider,
            pane_pid=pane_pid,
            pane_start_ticks=pane_start_ticks,
            issuance_ticks=issuance_ticks,
            issuance_boot_id=issuance_boot_id,
            state="launching",
            created_at=now,
        )
        db.add(inc)
    return incarnation_id


def f138_activate_incarnation(incarnation_id: str) -> bool:
    """CAS launching -> active. Returns True on success."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        inc = (
            db.query(ProcessIncarnationModel)
            .filter_by(id=incarnation_id, state="launching")
            .one_or_none()
        )
        if inc is None:
            return False
        inc.state = "active"
        inc.activated_at = now
    return True


def f138_abandon_incarnation(incarnation_id: str) -> None:
    """Mark incarnation as abandoned (launch failure)."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is not None and inc.state == "launching":
            inc.state = "abandoned"
            inc.reconciled_at = now


def f138_get_active_incarnation(terminal_id: str) -> dict[str, Any] | None:
    """Get the active incarnation for a terminal (if any)."""
    with SessionLocal() as db:
        inc = (
            db.query(ProcessIncarnationModel)
            .filter_by(terminal_id=terminal_id, state="active")
            .one_or_none()
        )
        if inc is None:
            return None
        return {
            "id": inc.id,
            "terminal_id": inc.terminal_id,
            "terminal_generation": inc.terminal_generation,
            "token_hash": inc.token_hash,
        }


def f138_update_incarnation_pane(
    incarnation_id: str, pane_pid: int, pane_start_ticks: int | None
) -> None:
    """Update diagnostic pane PID/start ticks after window creation."""
    with SessionLocal.begin() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is not None:
            inc.pane_pid = pane_pid
            inc.pane_start_ticks = pane_start_ticks


# --- F138 r7: Typed activation and force-reconciliation primitives (D21-D22) --


@dataclass(frozen=True)
class ActivationResult:
    """D21: Typed outcome of strict incarnation activation."""

    outcome: str  # "activated", "already_active", "needs_settlement", "missing"


def f138_strict_activate(incarnation_id: str) -> ActivationResult:
    """D21: Exact-row activation. Only launching→active transition is valid.

    Returns typed ActivationResult:
      - activated: row was launching, now active+activated_at set
      - already_active: row is already active (idempotent ok)
      - needs_settlement: row is in reconcile_pending/reconciled/abandoned
      - missing: no row with this ID exists
    """
    now = _utcnow()
    with SessionLocal.begin() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is None:
            return ActivationResult(outcome="missing")
        if inc.state == "active":
            return ActivationResult(outcome="already_active")
        if inc.state != "launching":
            return ActivationResult(outcome="needs_settlement")
        inc.state = "active"
        inc.activated_at = now
    return ActivationResult(outcome="activated")


@dataclass(frozen=True)
class ForceReconcileResult:
    """D22: Typed outcome of force-reconciliation primitive."""

    outcome: str  # "created", "reconciled_proven", "job_already_exists",
    #               "non_durable_invariant", "non_durable_missing"
    job_id: str | None = None
    detail: str | None = None


def f138_force_reconcile_incarnation(incarnation_id: str, source: str) -> ForceReconcileResult:
    """D22: Atomic force-reconciliation primitive.

    In one transaction: reads exact row, inspects unique job, returns typed result.
    Order: reconciled row + succeeded job → reconciled_proven |
           reconciled row WITHOUT succeeded job → non_durable_invariant |
           succeeded job (repairs row) → reconciled_proven |
           known due job states (pending/leased/retry_wait) → job_already_exists |
           attention_required job → reset to pending, attempt=0, clear notification
               counters (F166 D6 re-arm; a genuine new event re-arms cleanup) |
           unknown job state → non_durable_invariant |
           no existing job → normalize state + create.
    DB errors propagate. Wakes dispatcher after commit when a job is created or pending.
    """
    import uuid as _uuid

    now = _utcnow()
    wake_dispatcher = False
    result: ForceReconcileResult

    with SessionLocal.begin() as db:
        inc = db.query(ProcessIncarnationModel).filter_by(id=incarnation_id).one_or_none()
        if inc is None:
            return ForceReconcileResult(
                outcome="non_durable_missing",
                detail="incarnation_not_found",
            )

        # Check existing job first (needed for reconciled-row classification)
        existing = (
            db.query(OrphanReconcileJobModel).filter_by(incarnation_id=incarnation_id).one_or_none()
        )

        # Already fully reconciled — but only proven if succeeded job exists
        if inc.state == "reconciled":
            if existing is not None and existing.state == "succeeded":
                return ForceReconcileResult(
                    outcome="reconciled_proven",
                    job_id=existing.id,
                    detail="reconciled_with_succeeded_job",
                )
            # Reconciled without succeeded job = invariant violation
            return ForceReconcileResult(
                outcome="non_durable_invariant",
                detail="reconciled_without_succeeded_job",
            )

        if existing is not None:
            if existing.state == "succeeded":
                # Repair: job succeeded but row not updated (crash window)
                inc.state = "reconciled"
                inc.reconciled_at = now
                return ForceReconcileResult(
                    outcome="reconciled_proven",
                    job_id=existing.id,
                    detail="repaired_from_succeeded_job",
                )
            if existing.state in ("pending", "leased", "retry_wait"):
                # Job already in progress — normalize incarnation state
                if inc.state in ("active", "launching", "abandoned"):
                    inc.state = "reconcile_pending"
                wake_dispatcher = True
                result = ForceReconcileResult(
                    outcome="job_already_exists",
                    job_id=existing.id,
                    detail=f"state={existing.state}",
                )
            elif existing.state == "attention_required":
                # F166 D6: Re-arm — reset to pending with cleared counters.
                existing.state = "pending"
                existing.attempt = 0
                existing.notified_failure_code = None
                existing.notify_count = None
                existing.next_attempt_at = None
                existing.lease_owner = None
                existing.lease_expires_at = None
                existing.updated_at = now
                if inc.state in ("active", "launching", "abandoned"):
                    inc.state = "reconcile_pending"
                wake_dispatcher = True
                result = ForceReconcileResult(
                    outcome="created",
                    job_id=existing.id,
                    detail="reset_from_attention_required",
                )
            else:
                # Unknown job state = non-durable invariant
                result = ForceReconcileResult(
                    outcome="non_durable_invariant",
                    job_id=existing.id,
                    detail=f"unknown_job_state={existing.state}",
                )
        else:
            # No existing job — normalize incarnation state + create job
            if inc.state in ("active", "launching", "abandoned", "reconcile_pending"):
                inc.state = "reconcile_pending"

            job_id = str(_uuid.uuid4())
            job = OrphanReconcileJobModel(
                id=job_id,
                incarnation_id=incarnation_id,
                terminal_id=inc.terminal_id,
                terminal_generation=inc.terminal_generation,
                state="pending",
                attempt=0,
                gone_observed_at=now,
                source=source,
                created_at=now,
                updated_at=now,
            )
            db.add(job)
            wake_dispatcher = True
            result = ForceReconcileResult(outcome="created", job_id=job_id)

    if wake_dispatcher:
        try:
            from cli_agent_orchestrator.services.orphan_reconcile_service import (
                orphan_reconcile_service,
            )

            orphan_reconcile_service.signal_dirty()
        except Exception:
            pass

    return result


def f138_emit_attention_message(
    terminal_id: str, message: str, *, supervisor_id: str | None = None
) -> bool:
    """D23: Shared DB-only one-shot attention notification.

    Thread-safe, no async, no locks held. Returns True if message was persisted.
    Failure is logged but never raises — callers must not depend on this for safety.
    """
    try:
        # Find supervisor from same session if not provided
        if supervisor_id is None:
            with SessionLocal() as db:
                terminal = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
                if terminal is not None:
                    session_name = terminal.tmux_session
                    # Find supervisor in same session
                    candidates = (
                        db.query(TerminalModel)
                        .filter(
                            TerminalModel.tmux_session == session_name,
                            TerminalModel.id != terminal_id,
                        )
                        .all()
                    )
                    for c in candidates:
                        if c.agent_profile and "supervisor" in (c.agent_profile or ""):
                            supervisor_id = c.id
                            break
                    if supervisor_id is None and candidates:
                        supervisor_id = candidates[0].id
        if supervisor_id is None:
            logger.warning(
                "f138_attention_no_supervisor terminal=%s msg=%s", terminal_id, message[:80]
            )
            return False
        create_inbox_message(terminal_id, supervisor_id, message)
        return True
    except Exception:
        logger.warning("f138_attention_failed terminal=%s", terminal_id, exc_info=True)
        return False


def f138_get_incarnation_by_terminal_generation(
    terminal_id: str, terminal_generation: int
) -> dict[str, Any] | None:
    """D11/D24: Resolve incarnation by terminal_id + exact lifecycle generation."""
    with SessionLocal() as db:
        inc = (
            db.query(ProcessIncarnationModel)
            .filter_by(
                terminal_id=terminal_id,
                terminal_generation=terminal_generation,
            )
            .one_or_none()
        )
        if inc is None:
            return None
        return {
            "id": inc.id,
            "terminal_id": inc.terminal_id,
            "terminal_generation": inc.terminal_generation,
            "state": inc.state,
            "token_hash": inc.token_hash,
        }
