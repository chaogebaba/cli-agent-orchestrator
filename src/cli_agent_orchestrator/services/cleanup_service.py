"""Cleanup service for old terminals, messages, and logs."""

import logging
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent_orchestrator.clients.database import (
    InboxModel, InboxDeliveryAttemptMemberModel, InboxDeliveryAttemptModel,
    SessionLocal, TerminalModel,
)
from cli_agent_orchestrator.constants import (
    LOG_DIR,
    MEMORY_BASE_DIR,
    RETENTION_DAYS,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)


def cleanup_old_data():
    """Clean up terminals, inbox messages, and log files older than RETENTION_DAYS."""
    try:
        cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
        logger.info(
            f"Starting cleanup of data older than {RETENTION_DAYS} days (before {cutoff_date})"
        )

        # Clean up old terminals (stop FIFO readers and clear state first)
        with SessionLocal() as db:
            old_terminals = (
                db.query(TerminalModel).filter(TerminalModel.last_active < cutoff_date).all()
            )
            for terminal in old_terminals:
                fifo_manager.stop_reader(terminal.id)
                status_monitor.clear_terminal(terminal.id)
            deleted_terminals = (
                db.query(TerminalModel).filter(TerminalModel.last_active < cutoff_date).delete()
            )
            db.commit()
            logger.info(f"Deleted {deleted_terminals} old terminals from database")

        # Clean up old inbox messages
        with SessionLocal() as db:
            old_rows = db.query(InboxModel).filter(InboxModel.created_at < cutoff_date).all()
            old_ids_all = {row.id for row in old_rows}
            gated_rows = (db.query(InboxDeliveryAttemptModel, InboxDeliveryAttemptMemberModel)
                          .join(InboxDeliveryAttemptMemberModel,
                                InboxDeliveryAttemptMemberModel.attempt_uuid ==
                                InboxDeliveryAttemptModel.attempt_uuid)
                          .filter(InboxDeliveryAttemptModel.outcome == "ambiguous",
                                  InboxDeliveryAttemptModel.reason.in_((
                                      "confirmation_timeout", "receiver_gone")))
                          .all())
            batch_by_attempt: dict[str, set[int]] = {}
            for attempt, member in gated_rows:
                batch_by_attempt.setdefault(attempt.attempt_uuid, set()).add(member.message_id)
            attempts_by_batch: dict[tuple[int, ...], list] = {}
            for attempt, _member in gated_rows:
                key = tuple(sorted(batch_by_attempt[attempt.attempt_uuid]))
                if attempt not in attempts_by_batch.setdefault(key, []):
                    attempts_by_batch[key].append(attempt)
            retained_ids: set[int] = set()
            exempt_batches = 0
            now_cutoff = cutoff_date.replace(tzinfo=timezone.utc)
            for key, attempts in attempts_by_batch.items():
                rows = db.query(InboxModel).filter(InboxModel.id.in_(key)).all()
                if len(rows) != len(key):
                    continue
                clocks: list[datetime] = []
                malformed = False
                for attempt in attempts:
                    try:
                        evidence = json.loads(attempt.evidence or "{}")
                        value = evidence.get("terminal_settled_at")
                        if value is None:
                            continue
                        if not isinstance(value, str):
                            malformed = True
                            break
                        clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        clocks.append(clock if clock.tzinfo else clock.replace(tzinfo=timezone.utc))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        malformed = True
                        break
                pending = any(row.status == "pending" for row in rows)
                retain = pending
                if malformed:
                    logger.warning("Malformed WPM1 terminal_settled_at for batch %s; retaining", key)
                    retain = True
                elif not clocks:
                    logger.warning("Absent WPM1 terminal_settled_at for batch %s; retaining", key)
                    retain = True
                elif clocks and max(clocks) >= now_cutoff:
                    retain = True
                if retain:
                    exempt_batches += 1
                    retained_ids.update(key)

            # Durable notice keys live exactly as long as their referenced batch.
            notice_pattern = re.compile(
                r"^wpm1-notice kind=(?:stalled|corrective) batch=([0-9]+(?:,[0-9]+)*)\n")
            for row in old_rows:
                if not row.sender_id.startswith("message-trace:"):
                    continue
                match = notice_pattern.match(row.message)
                if match and set(map(int, match.group(1).split(","))) <= retained_ids:
                    retained_ids.add(row.id)
            old_ids = list(old_ids_all - retained_ids)
            logger.info("Exempted %s gated WPM1 batch(es) from inbox cleanup", exempt_batches)
            attempt_ids = [x[0] for x in db.query(InboxDeliveryAttemptMemberModel.attempt_uuid)
                           .filter(InboxDeliveryAttemptMemberModel.message_id.in_(old_ids)).all()]
            if old_ids:
                db.query(InboxDeliveryAttemptMemberModel).filter(
                    InboxDeliveryAttemptMemberModel.message_id.in_(old_ids)).delete(
                    synchronize_session=False)
            if attempt_ids:
                remaining_attempt_ids = {
                    row[0]
                    for row in db.query(InboxDeliveryAttemptMemberModel.attempt_uuid)
                    .filter(InboxDeliveryAttemptMemberModel.attempt_uuid.in_(attempt_ids)).all()
                }
                orphaned_attempt_ids = set(attempt_ids) - remaining_attempt_ids
                if orphaned_attempt_ids:
                    db.query(InboxDeliveryAttemptModel).filter(
                        InboxDeliveryAttemptModel.attempt_uuid.in_(orphaned_attempt_ids)).delete(
                        synchronize_session=False)
            deleted_messages = (
                db.query(InboxModel).filter(InboxModel.id.in_(old_ids)).delete(
                    synchronize_session=False)
            )
            db.commit()
            logger.info(f"Deleted {deleted_messages} old inbox messages from database")

        # Clean up old terminal log files
        terminal_logs_deleted = 0
        if TERMINAL_LOG_DIR.exists():
            for pattern in ("*.log", "*.scrollback", "*.snapshot.json"):
                for log_file in TERMINAL_LOG_DIR.glob(pattern):
                    if log_file.stat().st_mtime < cutoff_date.timestamp():
                        log_file.unlink()
                        terminal_logs_deleted += 1
        logger.info(f"Deleted {terminal_logs_deleted} old terminal log files")

        # Clean up old server log files
        server_logs_deleted = 0
        if LOG_DIR.exists():
            for log_file in LOG_DIR.glob("*.log"):
                if log_file.stat().st_mtime < cutoff_date.timestamp():
                    log_file.unlink()
                    server_logs_deleted += 1
        logger.info(f"Deleted {server_logs_deleted} old server log files")

        logger.info("Cleanup completed successfully")

    except Exception as e:
        logger.error(f"Error during cleanup: {e}")


