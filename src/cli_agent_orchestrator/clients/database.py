"""Minimal database client with only terminal metadata."""

import logging
import os
import json
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    create_engine,
    event,
    exists,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError

from cli_agent_orchestrator.constants import DATABASE_URL, DB_DIR, DEFAULT_PROVIDER
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import RecoveryState, TerminalStatus

logger = logging.getLogger(__name__)

Base: Any = declarative_base()
_ImmediateResult = TypeVar("_ImmediateResult")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TerminalModel(Base):
    """SQLAlchemy model for terminal metadata only."""

    __tablename__ = "terminals"

    id = Column(String, primary_key=True)  # "abc123ef"
    tmux_session = Column(String, nullable=False)  # "cao-session-name"
    tmux_window = Column(String, nullable=False)  # "window-name"
    provider = Column(String, nullable=False)  # "kiro_cli", "claude_code"
    agent_profile = Column(String)  # "developer", "reviewer" (optional)
    allowed_tools = Column(String, nullable=True)  # JSON-encoded list of CAO tool names
    shell_command = Column(String, nullable=True)  # shell process name captured before kiro launch
    caller_id = Column(String, nullable=True)  # terminal that created this one (callback target)
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
    last_active = Column(DateTime, default=datetime.now)
    __table_args__ = (
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
        Index("ix_transcript_bindings_terminal_received",
              "terminal_id", "received_at", "id"),
    )


class InboxModel(Base):
    """SQLAlchemy model for inbox messages."""

    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sender_id = Column(String, nullable=False)
    receiver_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    orchestration_type = Column(
        String,
        nullable=False,
        default=OrchestrationType.SEND_MESSAGE.value,
        server_default=OrchestrationType.SEND_MESSAGE.value,
    )
    status = Column(String, nullable=False)  # MessageStatus enum value
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


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
            "receiver_terminal_id", "payload_hash", "reason",
            unique=True,
            sqlite_where=(outcome == "deferred"),
        ),
    )


class InboxDeliveryAttemptMemberModel(Base):
    __tablename__ = "inbox_delivery_attempt_member"
    attempt_uuid = Column(String, primary_key=True)
    message_id = Column(Integer, primary_key=True, index=True)
    position = Column(Integer, nullable=False)


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


class FlowModel(Base):
    """SQLAlchemy model for flow metadata."""

    __tablename__ = "flows"

    name = Column(String, primary_key=True)
    file_path = Column(String, nullable=False)
    schedule = Column(String, nullable=False)
    agent_profile = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    script = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    enabled = Column(Boolean, default=True)


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
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

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
    _migrate_transcript_bindings_inode_nullable()
    _migrate_provider_sessions_status()
    _migrate_provider_sessions_session_name()
    _migrate_provider_sessions_kind()
    _restrict_db_file_permissions()
    _migrate_terminals_schema()
    _migrate_inbox_orchestration_type()
    _migrate_inbox_failure_reason()
    _migrate_memory_indexes()
    _migrate_add_access_count()
    _migrate_add_last_compiled_at()
    _migrate_add_related_keys()
    _migrate_workflow_index()
    _migrate_workflow_run()
    _migrate_workflow_run_step()


def _migrate_provider_sessions_status() -> None:
    """Rebuild legacy provider_sessions tables so ``retired`` is valid."""
    from sqlalchemy import text

    with engine.begin() as connection:
        table_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='provider_sessions'"
            )
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
                "dirty_hashes TEXT DEFAULT '{}' NOT NULL, summary TEXT, status TEXT NOT NULL, "
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
                "summary, status, kind, source_terminal_id, session_name, created_at, updated_at) "
                "SELECT id, name, provider, session_uuid, cwd, agent_profile, git_sha, "
                "dirty_hashes, summary, status, 'base', source_terminal_id, NULL, created_at, updated_at "
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
                    "ALTER TABLE provider_sessions ADD COLUMN kind TEXT NOT NULL "
                    "DEFAULT 'base'"
                )
            )


def _migrate_transcript_bindings_inode_nullable() -> None:
    """Rebuild the r4 table so startup bindings may defer inode discovery."""
    from sqlalchemy import text

    with engine.begin() as connection:
        columns = connection.execute(
            text("PRAGMA table_info(transcript_bindings)")
        ).mappings().all()
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
        connection.execute(text("ALTER TABLE transcript_bindings_new RENAME TO transcript_bindings"))
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


def _migrate_workflow_index() -> None:
    """Create the derived ``workflow_index`` table if missing (issue #312, N2).

    The table is a **derived, non-authoritative** projection of the workflow
    spec YAML files on disk (B2-BR-2): it can be dropped and rebuilt
    byte-identically from the files alone (``rebuild_index_from_files``). It
    carries no run/execution state — runs and per-step state are N5/N6.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors the existing ``_migrate_memory_indexes`` pattern. Failure is logged
    at debug and never propagated (a missing index table is recoverable: the
    next ``list`` rebuilds it).
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_index ("
                "name TEXT PRIMARY KEY, "
                "source_path TEXT NOT NULL, "
                "mode TEXT NOT NULL, "
                "step_count INTEGER NOT NULL, "
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
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_run_step migration skipped: {e}")


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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='terminals'"
        ).fetchone()
        table_sql = table_sql_row[0] if table_sql_row else ""
        init_columns = {
            "init_state", "init_started_at", "init_owner_epoch",
            "init_failure_token", "init_deadline_s",
        }
        has_token_unique = any(
            bool(row[2]) and any(
                detail[2] == "init_failure_token"
                for detail in conn.execute(f"PRAGMA index_info('{row[1]}')")
            )
            for row in conn.execute("PRAGMA index_list('terminals')")
        )
        trigger_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
            "name='terminals_init_failure_token_immutable'"
        ).fetchone() is not None
        schema_current = (
            init_columns.issubset(columns)
            and "init_state IN" in table_sql
            and "init_deadline_s >= 1.0" in table_sql
            and has_token_unique
        )
        if not columns:
            return
        if schema_current:
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
            return
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE terminals RENAME TO terminals_wpm4a_legacy")
        conn.execute(
            "CREATE TABLE terminals ("
            "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, tmux_window TEXT NOT NULL, "
            "provider TEXT NOT NULL, agent_profile TEXT, allowed_tools TEXT, "
            "shell_command TEXT, caller_id TEXT, provider_session_id TEXT, "
            "recovery_state TEXT, recovery_error TEXT, recovery_updated_at DATETIME, "
            "fallback_terminal_id TEXT, "
            "init_state TEXT NOT NULL DEFAULT 'ready' "
            "CHECK (init_state IN ('init_pending','ready','init_failed_notified',"
            "'init_failed_caller_gone')), "
            "init_started_at DATETIME, init_owner_epoch TEXT, "
            "init_failure_token TEXT UNIQUE, init_deadline_s REAL, "
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
        legacy_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(terminals_wpm4a_legacy)"
        )}
        destination = [
            "id", "tmux_session", "tmux_window", "provider", "agent_profile",
            "allowed_tools", "shell_command", "caller_id", "provider_session_id",
            "recovery_state", "recovery_error", "recovery_updated_at",
            "fallback_terminal_id", "last_active",
            "init_state", "init_started_at", "init_owner_epoch",
            "init_failure_token", "init_deadline_s",
        ]
        copied = [name for name in destination if name in legacy_columns]
        conn.execute(
            f"INSERT INTO terminals ({','.join(copied)}) "
            f"SELECT {','.join(copied)} FROM terminals_wpm4a_legacy"
        )
        conn.execute("DROP TABLE terminals_wpm4a_legacy")
        conn.execute(
            "CREATE TRIGGER terminals_init_failure_token_immutable "
            "BEFORE UPDATE OF init_failure_token ON terminals "
            "WHEN OLD.init_failure_token IS NOT NULL AND "
            "NEW.init_failure_token IS NOT OLD.init_failure_token "
            "BEGIN SELECT RAISE(ABORT, 'init_failure_token_immutable'); END"
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


def create_terminal(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    shell_command: Optional[str] = None,
    caller_id: Optional[str] = None,
    provider_session_id: Optional[str] = None,
    init_state: str = "ready",
    init_started_at: Optional[datetime] = None,
    init_owner_epoch: Optional[str] = None,
    init_deadline_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Create terminal metadata record."""
    import json as _json

    with SessionLocal() as db:
        terminal = TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
            shell_command=shell_command,
            caller_id=caller_id,
            provider_session_id=provider_session_id,
            init_state=init_state,
            init_started_at=init_started_at,
            init_owner_epoch=init_owner_epoch,
            init_deadline_s=init_deadline_s,
        )
        db.add(terminal)
        db.commit()
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "allowed_tools": allowed_tools,
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
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
        }


