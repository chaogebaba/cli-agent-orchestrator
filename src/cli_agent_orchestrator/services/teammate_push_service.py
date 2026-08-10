"""WP-W2M-PUSH-BRIDGE — native CC-teammate push notification bridge.

Writes short notification entries into the CC native inbox file
(~/.claude/teams/{team}/inboxes/team-lead.json) when worker messages
arrive for a supervisor terminal. The actual message payload stays in
the CAO inbox (SQLite) — this is a notify-only pointer so the supervisor
sees an instant teammate-message prompt and then drains via pull.

Feature flag: CAO_W2M_TEAMMATE_PUSH (default OFF).
Requires cc_team_inbox_path in the receiver terminal's metadata.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.clients.database import get_terminal_metadata, update_terminal_metadata
from cli_agent_orchestrator.models.inbox import InboxMessage
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# Fixed teammate name used as `from` in CC inbox entries (D5).
_TEAMMATE_FROM = "cao-bridge"

# Max chars of message body included in notification text.
_TEXT_PREVIEW_CHARS = 200

# Max chars for the summary field (shown in teammate-message header).
_SUMMARY_MAX_CHARS = 120

# Lockfile retry parameters.
_LOCK_MAX_RETRIES = 10
_LOCK_BACKOFF_MIN_MS = 5
_LOCK_BACKOFF_MAX_MS = 100
_LOCK_STALE_SECONDS = 5.0

# In-memory dedup high-water per terminal (SHOULD-2 fallback).
_last_notified: Dict[str, int] = {}


def _should_teammate_push(terminal_id: str) -> bool:
    """Return True if both the feature flag and inbox path are configured."""
    if not ConfigService.get("supervisor.teammate_push"):
        return False
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        return False
    md = metadata.get("metadata") or {}
    return bool(md.get("cc_team_inbox_path"))


def _resolve_inbox_path(terminal_id: str) -> Optional[Path]:
    """Resolve and expand the CC inbox path from terminal metadata."""
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        return None
    md = metadata.get("metadata") or {}
    raw = md.get("cc_team_inbox_path")
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def _get_last_notified_id(terminal_id: str) -> int:
    """Read last_notified_inbox_id from terminal metadata (SHOULD-2).

    Falls back to in-memory dict on read failure (degraded mode).
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            md = metadata.get("metadata") or {}
            stored = md.get("last_notified_inbox_id")
            if stored is not None:
                return int(stored)
    except Exception:
        pass
    return _last_notified.get(terminal_id, 0)


def _persist_last_notified_id(terminal_id: str, message_id: int) -> None:
    """Persist last_notified_inbox_id in terminal metadata (best-effort, SHOULD-2)."""
    _last_notified[terminal_id] = message_id
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            md = metadata.get("metadata") or {}
            md["last_notified_inbox_id"] = message_id
            update_terminal_metadata(terminal_id, md)
    except Exception as e:
        logger.debug(f"teammate_push: best-effort persist failed for {terminal_id}: {e}")


def _acquire_lockfile(lock_path: Path) -> Optional[int]:
    """Acquire a lockfile via O_CREAT|O_EXCL with retry + stale-lock handling.

    Returns the fd on success, None on failure after all retries.
    Implements SHOULD-1: fstat/stat inode verification after stale-lock force-remove.
    """
    for attempt in range(_LOCK_MAX_RETRIES):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            return fd
        except FileExistsError:
            # Check if stale (older than threshold).
            try:
                stat_result = os.stat(str(lock_path))
                age = time.time() - stat_result.st_mtime
                if age > _LOCK_STALE_SECONDS:
                    # Force-remove stale lock and recreate (SHOULD-1).
                    try:
                        os.unlink(str(lock_path))
                    except FileNotFoundError:
                        pass
                    # Attempt to create the lock after stale removal.
                    try:
                        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    except FileExistsError:
                        # Another writer recreated between our unlink and create.
                        _backoff()
                        continue
                    # SHOULD-1 TOCTOU verify: fstat fd vs stat path inode.
                    try:
                        fd_stat = os.fstat(fd)
                        path_stat = os.stat(str(lock_path))
                        if fd_stat.st_ino != path_stat.st_ino or fd_stat.st_dev != path_stat.st_dev:
                            # Inode mismatch — another writer owns the lock.
                            os.close(fd)
                            _backoff()
                            continue
                    except OSError:
                        # stat failed — lock path gone; close fd, retry.
                        os.close(fd)
                        _backoff()
                        continue
                    return fd
            except FileNotFoundError:
                # Lock disappeared between our open attempt and stat — retry immediately.
                continue
            except OSError:
                pass
            _backoff()
        except OSError as e:
            logger.debug(f"teammate_push: lockfile open error: {e}")
            _backoff()
    return None