# =============================================================================
# Memory Cleanup — tiered retention
# =============================================================================

# Scope-keyed retention policy. ``user`` and ``feedback`` memory_types
# are operator-curated and stay forever regardless of scope. Anything
# else expires per the scope of the entry.
SCOPE_RETENTION_DAYS: dict[str, int | None] = {
    "global": None,
    "agent": None,
    "project": 90,
    "session": 14,
    "federated": None,
}
PERMANENT_MEMORY_TYPES: frozenset[str] = frozenset({"user", "feedback"})


async def cleanup_expired_memories() -> None:
    """Delete expired memories based on scope-keyed retention policy.

    - session scope: 14 days
    - project scope: 90 days
    - global scope:  never expires
    - agent scope:   never expires
    - memory_type ``user`` or ``feedback``: never expires (regardless of scope)

    Idempotent — safe to run multiple times.
    """
    import asyncio

    try:
        now = datetime.now(timezone.utc)
        expired_count = 0

        if not MEMORY_BASE_DIR.exists():
            return

        # Lazy-import to avoid circular imports at module level
        from cli_agent_orchestrator.services.memory_service import MemoryService

        memory_service = MemoryService(base_dir=MEMORY_BASE_DIR)

        # Walk project dirs: {MEMORY_BASE_DIR}/{project_dir}/wiki/index.md
        # Glob and parse are sync I/O; offload to a thread so the event
        # loop stays responsive when there are many projects.
        index_paths = await asyncio.to_thread(lambda: list(MEMORY_BASE_DIR.glob("*/wiki/index.md")))
        for index_path in index_paths:
            expired_entries = await asyncio.to_thread(_find_expired_entries, index_path, now)
            if not expired_entries:
                continue

            # Extract scope_id from path: .../memory/{scope_id}/wiki/index.md
            # "global"/"federated" dirs → scope_id=None (flat, machine-wide),
            # project hash dirs → scope_id=hash
            project_dir_name = index_path.parent.parent.name
            scope_id = None if project_dir_name in ("global", "federated") else project_dir_name

            for entry in expired_entries:
                try:
                    # Prefer the entry's own scope_id (parsed from the
                    # nested wiki path) so per-session and per-agent
                    # files resolve correctly. Fall back to the
                    # container's scope_id otherwise.
                    effective_scope_id = entry.get("scope_id") or scope_id
                    # ``forget()`` is declared async but its body is
                    # sync FS work (unlink + flock + index rewrite).
                    # Offload to a thread so the event loop stays
                    # responsive when many entries expire.
                    await asyncio.to_thread(
                        _forget_sync,
                        memory_service,
                        entry["key"],
                        entry["scope"],
                        effective_scope_id,
                    )
                    expired_count += 1
                    logger.info(
                        f"Expired memory: key={entry['key']} scope={entry['scope']} "
                        f"type={entry['memory_type']}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to expire memory key={entry['key']}: {e}")

        if expired_count > 0:
            logger.info(f"Memory cleanup: expired {expired_count} memories")
        else:
            logger.debug("Memory cleanup: no expired memories found")

    except Exception as e:
        logger.error(f"Error during memory cleanup: {e}")