class WarmIntentPublishError(RuntimeError):
    """Terminal and warm-intent publication could not settle atomically."""


def create_terminal_with_warm_intent(
    *, terminal_id: str, tmux_session: str, tmux_window: str, provider: str,
    agent_profile: Optional[str], allowed_tools: Optional[List[str]],
    caller_id: Optional[str], parent_base_name: Optional[str],
    fork_mode: Optional[str], cas_hook=None, init_state: str = "ready",
    init_started_at: Optional[datetime] = None,
    init_owner_epoch: Optional[str] = None,
    init_deadline_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Publish terminal metadata and a fork-only warm intent together."""
    import json as _json
    import uuid

    with SessionLocal.begin() as db:
        db.add(TerminalModel(
            id=terminal_id, tmux_session=tmux_session, tmux_window=tmux_window,
            provider=provider, agent_profile=agent_profile,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
            caller_id=caller_id,
            init_state=init_state, init_started_at=init_started_at,
            init_owner_epoch=init_owner_epoch, init_deadline_s=init_deadline_s,
        ))
        if fork_mode == "fork" and parent_base_name and agent_profile:
            claimed = False
            for attempt in range(3):
                dead = (
                    db.query(WarmIntentModel)
                    .outerjoin(TerminalModel, TerminalModel.id == WarmIntentModel.worker_terminal_id)
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
                    db.add(WarmIntentModel(
                        intent_id=str(uuid.uuid4()), worker_terminal_id=terminal_id,
                        session_name=tmux_session, worker_profile=agent_profile,
                        parent_base_name=parent_base_name, provider=provider,
                        created_at=_utcnow(),
                    ))
                    claimed = True
                    break
                old_id = dead.worker_terminal_id
                if cas_hook and cas_hook(attempt, old_id, db) is False:
                    db.expire_all()
                    continue
                changed = db.query(WarmIntentModel).filter(
                    WarmIntentModel.intent_id == dead.intent_id,
                    WarmIntentModel.worker_terminal_id == old_id,
                    ~db.query(TerminalModel).filter(TerminalModel.id == old_id).exists(),
                ).update({
                    "worker_terminal_id": terminal_id,
                    "replaces_worker_terminal_id": old_id,
                    "created_at": _utcnow(),
                }, synchronize_session=False)
                if changed:
                    claimed = True
                    break
                db.expire_all()
            if not claimed:
                raise WarmIntentPublishError("db_publish_failed")
        db.flush()
        return {"id": terminal_id, "tmux_session": tmux_session, "tmux_window": tmux_window}


def get_terminal_metadata(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Get terminal metadata by ID."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            logger.warning(f"Terminal metadata not found for terminal_id: {terminal_id}")
            return None
        logger.debug(
            f"Retrieved terminal metadata for {terminal_id}: provider={terminal.provider}, session={terminal.tmux_session}"
        )
        allowed_tools = _json.loads(terminal.allowed_tools) if terminal.allowed_tools else None
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "allowed_tools": allowed_tools,
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
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
            "last_active": terminal.last_active,
        }


def list_terminals_by_session(tmux_session: str) -> List[Dict[str, Any]]:
    """List all terminals in a tmux session."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "allowed_tools": (
                    __import__("json").loads(t.allowed_tools)
                    if isinstance(t.allowed_tools, str) and t.allowed_tools else None
                ),
                "shell_command": t.shell_command,
                "caller_id": t.caller_id,
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
                "last_active": t.last_active,
            }
            for t in terminals
        ]


def update_last_active(terminal_id: str) -> bool:
    """Update last active timestamp."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.last_active = datetime.now()
            db.commit()
            return True
        return False


def update_terminal_shell_command(terminal_id: str, shell_command: str) -> bool:
    """Update the shell_command baseline for a terminal."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.shell_command = shell_command
            db.commit()
            return True
        return False


def update_terminal_provider_session_id_if_null(
    terminal_id: str, session_uuid: str
) -> str | None:
    """Claim an unset provider session id and return the persisted winner."""
    with SessionLocal.begin() as db:
        db.query(TerminalModel).filter(
            TerminalModel.id == terminal_id,
            TerminalModel.provider_session_id.is_(None),
        ).update({TerminalModel.provider_session_id: session_uuid}, synchronize_session=False)
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        return terminal.provider_session_id if terminal else None


def update_terminal_provider_session_id(terminal_id: str, session_uuid: str) -> bool:
    """Explicitly set a provider session id (base registration/allocated UUID paths)."""
    with SessionLocal.begin() as db:
        changed = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).update(
            {TerminalModel.provider_session_id: session_uuid}, synchronize_session=False)
        return changed > 0