def _backoff() -> None:
    """Random backoff between retries."""
    ms = random.randint(_LOCK_BACKOFF_MIN_MS, _LOCK_BACKOFF_MAX_MS)
    time.sleep(ms / 1000.0)


def _release_lockfile(fd: int, lock_path: Path) -> None:
    """Release a held lockfile."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


def _build_entry(worker_name: str, message_preview: str, msg_count: int) -> Dict[str, Any]:
    """Build a CC inbox entry per the pinned schema (msgV:1)."""
    text_body = (
        f"[CAO:{worker_name}] {message_preview[:_TEXT_PREVIEW_CHARS]}\n\n"
        f"---\n{msg_count} message(s) ready. Drain: list_messages \u2192 ack_messages"
    )
    summary_raw = f"{worker_name}: {message_preview[:80]}"
    summary = summary_raw[:_SUMMARY_MAX_CHARS]
    return {
        "type": "message",
        "from": _TEAMMATE_FROM,
        "text": text_body,
        "timestamp": _iso_now(),
        "summary": summary,
        "read": False,
        "msgV": 1,
        "msg_id": str(uuid.uuid4()),
    }


def _iso_now() -> str:
    """Return current UTC time in ISO8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _write_inbox_entry(inbox_path: Path, entry: Dict[str, Any]) -> bool:
    """Write an entry to the CC inbox file under lockfile protection.

    Returns True on success, False on failure (graceful fallback).
    """
    lock_path = Path(str(inbox_path) + ".lock")

    # Ensure parent directory exists (lazy creation per D1).
    try:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"teammate_push: cannot create inbox dir {inbox_path.parent}: {e}")
        return False

    fd = _acquire_lockfile(lock_path)
    if fd is None:
        logger.warning(f"teammate_push: failed to acquire lock {lock_path} after retries")
        return False

    try:
        # Read existing array (or create empty).
        entries: List[Dict[str, Any]] = []
        if inbox_path.exists():
            try:
                raw = inbox_path.read_text(encoding="utf-8")
                if raw.strip():
                    parsed = json.loads(raw)
                    if not isinstance(parsed, list):
                        logger.warning(
                            f"teammate_push: inbox file is not a JSON array, aborting write"
                        )
                        return False
                    entries = parsed
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"teammate_push: failed to read/parse inbox file: {e}")
                return False

        # Append new entry.
        entries.append(entry)

        # Atomic write: tempfile + os.replace.
        tmp_path = inbox_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(inbox_path))
        except OSError as e:
            logger.warning(f"teammate_push: atomic write failed: {e}")
            # Clean up temp if it exists.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

        return True
    finally:
        _release_lockfile(fd, lock_path)


def attempt_teammate_push(terminal_id: str, messages: List[InboxMessage]) -> bool:
    """Attempt to push a notification entry to the CC native inbox.

    Called as a best-effort side-effect from the pull-mode gate in
    inbox_service.deliver_pending. On failure, logs and returns False —
    the pull channel remains authoritative.

    Returns True if a notification was written, False otherwise.
    """
    if not messages:
        return False

    inbox_path = _resolve_inbox_path(terminal_id)
    if inbox_path is None:
        logger.warning(
            "teammate_push_outcome",
            extra={
                "event": "teammate_push_no_inbox",
                "terminal_id": terminal_id,
                "reason": "inbox_path_not_resolved",
            },
        )
        return False

    # Dedup: only notify for messages with id > last_notified (SHOULD-2 / Risk#3).
    last_notified_id = _get_last_notified_id(terminal_id)
    new_messages = [m for m in messages if m.id > last_notified_id]
    if not new_messages:
        return False

    # Shape notification: batch all new messages into one entry.
    # Use the first message's sender as representative worker name.
    first_msg = new_messages[0]
    worker_name = first_msg.sender_id
    message_preview = first_msg.message.split("\n", 1)[0] if first_msg.message else ""

    entry = _build_entry(worker_name, message_preview, len(new_messages))

    success = _write_inbox_entry(inbox_path, entry)
    if success:
        # Persist high-water mark (best-effort, SHOULD-2).
        max_id = max(m.id for m in new_messages)
        _persist_last_notified_id(terminal_id, max_id)
        logger.info(
            "teammate_push_outcome",
            extra={
                "event": "teammate_push_ok",
                "terminal_id": terminal_id,
                "msg_count": len(new_messages),
                "msg_ids": [m.id for m in new_messages],
                "high_water": max_id,
            },
        )
    else:
        logger.warning(
            "teammate_push_outcome",
            extra={
                "event": "teammate_push_fail",
                "terminal_id": terminal_id,
                "reason": "write_failed",
                "msg_ids": [m.id for m in new_messages],
            },
        )

    return success