def _forget_sync(memory_service, key: str, scope: str, scope_id: str | None) -> None:
    """Run MemoryService.forget() synchronously in a worker thread.

    forget() is declared async but its body is sync; we invoke it
    via asyncio.run inside the worker thread so the outer event
    loop is not blocked by the unlink + flock + index rewrite.
    """
    import asyncio as _asyncio

    _asyncio.run(memory_service.forget(key=key, scope=scope, scope_id=scope_id))


def _find_expired_entries(index_path: Path, now: datetime) -> list[dict]:
    """Parse an index.md and return entries that have exceeded their retention."""
    expired: list[dict] = []

    try:
        content = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return expired

    current_scope: str | None = None

    for line in content.splitlines():
        # Detect scope section headers: ## global, ## session, etc.
        if line.startswith("## "):
            section = line[3:].strip()
            if section in ("global", "project", "session", "agent", "federated"):
                current_scope = section
            continue

        if not current_scope:
            continue

        # Parse entry: - [key](scope/key.md) — type:X tags:Y ~Ntok updated:Z
        match = re.match(
            r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:\S* ~\d+tok updated:(\S+)$",
            line,
        )
        if not match:
            continue

        key = match.group(1)
        relative_path = match.group(2)
        memory_type = match.group(3)
        updated_str = match.group(4)

        # Extract scope_id from nested path for session/agent scopes:
        #   session/<scope_id>/<key>.md  →  scope_id
        # Flat paths (project, global) leave entry_scope_id = None.
        entry_scope_id: str | None = None
        path_parts = relative_path.split("/")
        if len(path_parts) >= 3 and path_parts[0] in ("session", "agent"):
            entry_scope_id = path_parts[1]

        # Parse updated_at timestamp
        try:
            updated_at = datetime.strptime(updated_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        age_days = (now - updated_at).days

        # ``user`` and ``feedback`` memory_types are curated knowledge;
        # they never expire regardless of scope.
        if memory_type in PERMANENT_MEMORY_TYPES:
            continue

        retention_days = SCOPE_RETENTION_DAYS.get(current_scope)
        if retention_days is not None and age_days > retention_days:
            expired.append(
                {
                    "key": key,
                    "scope": current_scope,
                    "scope_id": entry_scope_id,
                    "memory_type": memory_type,
                }
            )

    return expired