def update_terminal_runtime_identity(
    terminal_id: str, session_uuid: str, shell_command: str | None,
    *, supersede_other_claims: bool = False, require_published_uuid: bool = False,
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
        return True


def settle_terminal_rebound(
    terminal_id: str, session_uuid: str, shell_command: str,
) -> bool:
    """Atomically persist proven runtime identity and the healthy projection."""
    with SessionLocal.begin() as db:
        changed = db.query(TerminalModel).filter_by(id=terminal_id).update(
            {
                "provider_session_id": session_uuid,
                "shell_command": shell_command,
                "recovery_state": "rebound",
                "recovery_error": None,
                "recovery_updated_at": _utcnow(),
            },
            synchronize_session=False,
        )
        return changed > 0


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
        return db.query(TerminalModel).filter_by(id=terminal_id).update(
            values, synchronize_session=False
        ) > 0


def quarantine_terminal_owner(
    terminal_id: str, session_uuid: str | None, error: str,
) -> str:
    """Atomically retain attempted native ownership and quarantine projection."""
    with SessionLocal.begin() as db:
        row = db.query(TerminalModel).filter_by(id=terminal_id).first()
        if row is None:
            return ""
        association = "skipped_existing_owner"
        if row.provider_session_id is None and session_uuid:
            associated = db.query(TerminalModel).filter(
                TerminalModel.id == terminal_id,
                TerminalModel.provider_session_id.is_(None),
                ~db.query(TerminalModel.id).filter(
                    TerminalModel.provider_session_id == session_uuid,
                    TerminalModel.id != terminal_id,
                ).exists(),
            ).update({"provider_session_id": session_uuid}, synchronize_session=False)
            association = "associated" if associated == 1 else "skipped_existing_owner"
        row.recovery_state = "rebind_failed"
        row.recovery_error = error[:2048]
        row.recovery_updated_at = _utcnow()
        return association


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
        changed = db.query(InboxModel).filter(
            InboxModel.receiver_id == old_terminal_id,
            InboxModel.status == MessageStatus.PENDING.value,
        ).update({"receiver_id": new_terminal_id}, synchronize_session=False)
        old.fallback_terminal_id = new_terminal_id
        old.recovery_state = "fallback_ready"
        old.recovery_error = None
        old.recovery_updated_at = _utcnow()
        db.query(TerminalModel).filter(
            TerminalModel.provider_session_id == new.provider_session_id,
            TerminalModel.id != new_terminal_id,
        ).update({"provider_session_id": None}, synchronize_session=False)
        return changed


def has_unsettled_delivery_attempt(terminal_id: str) -> bool:
    with SessionLocal() as db:
        return db.query(InboxDeliveryAttemptModel).filter_by(
            receiver_terminal_id=terminal_id, settled_at=None
        ).first() is not None


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


def register_provider_session(**values: Any) -> Dict[str, Any]:
    """Atomically supersede a ready name and register its replacement."""
    if values.get("name") == "cold":
        raise ValueError("base_name_reserved:cold")
    if values.get("kind", "base") not in {"base", "anchor"}:
        raise ValueError("invalid_provider_session_kind")
    with SessionLocal() as db:
        now = _utcnow()
        db.query(ProviderSessionModel).filter(
            ProviderSessionModel.name == values["name"],
            ProviderSessionModel.status == "ready",
        ).update({"status": "superseded", "updated_at": now})
        row = ProviderSessionModel(**values, status="ready", created_at=now, updated_at=now)
        db.add(row)
        db.commit()
        db.refresh(row)
        return provider_session_to_dict(row)


def get_provider_session_history(name: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = (db.query(ProviderSessionModel).filter_by(name=name)
               .order_by(ProviderSessionModel.updated_at.desc(), ProviderSessionModel.id.desc())
               .first())
        return provider_session_to_dict(row) if row else None


def list_ready_provider_sessions_for_session(session_name: str) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(ProviderSessionModel).filter_by(
            status="ready", session_name=session_name).order_by(ProviderSessionModel.name).all()
        return [provider_session_to_dict(row) for row in rows]


def delete_terminal_and_warm_intent(
    terminal_id: str, *, preserve_warm_intent: bool = False,
) -> Dict[str, bool]:
    """Delete the terminal and, unless retained, its warm intent atomically."""
    with SessionLocal.begin() as db:
        intent_deleted = False
        if not preserve_warm_intent:
            intent_deleted = (
                db.query(WarmIntentModel)
                .filter_by(worker_terminal_id=terminal_id)
                .delete() > 0
            )
        terminal_deleted = (
            db.query(TerminalModel).filter_by(id=terminal_id).delete() > 0
        )
        return {
            "terminal_deleted": terminal_deleted,
            "intent_deleted": intent_deleted,
        }


def list_warm_intents(session_name: str) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(WarmIntentModel).filter_by(session_name=session_name).order_by(
            WarmIntentModel.created_at, WarmIntentModel.intent_id).all()
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows]


def delete_warm_intents_for_session(session_name: str) -> int:
    with SessionLocal.begin() as db:
        return db.query(WarmIntentModel).filter_by(session_name=session_name).delete()


def increment_session_epoch(session_name: str) -> Dict[str, Any]:
    from sqlalchemy.dialects.sqlite import insert
    now = _utcnow()
    with SessionLocal.begin() as db:
        statement = insert(SessionEpochModel).values(
            session_name=session_name, count=1, last_epoch_at=now,
        ).on_conflict_do_update(
            index_elements=[SessionEpochModel.session_name],
            set_={"count": SessionEpochModel.count + 1, "last_epoch_at": now},
        ).returning(SessionEpochModel.count, SessionEpochModel.last_epoch_at)
        count, last_epoch_at = db.execute(statement).one()
        return {"count": count, "last_epoch_at": last_epoch_at}


def get_session_epoch(session_name: str) -> Optional[Dict[str, Any]]:
    try:
        with SessionLocal() as db:
            row = db.query(SessionEpochModel).filter_by(session_name=session_name).first()
            return ({"count": row.count, "last_epoch_at": row.last_epoch_at} if row else None)
    except Exception as exc:
        if "no such table: session_epochs" in str(exc):
            return None
        raise


def delete_session_epoch(session_name: str) -> bool:
    with SessionLocal.begin() as db:
        return db.query(SessionEpochModel).filter_by(session_name=session_name).delete() > 0


def retire_provider_session(name: str) -> Optional[Dict[str, Any]]:
    """Atomically retire the current ready registration for ``name``."""
    with SessionLocal() as db:
        row = db.query(ProviderSessionModel).filter_by(name=name, status="ready").first()
        if row is None:
            return None
        row.status = "retired"
        row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)
        return provider_session_to_dict(row)


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
) -> Optional[Dict[str, Any]]:
    """CAS-refresh the snapshot for the same still-ready registry row."""
    with SessionLocal() as db:
        row = db.query(ProviderSessionModel).filter_by(id=row_id, status="ready").first()
        if row is None:
            return None
        row.git_sha = git_sha
        row.dirty_hashes = dirty_hashes
        row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)
        return provider_session_to_dict(row)


