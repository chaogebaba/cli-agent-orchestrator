"""Cleanup service for old terminals, messages, and logs."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent_orchestrator.clients.database import (
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxModel,
    SessionLocal,
    TerminalModel,
    _utcnow,
    delete_terminal_and_warm_intent,
)
from cli_agent_orchestrator.constants import (
    LOG_DIR,
    MEMORY_BASE_DIR,
    RETENTION_DAYS,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.memory_format import parse_index_entry
from cli_agent_orchestrator.services.settings_service import get_logs_settings
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)

# Terminal-log file suffixes managed by the F619 (#475) retention machinery.
# The active pipe-pane byte log and its single rotation backup. The delete-time
# ``.scrollback`` / ``.snapshot.json`` restore artifacts are deliberately NOT
# in this set: those are the debugging/restore snapshot and are aged out by the
# separate RETENTION_DAYS sweep in cleanup_old_data, not by the log caps here.
_TERMINAL_LOG_SUFFIXES = (".log", ".log.1")


def _terminal_id_from_log_name(name: str) -> str:
    """Return the terminal id encoded in a managed log filename.

    ``<id>.log`` and ``<id>.log.1`` both map back to ``<id>``. A name that
    matches neither suffix returns ``""`` (caller skips it).
    """
    if name.endswith(".log.1"):
        return name[: -len(".log.1")]
    if name.endswith(".log"):
        return name[: -len(".log")]
    return ""


def _iter_terminal_log_files() -> list[Path]:
    """Return every managed terminal-log file (``*.log`` and ``*.log.1``)."""
    if not TERMINAL_LOG_DIR.exists():
        return []
    files: list[Path] = []
    for suffix in _TERMINAL_LOG_SUFFIXES:
        files.extend(TERMINAL_LOG_DIR.glob(f"*{suffix}"))
    return files


def _live_terminal_ids() -> set[str]:
    """Return the id of every terminal that still has a DB row.

    A log whose terminal id is in this set belongs to a LIVE (still-tracked)
    terminal and is never pruned by age or by the total-size cap, regardless of
    mtime — pruning it would delete the log of a running agent (F619 #475).
    A DB read failure raises to the caller, which treats it as "cannot prove
    dead" and prunes nothing (fail-safe).
    """
    with SessionLocal() as db:
        return {row.id for row in db.query(TerminalModel.id).all()}


def prune_terminal_log(terminal_id: str) -> int:
    """Remove a single terminal's managed log files (``.log`` + ``.log.1``).

    Called from the ``delete_terminal`` teardown path so a reaped terminal's
    (potentially large) pipe-pane byte log does not linger until the retention
    sweep. Returns the number of files removed. Best-effort and exception-safe:
    a failure to unlink is logged and swallowed so it can never fail a delete.
    """
    removed = 0
    if not terminal_id:
        return 0
    for suffix in _TERMINAL_LOG_SUFFIXES:
        path = TERMINAL_LOG_DIR / f"{terminal_id}{suffix}"
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Failed to prune terminal log %s: %s", path, e)
    if removed:
        logger.info("Pruned %d log file(s) for terminal %s", removed, terminal_id)
    return removed


def prune_terminal_logs_at_startup() -> dict[str, int]:
    """Prune dead-terminal logs by age, then enforce the whole-dir size cap.

    Two passes over ``TERMINAL_LOG_DIR`` (F619 #475), both skipping any file
    whose terminal id still has a DB row (LIVE — never touched):

    1. AGE: delete a dead terminal's log when its mtime is older than
       ``logs.retention_hours``.
    2. TOTAL CAP: if the surviving dead-terminal logs still exceed
       ``logs.max_total_mb`` in aggregate, delete them OLDEST-mtime-FIRST until
       the total is back under the cap.

    Returns a small counters dict for the startup log line. Fully exception-safe:
    any failure degrades to a no-op with a WARNING rather than blocking boot.
    """
    counters = {"pruned_age": 0, "pruned_total_cap": 0}
    try:
        settings = get_logs_settings()
        retention_hours = float(settings["retention_hours"])
        max_total_bytes = int(settings["max_total_mb"]) * 1024 * 1024

        try:
            live_ids = _live_terminal_ids()
        except Exception as e:
            logger.warning("Startup log prune skipped — cannot read live terminals: %s", e)
            return counters

        cutoff = (_utcnow() - timedelta(hours=retention_hours)).timestamp()

        # Pass 1 — age. Collect (path, mtime, size) for dead-terminal logs that
        # survive the age cut so pass 2 can enforce the total cap on them.
        survivors: list[tuple[Path, float, int]] = []
        for path in _iter_terminal_log_files():
            tid = _terminal_id_from_log_name(path.name)
            if not tid or tid in live_ids:
                continue  # unrecognized name, or LIVE terminal — never touch
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                try:
                    path.unlink()
                    counters["pruned_age"] += 1
                except OSError as e:
                    logger.warning("Failed to age-prune terminal log %s: %s", path, e)
            else:
                survivors.append((path, st.st_mtime, st.st_size))

        # Pass 2 — total cap. Oldest-first over the age-pass survivors (all
        # dead-terminal). Live-terminal logs were never added, so their bytes
        # are intentionally NOT counted against the cap and never deleted here.
        total = sum(size for _, _, size in survivors)
        if total > max_total_bytes:
            for path, _mtime, size in sorted(survivors, key=lambda t: t[1]):
                if total <= max_total_bytes:
                    break
                try:
                    path.unlink()
                    total -= size
                    counters["pruned_total_cap"] += 1
                except OSError as e:
                    logger.warning("Failed to total-cap-prune terminal log %s: %s", path, e)

        logger.info(
            "startup_terminal_log_prune pruned_age=%d pruned_total_cap=%d",
            counters["pruned_age"],
            counters["pruned_total_cap"],
        )
    except Exception as e:
        logger.warning("Startup terminal-log prune failed; continuing: %s", e)
    return counters


def cleanup_old_data():
    """Clean up terminals, inbox messages, and log files older than RETENTION_DAYS."""
    try:
        cutoff_date = _utcnow() - timedelta(days=RETENTION_DAYS)
        logger.info(
            f"Starting cleanup of data older than {RETENTION_DAYS} days (before {cutoff_date})"
        )

        # Clean up old terminals (stop FIFO readers and clear state first)
        with SessionLocal() as db:
            old_terminals = (
                db.query(TerminalModel)
                .filter(
                    (TerminalModel.last_active < cutoff_date)
                    & (TerminalModel.init_state == "ready"),
                )
                .all()
            )
            # D10+D14 (F202): pane liveness RESETS the idle clock — a live pane
            # is never reclaimed by idle-age retention alone, but a dead one still is.
            from cli_agent_orchestrator.backends.registry import get_backend

            backend = get_backend()
            reclaimable = []
            retained_terminal_ids: set[str] = set()
            for terminal in old_terminals:
                liveness = backend.window_liveness(terminal.tmux_session, terminal.tmux_window)
                if liveness == "live":
                    # D14: reset idle clock so it doesn't re-appear until another
                    # RETENTION_DAYS pass without activity.
                    terminal.last_active = _utcnow()
                    logger.info(
                        "retention_survivor_reset terminal=%s session=%s window=%s",
                        terminal.id,
                        terminal.tmux_session,
                        terminal.tmux_window,
                    )
                else:
                    reclaimable.append(terminal)
            db.commit()

            for terminal in reclaimable:
                fifo_manager.stop_reader(terminal.id)
                status_monitor.clear_terminal(terminal.id)
                # A stale Grok terminal can still own a private GROK_HOME. An
                # explicit deferred cleanup is its retry handle, so retention
                # housekeeping must not bulk-delete that row underneath it.
                if (
                    getattr(terminal, "provider", None) == ProviderType.GROK_CLI.value
                    and provider_manager.cleanup_provider(terminal.id) is False
                ):
                    retained_terminal_ids.add(terminal.id)
                    logger.warning(
                        "Retaining stale Grok terminal %s while cleanup is deferred", terminal.id
                    )
            skipped = (
                db.query(TerminalModel)
                .filter(
                    (TerminalModel.last_active < cutoff_date)
                    & (TerminalModel.init_state != "ready"),
                )
                .count()
            )
        deleted_terminals = sum(
            delete_terminal_and_warm_intent(
                terminal.id,
                preserve_warm_intent=False,
            )["terminal_deleted"]
            for terminal in reclaimable
            if terminal.id not in retained_terminal_ids
        )
        logger.info(f"Deleted {deleted_terminals} old terminals from database")
        if skipped:
            logger.warning("retention_cleanup_skipped_non_ready count=%d", skipped)

        # Clean up old inbox messages
        with SessionLocal() as db:
            old_rows = db.query(InboxModel).filter(InboxModel.created_at < cutoff_date).all()
            old_ids_all = {row.id for row in old_rows}
            gated_rows = (
                db.query(InboxDeliveryAttemptModel, InboxDeliveryAttemptMemberModel)
                .join(
                    InboxDeliveryAttemptMemberModel,
                    InboxDeliveryAttemptMemberModel.attempt_uuid
                    == InboxDeliveryAttemptModel.attempt_uuid,
                )
                .filter(
                    InboxDeliveryAttemptModel.outcome == "ambiguous",
                    InboxDeliveryAttemptModel.reason.in_(("confirmation_timeout", "receiver_gone")),
                )
                .all()
            )
            batch_by_attempt: dict[str, set[int]] = {}
            for attempt, member in gated_rows:
                batch_by_attempt.setdefault(attempt.attempt_uuid, set()).add(member.message_id)
            attempts_by_batch: dict[tuple[int, ...], list] = {}
            for attempt, _member in gated_rows:
                key = tuple(sorted(batch_by_attempt[attempt.attempt_uuid]))
                if attempt not in attempts_by_batch.setdefault(key, []):
                    attempts_by_batch[key].append(attempt)
            retained_ids: set[int] = set()
            retained_ids.update(
                row[0]
                for row in db.query(InboxModel.id)
                .join(
                    CallbackBarrierModel,
                    CallbackBarrierModel.id == InboxModel.barrier_id,
                )
                .filter(
                    InboxModel.status == "held",
                    CallbackBarrierModel.state == "OPEN",
                )
                .all()
            )
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
                    logger.warning(
                        "Malformed WPM1 terminal_settled_at for batch %s; retaining", key
                    )
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
                r"^wpm1-notice kind=(?:stalled|corrective) batch=([0-9]+(?:,[0-9]+)*)\n"
            )
            for row in old_rows:
                if not row.sender_id.startswith("message-trace:"):
                    continue
                match = notice_pattern.match(row.message)
                if match and set(map(int, match.group(1).split(","))) <= retained_ids:
                    retained_ids.add(row.id)
            old_ids = list(old_ids_all - retained_ids)
            logger.info("Exempted %s gated WPM1 batch(es) from inbox cleanup", exempt_batches)
            attempt_ids = [
                x[0]
                for x in db.query(InboxDeliveryAttemptMemberModel.attempt_uuid)
                .filter(InboxDeliveryAttemptMemberModel.message_id.in_(old_ids))
                .all()
            ]
            if old_ids:
                db.query(CallbackBarrierMemberModel).filter(
                    CallbackBarrierMemberModel.message_id.in_(old_ids)
                ).update(
                    {CallbackBarrierMemberModel.message_id: None},
                    synchronize_session=False,
                )
                db.query(InboxDeliveryAttemptMemberModel).filter(
                    InboxDeliveryAttemptMemberModel.message_id.in_(old_ids)
                ).delete(synchronize_session=False)
            if attempt_ids:
                remaining_attempt_ids = {
                    row[0]
                    for row in db.query(InboxDeliveryAttemptMemberModel.attempt_uuid)
                    .filter(InboxDeliveryAttemptMemberModel.attempt_uuid.in_(attempt_ids))
                    .all()
                }
                orphaned_attempt_ids = set(attempt_ids) - remaining_attempt_ids
                if orphaned_attempt_ids:
                    db.query(InboxDeliveryAttemptModel).filter(
                        InboxDeliveryAttemptModel.attempt_uuid.in_(orphaned_attempt_ids)
                    ).delete(synchronize_session=False)
            deleted_messages = (
                db.query(InboxModel)
                .filter(InboxModel.id.in_(old_ids))
                .delete(synchronize_session=False)
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
        match = parse_index_entry(line)
        if not match:
            continue

        key = match.group("key")
        relative_path = match.group("path")
        memory_type = match.group("type")
        updated_str = match.group("updated")

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