def get_ready_provider_session_by_source_terminal(
    terminal_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the ready base owned by ``terminal_id``, if any."""
    with SessionLocal() as db:
        row = db.query(ProviderSessionModel).filter_by(
            source_terminal_id=terminal_id, status="ready"
        ).first()
        return provider_session_to_dict(row) if row else None


def get_provider_session_by_uuid(session_uuid: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = (db.query(ProviderSessionModel).filter_by(session_uuid=session_uuid)
               .order_by((ProviderSessionModel.status == "ready").desc(),
                         ProviderSessionModel.updated_at.desc(), ProviderSessionModel.id.desc())
               .first())
        if row is None or row.status == "retired":
            return None
        return provider_session_to_dict(row)


def list_ready_provider_sessions() -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        return [provider_session_to_dict(r) for r in db.query(ProviderSessionModel)
                .filter_by(status="ready").order_by(ProviderSessionModel.name).all()]


def list_terminals_by_provider_session_id(session_uuid: str) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(TerminalModel).filter_by(provider_session_id=session_uuid).all()
        return [{"id": r.id, "tmux_session": r.tmux_session, "tmux_window": r.tmux_window,
                 "provider": r.provider} for r in rows]


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
            changed = db.query(TerminalModel).filter(
                TerminalModel.id == terminal_id,
                TerminalModel.init_state == "init_pending",
            ).update({
                "init_state": "ready",
                "init_owner_epoch": None,
                "init_failure_token": None,
            }, synchronize_session=False) == 1
            if should_commit is not None and not should_commit():
                if progress_fence is not None:
                    progress_fence(None, 0)
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
                progress_fence(None, 0)
            db.rollback()
            return False
        except Exception as exc:
            decided = commit_is_decided is not None and commit_is_decided()
            abandoned = should_commit is not None and not should_commit()
            if decided:
                logger.critical(
                    "ready_commit_invariant_breach terminal=%s", terminal_id,
                    exc_info=True,
                )
            db.info.pop(_READY_COMMIT_CALLBACK, None)
            if progress_fence is not None:
                if decided:
                    try:
                        progress_fence(None, 0)
                    except Exception:
                        logger.error(
                            "ready_commit_fence_cleanup_failed terminal=%s",
                            terminal_id,
                            exc_info=True,
                        )
                    progress_fence = None
                else:
                    progress_fence(None, 0)
            if decided:
                try:
                    db.rollback()
                except Exception:
                    logger.error(
                        "ready_commit_rollback_failed terminal=%s", terminal_id,
                        exc_info=True,
                    )
                raise ReadyCommitInvariantBreach(
                    "ready_commit_failed_after_decision"
                ) from exc
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
                progress_fence(None, 0)
            db.info.pop(_READY_COMMIT_CALLBACK, None)
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
            try:
                db.execute(text("BEGIN IMMEDIATE"))
                row = db.query(TerminalModel).filter(
                    TerminalModel.id == terminal_id,
                    TerminalModel.init_state == "init_pending",
                ).first()
                if row is None:
                    existing = db.query(TerminalModel).filter_by(id=terminal_id).first()
                    db.rollback()
                    return {
                        "status": "row_missing" if existing is None else "already_claimed",
                        "init_state": existing.init_state if existing is not None else None,
                        "token": existing.init_failure_token if existing is not None else None,
                    }
                receiver_exists = bool(caller_id) and db.query(TerminalModel.id).filter(
                    TerminalModel.id == caller_id
                ).first() is not None
                row.init_failure_token = failure_token
                if receiver_exists:
                    row.init_state = "init_failed_notified"
                    db.add(InboxModel(
                        sender_id=terminal_id,
                        receiver_id=cast(str, caller_id),
                        message=notice,
                        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                        status=MessageStatus.PENDING.value,
                    ))
                    status = "claimed_notified"
                else:
                    row.init_state = "init_failed_caller_gone"
                    status = "claimed_caller_gone"
                db.flush()
                db.commit()
                return {"status": status, "init_state": row.init_state, "token": failure_token}
            except OperationalError as exc:
                db.rollback()
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt + 1 >= busy_attempts:
                    raise RuntimeError("deferred_init_claim_busy_exhausted") from exc
            except Exception:
                db.rollback()
                raise
        time.sleep(busy_delay_s)
    raise RuntimeError("deferred_init_claim_busy_exhausted")


def list_deferred_init_recovery_rows(current_owner_epoch: str) -> List[Dict[str, Any]]:
    """Return only durable init rows owned by the H5 startup sweep."""
    with SessionLocal() as db:
        rows = db.query(TerminalModel).filter(
            ((TerminalModel.init_state == "init_pending") &
             ((TerminalModel.init_owner_epoch != current_owner_epoch) |
              TerminalModel.init_owner_epoch.is_(None))) |
            TerminalModel.init_state.in_((
                "init_failed_notified", "init_failed_caller_gone"
            ))
        ).all()
        return [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in rows
        ]


def begin_teardown_intent(workspace_id: str, session_name: str) -> Dict[str, Any]:
    """Create or supersede the single active close authority for a workspace."""
    now = _utcnow()
    with SessionLocal.begin() as db:
        row = db.get(TeardownIntentModel, workspace_id)
        if row is None:
            row = TeardownIntentModel(
                workspace_id=workspace_id, session_name=session_name,
                created_at=now, state="issuing", generation=1,
            )
            db.add(row)
        else:
            row.session_name = session_name
            row.created_at = now
            row.state = "issuing"
            row.generation += 1
        db.flush()
        return {"workspace_id": workspace_id, "session_name": session_name,
                "state": row.state, "generation": row.generation,
                "created_at": row.created_at}


def settle_teardown_intent(workspace_id: str, generation: int, *, issued: bool) -> bool:
    with SessionLocal.begin() as db:
        return db.query(TeardownIntentModel).filter(
            TeardownIntentModel.workspace_id == workspace_id,
            TeardownIntentModel.generation == generation,
            TeardownIntentModel.state == "issuing",
        ).update({"state": "issued_ok" if issued else "void"},
                 synchronize_session=False) == 1


def get_teardown_intent(workspace_id: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = db.get(TeardownIntentModel, workspace_id)
        if row is None:
            return None
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def consume_current_teardown_intent(
    workspace_id: str, *, ttl_s: float, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Consume only the current issued, unexpired generation exactly once."""
    cutoff = (now or _utcnow()) - timedelta(seconds=ttl_s)
    with SessionLocal.begin() as db:
        row = db.query(TeardownIntentModel).filter(
            TeardownIntentModel.workspace_id == workspace_id,
            TeardownIntentModel.state == "issued_ok",
            TeardownIntentModel.created_at >= cutoff,
        ).first()
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
            db.add(WorkspaceMapModel(
                workspace_id=workspace_id, session_name=session_name,
                active=True, updated_at=_utcnow(),
            ))
        else:
            row.session_name = session_name
            row.active = True
            row.updated_at = _utcnow()


def resolve_workspace_mapping(workspace_id: str) -> Optional[str]:
    with SessionLocal() as db:
        row = db.query(WorkspaceMapModel).filter_by(
            workspace_id=workspace_id, active=True
        ).first()
        return cast(Optional[str], row.session_name) if row else None


def current_workspace_for_session(session_name: str) -> Optional[str]:
    with SessionLocal() as db:
        row = db.query(WorkspaceMapModel).filter_by(
            session_name=session_name, active=True
        ).order_by(WorkspaceMapModel.updated_at.desc()).first()
        return cast(Optional[str], row.workspace_id) if row else None


def retire_workspace_mapping(workspace_id: str) -> bool:
    with SessionLocal.begin() as db:
        return db.query(WorkspaceMapModel).filter_by(
            workspace_id=workspace_id, active=True
        ).update({"active": False, "updated_at": _utcnow()},
                 synchronize_session=False) == 1


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

    ``created_at`` is stored local-naive (``InboxModel.created_at`` defaults to
    ``datetime.now``), so the cutoff uses ``datetime.now()`` to match — the same
    convention as the retention query in ``cleanup_service.cleanup_old_data``.
    """
    cutoff = datetime.now() - timedelta(seconds=min_age_seconds)
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


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal metadata and its warm intent through the universal seam."""
    return delete_terminal_and_warm_intent(
        terminal_id, preserve_warm_intent=False,
    )["terminal_deleted"]


def delete_terminals_by_session(tmux_session: str) -> int:
    """Delete all session terminals and their warm intents through the universal seam."""
    with SessionLocal() as db:
        terminal_ids = [
            terminal_id for terminal_id, in
            db.query(TerminalModel.id).filter(TerminalModel.tmux_session == tmux_session).all()
        ]
    return sum(
        delete_terminal_and_warm_intent(
            terminal_id, preserve_warm_intent=False,
        )["terminal_deleted"]
        for terminal_id in terminal_ids
    )


def create_inbox_message(
    sender_id: str,
    receiver_id: str,
    message: str,
    orchestration_type: OrchestrationType = OrchestrationType.SEND_MESSAGE,
) -> InboxMessage:
    """Create inbox message with status=MessageStatus.PENDING.

    Raises:
        ValueError: If the receiver terminal does not exist.
    """
    with SessionLocal() as db:
        if not db.query(TerminalModel).filter(TerminalModel.id == receiver_id).first():
            raise ValueError(f"Terminal '{receiver_id}' not found")
        inbox_msg = InboxModel(
            sender_id=sender_id,
            receiver_id=receiver_id,
            message=message,
            orchestration_type=orchestration_type.value,
            status=MessageStatus.PENDING.value,
        )
        db.add(inbox_msg)
        db.commit()
        db.refresh(inbox_msg)
        return InboxMessage(
            id=inbox_msg.id,
            sender_id=inbox_msg.sender_id,
            receiver_id=inbox_msg.receiver_id,
            message=inbox_msg.message,
            orchestration_type=OrchestrationType(inbox_msg.orchestration_type),
            status=MessageStatus(inbox_msg.status),
            created_at=inbox_msg.created_at,
        )


def get_pending_messages(
    receiver_id: str, limit: int = 1, excluded_message_ids: set[int] | None = None,
) -> List[InboxMessage]:
    """Get pending messages ordered by created_at ASC (oldest first)."""
    excluded = set(excluded_message_ids or ())
    with SessionLocal() as db:
        query = db.query(InboxModel).filter(
            InboxModel.receiver_id == receiver_id,
            InboxModel.status == MessageStatus.PENDING.value,
        )
        if excluded:
            query = query.filter(~InboxModel.id.in_(excluded))
        rows = query.order_by(InboxModel.created_at.asc(), InboxModel.id.asc()).limit(limit).all()
        return [InboxMessage(
            id=row.id, sender_id=row.sender_id, receiver_id=row.receiver_id,
            message=row.message, orchestration_type=OrchestrationType(row.orchestration_type),
            status=MessageStatus(row.status), created_at=row.created_at,
        ) for row in rows]


def get_pending_messages_by_ids(receiver_id: str, message_ids: list[int]) -> List[InboxMessage]:
    ids = sorted(set(message_ids))
    if not ids:
        return []
    with SessionLocal() as db:
        rows = db.query(InboxModel).filter(
            InboxModel.receiver_id == receiver_id, InboxModel.id.in_(ids),
            InboxModel.status == MessageStatus.PENDING.value,
        ).order_by(InboxModel.created_at, InboxModel.id).all()
        return [InboxMessage(
            id=row.id, sender_id=row.sender_id, receiver_id=row.receiver_id,
            message=row.message, orchestration_type=OrchestrationType(row.orchestration_type),
            status=MessageStatus(row.status), created_at=row.created_at,
        ) for row in rows]


def get_inbox_messages(
    receiver_id: str, limit: int = 10, status: Optional[MessageStatus] = None
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
        query = db.query(InboxModel).filter(InboxModel.receiver_id == receiver_id)

        if status is not None:
            query = query.filter(InboxModel.status == status.value)

        messages = query.order_by(InboxModel.created_at.asc()).limit(limit).all()

        return [
            InboxMessage(
                id=msg.id,
                sender_id=msg.sender_id,
                receiver_id=msg.receiver_id,
                message=msg.message,
                orchestration_type=OrchestrationType(msg.orchestration_type),
                status=MessageStatus(msg.status),
                created_at=msg.created_at,
            )
            for msg in messages
        ]


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


WPM1_EVIDENCE_KEYS = frozenset({
    "boundary_authorized", "boundary_exhausted_at", "idle_observed_at",
    "last_activity_at", "last_observed_status", "last_observed_ref",
    "stalled_notified_at", "terminal_settled_at",
    "injection_completed_seq", "crash_recovery", "boundary_snapshot",
    "queue_corroboration", "busy_initial_submit", "redelivery_tag",
})

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
    rows = (db.query(InboxDeliveryAttemptModel).join(
        InboxDeliveryAttemptMemberModel,
        InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid,
    ).filter(InboxDeliveryAttemptMemberModel.message_id.in_(message_ids))
      .order_by(InboxDeliveryAttemptModel.started_at, InboxDeliveryAttemptModel.attempt_uuid).distinct().all())
    result = []
    for row in rows:
        members = sorted(x.message_id for x in db.query(InboxDeliveryAttemptMemberModel)
                         .filter_by(attempt_uuid=row.attempt_uuid).all())
        result.append({
            "attempt_uuid": row.attempt_uuid, "members": members, "outcome": row.outcome,
            "reason": row.reason, "payload_hash": row.payload_hash,
            "prior_attempt_uuid": row.prior_attempt_uuid,
            "receiver_terminal_id": row.receiver_terminal_id,
            "started_at": row.started_at,
            "evidence": row.evidence,
            "evidence_hash": hashlib.sha256((row.evidence or "{}").encode()).hexdigest(),
        })
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
    kind: str, message_ids: list[int], prior_attempt_uuid: str | None = None,
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
            binding = get_current_transcript_binding(row["receiver_terminal_id"])
            authority = {
                "binding_id": binding.get("id") if binding else None,
                "session_id": binding.get("session_id") if binding else None,
                "path": cursor["path"], "inode": cursor["inode"],
                "resolution_kind": cursor["resolution_kind"],
            }
            checks.append((row["payload_hash"], row.get("started_at"),
                           tuple(sorted(cursor.items())), tuple(sorted(authority.items()))))
    return AdmissionProof(kind, tuple(ids), _history_fingerprint(history),
                          prior_attempt_uuid, tuple(checks))


def _delivering_authority_in_db(db, terminal_id: str) -> list[dict[str, Any]]:
    """Map each DELIVERING inbox row to its newest durable attempt owner."""
    messages = db.query(InboxModel).filter(
        InboxModel.receiver_id == terminal_id,
        InboxModel.status == MessageStatus.DELIVERING.value,
    ).all()
    owners: dict[str, set[int]] = {}
    for message in messages:
        owner = (db.query(InboxDeliveryAttemptModel).join(
            InboxDeliveryAttemptMemberModel,
            InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid,
        ).filter(InboxDeliveryAttemptMemberModel.message_id == message.id).order_by(
            InboxDeliveryAttemptModel.started_at.desc(),
            InboxDeliveryAttemptModel.attempt_uuid.desc(),
        ).first())
        if owner is not None:
            owners.setdefault(owner.attempt_uuid, set()).add(message.id)
    return [{"attempt_uuid": attempt_uuid, "message_ids": sorted(message_ids)}
            for attempt_uuid, message_ids in sorted(owners.items())]


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
    if (not isinstance(anchor, dict) or not isinstance(exhausted_at, str)
            or not exhausted_at or not isinstance(snapshot, dict)):
        return False
    epoch, anchor_seq = anchor.get("observation_epoch"), anchor.get("seq")
    required = {
        "observation_epoch", "status", "status_gen", "input_gen", "seq",
        "last_non_ready_seq", "last_ready_seq",
    }
    if (set(snapshot) != required or not isinstance(epoch, str) or not epoch
            or type(anchor_seq) is not int
            or snapshot.get("observation_epoch") != epoch
            or snapshot.get("status") not in {
                TerminalStatus.IDLE.value, TerminalStatus.COMPLETED.value}
            or type(snapshot.get("input_gen")) is not int
            or (snapshot.get("status_gen") is not None
                and type(snapshot.get("status_gen")) is not int)
            or type(snapshot.get("seq")) is not int
            or type(snapshot.get("last_non_ready_seq")) is not int
            or type(snapshot.get("last_ready_seq")) is not int):
        return False
    non_ready = snapshot["last_non_ready_seq"]
    ready = snapshot["last_ready_seq"]
    return anchor_seq < non_ready < ready <= snapshot["seq"]


def _admission_valid(
    kind: str, history: list[dict[str, Any]], prior_uuid: str | None,
    candidate_ids: list[int],
) -> bool:
    if kind == "s4_initial":
        return all(row["outcome"] == "deferred" and row["reason"] in {
            "delivery_deferred", "input_blocked"} for row in history)
    if kind == "corrective":
        prior = next((row for row in history if row["attempt_uuid"] == prior_uuid), None)
        return bool(prior and prior["outcome"] == "ambiguous" and
                    prior["reason"] == "confirmation_timeout" and
                    _corrective_evidence_valid(prior, candidate_ids) and
                    not any(row["prior_attempt_uuid"] == prior_uuid and
                            not attempt_proven_pre_paste(row) for row in history))
    return True


def begin_delivery_attempt_if_no_other_delivering(
    messages, receiver_terminal_id: str, provider: str, payload_hash: str,
    payload_length: int, pre_input_gen=None, pre_status_gen=None, evidence: str = "{}",
    prior_attempt_uuid: str | None = None, admission_proof: AdmissionProof | None = None,
) -> AttemptOpenResult:
    ids = sorted({int(message.id) for message in messages})
    proof = admission_proof or make_admission_proof(
        "corrective" if prior_attempt_uuid else "ordinary", ids, prior_attempt_uuid)
    if tuple(ids) != proof.candidate_ids:
        return AttemptOpenResult("stale_candidate")
    attempt_uuid = str(uuid.uuid4())

    def operation(db) -> str:
        open_rows = _delivering_authority_in_db(db, receiver_terminal_id)
        if open_rows:
            db.rollback()
            return "delivering_conflict"
        history = _attempt_history_in_db(db, ids)
        if (_history_fingerprint(history) != proof.fingerprint or
                not _admission_valid(proof.kind, history, proof.prior_attempt_uuid, ids)):
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
                binding = (db.query(TranscriptBindingModel).filter_by(
                    terminal_id=receiver_terminal_id).order_by(
                        TranscriptBindingModel.received_at.desc(),
                        TranscriptBindingModel.id.desc()).first())
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
                        "path": str(live_path), "inode": live_inode,
                        "resolution_kind": "binding" if binding else cursor["resolution_kind"],
                    }
                if current_authority != authority:
                    db.rollback()
                    return "stale_admission"
                outcome, _ = bounded_transcript_suffix_lookup(cursor, payloads)
                if outcome != "absent":
                    db.rollback()
                    return "stale_admission"
        candidates = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.receiver_id == receiver_terminal_id,
            InboxModel.status == MessageStatus.PENDING.value,
        ).all()
        if sorted(row.id for row in candidates) != ids:
            db.rollback()
            return "stale_candidate"
        changed = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.receiver_id == receiver_terminal_id,
            InboxModel.status == MessageStatus.PENDING.value,
        ).update({InboxModel.status: MessageStatus.DELIVERING.value}, synchronize_session=False)
        if changed != len(ids):
            db.rollback()
            return "stale_candidate"
        first = sorted(messages, key=lambda item: item.id)[0]
        row = InboxDeliveryAttemptModel(
            attempt_uuid=attempt_uuid, receiver_terminal_id=receiver_terminal_id,
            provider=provider, payload_hash=payload_hash, payload_length=payload_length,
            pre_input_gen=pre_input_gen, pre_status_gen=pre_status_gen,
            prior_attempt_uuid=prior_attempt_uuid,
            sender_id=first.sender_id, orchestration_type=first.orchestration_type.value,
            evidence=evidence if _is_wpm1_evidence(evidence) else evidence[:2048],
        )
        db.add(row)
        for position, message_id in enumerate(ids):
            db.add(InboxDeliveryAttemptMemberModel(
                attempt_uuid=attempt_uuid, message_id=message_id, position=position))
        db.flush()
        terminal_open = _delivering_authority_in_db(db, receiver_terminal_id)
        if {row["attempt_uuid"] for row in terminal_open} != {attempt_uuid}:
            db.rollback()
            return "delivering_conflict"
        self_members = sorted(x.message_id for x in db.query(InboxDeliveryAttemptMemberModel)
                              .filter_by(attempt_uuid=attempt_uuid).all())
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
    if versioned and (type(value.get("cursor_version")) is not int or
                      value.get("cursor_version") != WPM2_CURSOR_VERSION):
        return None
    required = ("path", "inode", "size", "resolution_kind")
    if (not all(key in value for key in required) or not isinstance(value["path"], str)
            or not value["path"] or type(value["size"]) is not int or value["size"] < 0
            or not isinstance(value["resolution_kind"], str)
            or (value["inode"] is not None and type(value["inode"]) is not int)):
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


def begin_delivery_attempt(messages, receiver_terminal_id: str, provider: str, payload_hash: str,
                           payload_length: int, pre_input_gen=None, pre_status_gen=None,
                           evidence: str = "{}", prior_attempt_uuid: str | None = None) -> str:
    attempt_uuid = str(uuid.uuid4())
    with SessionLocal.begin() as db:
        if prior_attempt_uuid is not None:
            prior = db.query(InboxDeliveryAttemptModel).filter_by(
                attempt_uuid=prior_attempt_uuid).one_or_none()
            prior_members = {
                member.message_id
                for member in db.query(InboxDeliveryAttemptMemberModel).filter_by(
                    attempt_uuid=prior_attempt_uuid).all()
            }
            if prior is None or prior_members != {message.id for message in messages}:
                raise ValueError("WPM1 successor prior attempt does not match exact batch")
        else:
            prior = (db.query(InboxDeliveryAttemptModel).join(
                     InboxDeliveryAttemptMemberModel,
                     InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid)
                     .filter(InboxDeliveryAttemptMemberModel.message_id == messages[0].id)
                     .order_by(InboxDeliveryAttemptModel.started_at.desc()).first())
        row = InboxDeliveryAttemptModel(
            attempt_uuid=attempt_uuid, receiver_terminal_id=receiver_terminal_id,
            provider=provider, payload_hash=payload_hash, payload_length=payload_length,
            pre_input_gen=pre_input_gen, pre_status_gen=pre_status_gen,
            prior_attempt_uuid=prior.attempt_uuid if prior else None,
            sender_id=messages[0].sender_id,
            orchestration_type=messages[0].orchestration_type.value,
            evidence=evidence if _is_wpm1_evidence(evidence) else evidence[:2048],
        )
        db.add(row)
        for position, message in enumerate(messages):
            current = db.query(InboxModel).filter_by(id=message.id).one()
            current.status = MessageStatus.DELIVERING.value
            db.add(InboxDeliveryAttemptMemberModel(attempt_uuid=attempt_uuid,
                                                   message_id=message.id, position=position))
    return attempt_uuid


def settle_delivery_attempt(attempt_uuid: str, status: MessageStatus, outcome: str,
                            reason: str | None = None, error: str | None = None,
                            evidence: str = "{}", settled_status_gen=None,
                            on_confirmed: Callable[[], None] | None = None) -> bool:
    with SessionLocal.begin() as db:
        row = db.query(InboxDeliveryAttemptModel).filter_by(attempt_uuid=attempt_uuid).one()
        if row.settled_at is not None:
            return False
        if outcome == "deferred":
            existing = (db.query(InboxDeliveryAttemptModel).filter(
                InboxDeliveryAttemptModel.attempt_uuid != attempt_uuid,
                InboxDeliveryAttemptModel.receiver_terminal_id == row.receiver_terminal_id,
                InboxDeliveryAttemptModel.payload_hash == row.payload_hash,
                InboxDeliveryAttemptModel.reason == reason,
                InboxDeliveryAttemptModel.outcome == "deferred",
            ).first())
            if existing is not None:
                existing.count += 1
                existing.last_at = _utcnow()
                members = db.query(InboxDeliveryAttemptMemberModel).filter_by(
                    attempt_uuid=attempt_uuid).all()
                existing_ids = {x.message_id for x in db.query(InboxDeliveryAttemptMemberModel)
                                .filter_by(attempt_uuid=existing.attempt_uuid).all()}
                for member in members:
                    if member.message_id not in existing_ids:
                        member.attempt_uuid = existing.attempt_uuid
                    else:
                        db.delete(member)
                db.delete(row)
                ids = [m.message_id for m in members]
                db.query(InboxModel).filter(InboxModel.id.in_(ids)).update(
                    {InboxModel.status: status.value}, synchronize_session=False)
                return True
        row.outcome, row.reason, row.error = outcome, reason, error
        evidence_value = evidence
        if row.provider == "claude_code" and outcome not in {"confirmed", "failed"}:
            evidence_value = _canonical_json(_initialize_wpm2_cursor(_evidence_object(evidence)))
        preserve_evidence = (
            outcome == "ambiguous" and reason == "confirmation_timeout"
            and _is_wpm1_evidence(evidence_value)
        ) or _has_valid_redelivery_tag(evidence_value, row.prior_attempt_uuid)
        row.evidence = (
            _canonical_json(_evidence_object(evidence_value))
            if preserve_evidence else evidence_value[:2048]
        )
        row.settled_at = row.last_at = _utcnow()
        row.settled_status_gen = settled_status_gen
        ids = [x.message_id for x in db.query(InboxDeliveryAttemptMemberModel)
               .filter_by(attempt_uuid=attempt_uuid).all()]
        query = db.query(InboxModel).filter(InboxModel.id.in_(ids))
        if status == MessageStatus.DELIVERED:
            query = query.filter(InboxModel.status == MessageStatus.DELIVERING.value)
        changed = query.update({InboxModel.status: status.value}, synchronize_session=False)
        if status == MessageStatus.DELIVERED and changed != len(ids):
            raise RuntimeError("delivery confirmation compare-and-set lost")
        if status == MessageStatus.DELIVERED and on_confirmed is not None:
            on_confirmed()
        return True


def settle_delivery_attempt_proof_safe(
    attempt_uuid: str, evidence: dict[str, Any], settled_status_gen=None,
) -> str:
    """Compensating post-submit settlement; never leaks into generic failure."""
    def operation(db) -> str:
        row = db.query(InboxDeliveryAttemptModel).filter_by(
            attempt_uuid=attempt_uuid, settled_at=None).one_or_none()
        if row is None:
            return "stale"
        members = db.query(InboxDeliveryAttemptMemberModel).filter_by(
            attempt_uuid=attempt_uuid).all()
        ids = [member.message_id for member in members]
        delivering = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.DELIVERING.value,
        ).count()
        if delivering != len(ids):
            return "stale"
        row.outcome = "ambiguous"
        row.reason = "confirmation_timeout"
        safe_evidence = dict(evidence)
        if row.provider == "claude_code":
            safe_evidence = _initialize_wpm2_cursor(safe_evidence)
        row.evidence = _canonical_json(safe_evidence)
        row.settled_at = row.last_at = _utcnow()
        row.settled_status_gen = settled_status_gen
        changed = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.DELIVERING.value,
        ).update({InboxModel.status: MessageStatus.PENDING.value}, synchronize_session=False)
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
    message_ids: list[int], prior_attempt_uuid: str,
    on_confirmed: Callable[[], None] | None = None,
) -> bool:
    """Atomically confirm a pending batch by an existing authoritative attempt."""
    with SessionLocal.begin() as db:
        referenced_ids = {
            row.message_id for row in db.query(InboxDeliveryAttemptMemberModel).filter(
                InboxDeliveryAttemptMemberModel.attempt_uuid == prior_attempt_uuid,
                InboxDeliveryAttemptMemberModel.message_id.in_(message_ids),
            ).all()
        }
        if referenced_ids != set(message_ids):
            return False
        changed = db.query(InboxModel).filter(
            InboxModel.id.in_(message_ids),
            InboxModel.status == MessageStatus.PENDING.value,
        ).update({InboxModel.status: MessageStatus.DELIVERED.value}, synchronize_session=False)
        if changed != len(message_ids):
            return False
        if on_confirmed is not None:
            on_confirmed()
        return True


def get_message_trace(message_id: int) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        msg = db.query(InboxModel).filter_by(id=message_id).first()
        if not msg:
            return None
        rows = (db.query(InboxDeliveryAttemptModel, InboxDeliveryAttemptMemberModel.position)
                .join(InboxDeliveryAttemptMemberModel,
                      InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid)
                .filter(InboxDeliveryAttemptMemberModel.message_id == message_id)
                .order_by(InboxDeliveryAttemptModel.started_at).all())
        attempts = []
        for row, position in rows:
            item = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            for key in ("started_at", "settled_at", "last_at"):
                item[key] = item[key].isoformat() if item[key] else None
            item["position"] = position
            try: item["evidence"] = __import__("json").loads(item["evidence"])
            except Exception: item["evidence"] = {}
            attempts.append(item)
        return {"message": {"id": msg.id, "sender_id": msg.sender_id,
                            "receiver_id": msg.receiver_id, "status": msg.status,
                            "failure_reason": msg.failure_reason,
                            "created_at": msg.created_at.isoformat()}, "attempts": attempts}


def count_ambiguous_attempts(message_ids: list[int]) -> int:
    with SessionLocal() as db:
        return (db.query(InboxDeliveryAttemptModel).join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid)
                .filter(InboxDeliveryAttemptMemberModel.message_id.in_(message_ids),
                        InboxDeliveryAttemptModel.outcome == "ambiguous").distinct().count())


def list_message_attempts(message_ids: list[int]) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = (db.query(InboxDeliveryAttemptModel).join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid == InboxDeliveryAttemptModel.attempt_uuid)
                .filter(InboxDeliveryAttemptMemberModel.message_id.in_(message_ids))
                .order_by(InboxDeliveryAttemptModel.started_at).all())
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows]


def list_attempt_member_ids(attempt_uuid: str) -> list[int]:
    with SessionLocal() as db:
        rows = (db.query(InboxDeliveryAttemptMemberModel)
                .filter_by(attempt_uuid=attempt_uuid)
                .order_by(InboxDeliveryAttemptMemberModel.position).all())
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


def _record_p5_orphan_notices(db: Any, rows: list[InboxModel]) -> tuple[int, int]:
    """Insert deterministic sender notices inside the owning settlement transaction."""
    batches: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        batches.setdefault((row.sender_id, row.receiver_id), []).append(row.id)

    notification_count = 0
    logged_only_count = 0
    for (sender_id, receiver_id), message_ids in sorted(batches.items()):
        ids = sorted(message_ids)
        if db.query(TerminalModel.id).filter(TerminalModel.id == sender_id).first() is None:
            logged_only_count += 1
            logger.warning(
                "P5 orphan settlement has no live sender %s for receiver %s batch %s",
                sender_id, receiver_id, ids,
            )
            continue
        header = f"p5-orphan receiver={receiver_id} batch={_p5_batch_key(ids)}\n"
        notice_sender = f"message-trace:{receiver_id}"
        existing = db.query(InboxModel).filter(
            InboxModel.sender_id == notice_sender,
            InboxModel.receiver_id == sender_id,
            text("substr(message, 1, :n) = :header").bindparams(
                n=len(header), header=header),
        ).first()
        if existing is None:
            db.add(InboxModel(
                sender_id=notice_sender, receiver_id=sender_id,
                message=(header +
                         f"[message-trace] delivery to terminal {receiver_id} failed because "
                         f"the receiver terminal no longer exists for message(s) {ids}."),
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            ))
            notification_count += 1
    return notification_count, logged_only_count


def settle_pending_orphan_messages(
    limit: int = ORPHAN_RECONCILE_BATCH_LIMIT,
) -> OrphanReconcileResult:
    """Settle the oldest PENDING messages whose receiver row is absent."""
    if limit <= 0:
        raise ValueError("orphan reconcile limit must be positive")

    def operation(db: Any) -> OrphanReconcileResult:
        candidates = (db.query(InboxModel).filter(
            InboxModel.status == MessageStatus.PENDING.value,
            ~exists().where(TerminalModel.id == InboxModel.receiver_id),
        ).order_by(InboxModel.created_at.asc(), InboxModel.id.asc()).limit(limit).all())
        settled: list[InboxModel] = []
        for candidate in candidates:
            changed = (db.query(InboxModel).filter(
                InboxModel.id == candidate.id,
                InboxModel.status == MessageStatus.PENDING.value,
                ~exists().where(TerminalModel.id == InboxModel.receiver_id),
            ).update({
                InboxModel.status: MessageStatus.DELIVERY_FAILED.value,
                InboxModel.failure_reason: "receiver_gone",
            }, synchronize_session=False))
            if changed == 1:
                settled.append(candidate)
        notification_count, logged_only_count = _record_p5_orphan_notices(db, settled)
        return OrphanReconcileResult(
            settled_count=len(settled), notification_count=notification_count,
            logged_only_count=logged_only_count,
        )

    result = _run_wpm1_immediate(operation)
    if isinstance(result, OrphanReconcileResult):
        return result
    return OrphanReconcileResult(busy_aborted=True)


def advance_wpm2_continuity_cursor(
    attempt_uuid: str, exact_message_ids: list[int], expected_ref: dict[str, Any],
    observed_ref: dict[str, Any],
) -> str:
    expected = _valid_cursor(expected_ref) or _valid_cursor(expected_ref, versioned=False)
    observed = _valid_cursor(observed_ref) or _valid_cursor(observed_ref, versioned=False)
    if expected is None or observed is None:
        return "stale"
    identity = ("path", "inode", "resolution_kind")
    if (any(expected[key] != observed[key] for key in identity) or
            observed["size"] < expected["size"]):
        return "stale"
    ids = sorted(set(exact_message_ids))

    def operation(db) -> str:
        row = db.query(InboxDeliveryAttemptModel).filter_by(
            attempt_uuid=attempt_uuid).one_or_none()
        if row is None or row.settled_at is None or row.outcome not in {
            "ambiguous", "interrupted", "deferred"}:
            return "stale"
        if row.outcome == "deferred" and row.reason not in {"delivery_deferred", "input_blocked"}:
            return "stale"
        members = sorted(x.message_id for x in db.query(InboxDeliveryAttemptMemberModel)
                         .filter_by(attempt_uuid=attempt_uuid).all())
        pending = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value).count()
        if members != ids or pending != len(ids):
            return "stale"
        evidence = _evidence_object(row.evidence)
        stored_raw = evidence.get("last_observed_ref")
        stored = _valid_cursor(stored_raw)
        upgrade = stored is None and isinstance(stored_raw, dict) and "cursor_version" not in stored_raw
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
            **{key: observed[key] for key in identity}, "size": observed["size"],
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
        row = db.query(InboxDeliveryAttemptModel).filter_by(
            attempt_uuid=attempt_uuid, outcome="ambiguous", reason="confirmation_timeout"
        ).first()
        if row is None:
            return "stale"
        members = [x.message_id for x in db.query(InboxDeliveryAttemptMemberModel).filter_by(
            attempt_uuid=attempt_uuid).all()]
        if set(members) != set(message_ids):
            return "stale"
        pending = db.query(InboxModel).filter(
            InboxModel.id.in_(message_ids), InboxModel.status == MessageStatus.PENDING.value
        ).count()
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
    if db.query(TerminalModel).filter_by(id=sender_id).first() is not None:
        return sender_id
    receiver = db.query(TerminalModel).filter_by(id=receiver_terminal_id).first()
    caller_id = receiver.caller_id if receiver is not None else None
    if caller_id and db.query(TerminalModel).filter_by(id=caller_id).first() is not None:
        return cast(str, caller_id)
    return None


def record_wpm1_stalled_notice(
    attempt_uuid: str, message_ids: list[int], receiver_terminal_id: str,
    notified_at: str,
) -> str:
    """Atomically mark a stalled batch and enqueue its exactly-once notice."""
    ids = sorted(set(message_ids))
    header = f"wpm1-notice kind=stalled batch={_wpm1_batch_key(ids)}\n"

    def operation(db) -> str:
        row = db.query(InboxDeliveryAttemptModel).filter_by(
            attempt_uuid=attempt_uuid, outcome="ambiguous", reason="confirmation_timeout"
        ).first()
        if row is None:
            db.rollback()
            return "stale"
        members = {x.message_id for x in db.query(InboxDeliveryAttemptMemberModel).filter_by(
            attempt_uuid=attempt_uuid).all()}
        pending = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value).count()
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
        existing = db.query(InboxModel).filter(
            InboxModel.sender_id == sender, InboxModel.receiver_id == recipient,
            text("substr(message, 1, :n) = :header").bindparams(n=len(header), header=header),
        ).first()
        if existing is None:
            db.add(InboxModel(
                sender_id=sender, receiver_id=recipient,
                message=header + "delivery stalled: receiver shows no progress / payload not yet "
                "confirmed; no reinjection will occur while unproven; will confirm if consumed",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            ))
        return "recorded"

    return _run_wpm1_immediate(operation)


def settle_wpm1_terminal_batch(
    message_ids: list[int], status: MessageStatus, receiver_terminal_id: str,
    *, reason: str | None = None, on_confirmed: Callable[[], None] | None = None,
    confirmation_evidence: tuple[str, dict[str, Any]] | None = None,
) -> str:
    """Merge the terminal clock before the exact-batch CAS, with corrective notice."""
    ids = sorted(set(message_ids))
    clock = _utcnow().isoformat().replace("+00:00", "Z")
    stalled_header = f"wpm1-notice kind=stalled batch={_wpm1_batch_key(ids)}\n"
    corrective_header = f"wpm1-notice kind=corrective batch={_wpm1_batch_key(ids)}\n"

    def operation(db) -> str:
        attempts = (db.query(InboxDeliveryAttemptModel)
                    .join(InboxDeliveryAttemptMemberModel,
                          InboxDeliveryAttemptMemberModel.attempt_uuid ==
                          InboxDeliveryAttemptModel.attempt_uuid)
                    .filter(InboxDeliveryAttemptMemberModel.message_id.in_(ids),
                            InboxDeliveryAttemptModel.outcome == "ambiguous",
                            InboxDeliveryAttemptModel.reason == "confirmation_timeout")
                    .order_by(InboxDeliveryAttemptModel.started_at.desc()).all())
        target = next((row for row in attempts if {
            x.message_id for x in db.query(InboxDeliveryAttemptMemberModel).filter_by(
                attempt_uuid=row.attempt_uuid).all()} == set(ids)), None)
        if target is None:
            db.rollback()
            return "stale"
        pending = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value).count()
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
        changed = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.PENDING.value
        ).update({InboxModel.status: status.value}, synchronize_session=False)
        if changed != len(ids):
            db.rollback()
            return "stale"
        if status == MessageStatus.DELIVERED:
            any_stalled = any(_evidence_object(row.evidence).get("stalled_notified_at")
                              for row in attempts)
            if any_stalled:
                sender = f"message-trace:{receiver_terminal_id}"
                stalled = db.query(InboxModel).filter(
                    InboxModel.sender_id == sender,
                    text("substr(message, 1, :n) = :header").bindparams(
                        n=len(stalled_header), header=stalled_header),
                ).first()
                if stalled is not None and db.query(TerminalModel).filter_by(
                        id=stalled.receiver_id).first() is not None:
                    existing = db.query(InboxModel).filter(
                        InboxModel.sender_id == sender,
                        InboxModel.receiver_id == stalled.receiver_id,
                        text("substr(message, 1, :n) = :header").bindparams(
                            n=len(corrective_header), header=corrective_header),
                    ).first()
                    if existing is None:
                        db.add(InboxModel(
                            sender_id=sender, receiver_id=stalled.receiver_id,
                            message=corrective_header + "previously-stalled message was delivered",
                            orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                            status=MessageStatus.PENDING.value,
                        ))
                else:
                    logger.warning("WPM1 corrective notice recipient is no longer available for batch %s", ids)
            if on_confirmed is not None:
                on_confirmed()
        return "settled"

    return _run_wpm1_immediate(operation)


def get_callback_status_since(
    sender_id: str, receiver_id: str, since: datetime
) -> MessageStatus | None:
    """Return a newer callback status that suppresses the watchdog."""
    with SessionLocal() as db:
        row = db.query(InboxModel.status).filter(
            InboxModel.sender_id == sender_id,
            InboxModel.receiver_id == receiver_id,
            InboxModel.created_at > since,
            InboxModel.status.in_(
                (
                    MessageStatus.PENDING.value,
                    MessageStatus.DELIVERING.value,
                    MessageStatus.DELIVERED.value,
                )
            ),
        ).first()
        return MessageStatus(row[0]) if row is not None else None


def transition_pending_to_delivery_failed(message_ids: list[int]) -> bool:
    """Cap transition; True exactly once even across process restarts."""
    with SessionLocal.begin() as db:
        changed = (db.query(InboxModel).filter(
            InboxModel.id.in_(message_ids), InboxModel.status == MessageStatus.PENDING.value)
            .update({InboxModel.status: MessageStatus.DELIVERY_FAILED.value},
                    synchronize_session=False))
        return changed > 0


def list_stale_delivering_messages() -> List[InboxMessage]:
    with SessionLocal() as db:
        rows = db.query(InboxModel).filter_by(status=MessageStatus.DELIVERING.value).all()
        return [InboxMessage(id=x.id, sender_id=x.sender_id, receiver_id=x.receiver_id,
                message=x.message, orchestration_type=OrchestrationType(x.orchestration_type),
                status=MessageStatus(x.status), created_at=x.created_at) for x in rows]


def list_stale_open_claude_attempts(age_seconds: int) -> list[dict[str, Any]]:
    bound = _utcnow() - timedelta(seconds=age_seconds)
    with SessionLocal() as db:
        rows = db.query(InboxDeliveryAttemptModel).filter(
            InboxDeliveryAttemptModel.provider == "claude_code",
            InboxDeliveryAttemptModel.settled_at.is_(None),
            InboxDeliveryAttemptModel.started_at <= bound,
        ).order_by(InboxDeliveryAttemptModel.started_at).all()
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} | {
            "message_ids": sorted(x.message_id for x in db.query(
                InboxDeliveryAttemptMemberModel).filter_by(
                    attempt_uuid=row.attempt_uuid).all())
        } for row in rows]


def recover_wpm2_stale_attempt(
    attempt_uuid: str, exact_message_ids: list[int], status: MessageStatus,
    outcome: str, reason: str, evidence: dict[str, Any],
) -> str:
    ids = sorted(set(exact_message_ids))

    def operation(db) -> str:
        row = db.query(InboxDeliveryAttemptModel).filter_by(
            attempt_uuid=attempt_uuid, settled_at=None, provider="claude_code").one_or_none()
        if row is None:
            return "stale"
        members = sorted(x.message_id for x in db.query(InboxDeliveryAttemptMemberModel)
                         .filter_by(attempt_uuid=attempt_uuid).all())
        delivering_rows = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.DELIVERING.value).all()
        if members != ids or sorted(message.id for message in delivering_rows) != ids:
            return "stale"
        row.outcome, row.reason = outcome, reason
        row.evidence = _canonical_json(_initialize_wpm2_cursor(dict(evidence)))
        row.settled_at = row.last_at = _utcnow()
        updates: dict[Any, Any] = {InboxModel.status: status.value}
        if status == MessageStatus.DELIVERY_FAILED and reason == "receiver_gone":
            updates[InboxModel.failure_reason] = "receiver_gone"
        changed = db.query(InboxModel).filter(
            InboxModel.id.in_(ids), InboxModel.status == MessageStatus.DELIVERING.value,
        ).update(updates, synchronize_session=False)
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
        now = datetime.now()
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
