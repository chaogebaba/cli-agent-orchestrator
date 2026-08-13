"""F136 — Single-writer callback notification service (D1/D11/D12).

Owns the only native CC inbox writer for supervisor mailbox notifications.
Deterministic identity per (mailbox_id, message_id) pair ensures idempotent
durable writes across retry/rebind/path changes.

Legacy teammate_push functions are retained for backward compat but the
F123 insert-path direct append (attempt_teammate_push_on_insert) is retired.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.clients.database import (
    get_mailbox_consumption_cursor,
    get_terminal_last_notified_inbox_id,
    get_terminal_metadata,
    set_terminal_last_notified_inbox_id,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# D11: Pinned deterministic namespace (frozen forever).
# Derivation: UUID5 of "https://github.com/awslabs/cli-agent-orchestrator/f136-supervisor-callback"
F136_CALLBACK_NAMESPACE = uuid.UUID("e5c543e1-dd9d-57af-8cd1-6fbf54ada4fb")

# Fixed teammate name used as `from` in CC inbox entries.
_TEAMMATE_FROM = "cao-bridge"

# Max chars of message body included in notification text.
_TEXT_PREVIEW_CHARS = 200

# Summary field max chars.
_SUMMARY_MAX_CHARS = 120

# Lockfile parameters with deadline support (D14).
_LOCK_BACKOFF_MIN_MS = 5
_LOCK_BACKOFF_MAX_MS = 50
_LOCK_STALE_SECONDS = 5.0

# In-memory dedup high-water per terminal (legacy SHOULD-2 fallback).
_last_notified: Dict[str, int] = {}


# ---------------------------------------------------------------------------
# D11: Deterministic identity
# ---------------------------------------------------------------------------


def callback_notification_id(mailbox_id: str, message_id: int) -> str:
    """Deterministic native inbox msg_id for a (mailbox, row) pair.

    Terminal ID, generation, path, and path version are excluded -- the same
    logical row uses the same identity across retry/rebind/path changes.
    """
    return str(uuid.uuid5(F136_CALLBACK_NAMESPACE, f"{mailbox_id}:{message_id}"))


# ---------------------------------------------------------------------------
# D12: Typed write result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeInboxWriteResult:
    kind: str  # "written", "already_present", "retryable_failure", "identity_conflict"
    reason: str = ""


# ---------------------------------------------------------------------------
# D12: Public durable writer
# ---------------------------------------------------------------------------


def write_supervisor_callback_notification(
    *,
    inbox_path: Path,
    mailbox_id: str,
    message: InboxMessage,
    deadline_mono: float | None = None,
) -> NativeInboxWriteResult:
    """D1/D12: The single public native callback writer.

    Shapes immutable content from mailbox ID and row, acquires lockfile within
    deadline, reads/validates JSON, deduplicates by deterministic msg_id, and
    performs atomic durable write.
    """
    # F178-S1: resolve symlinks so os.replace targets the real file, not the link.
    inbox_path = inbox_path.resolve()
    msg_id = callback_notification_id(mailbox_id, message.id)

    # Shape immutable entry content
    worker_name = message.sender_id
    message_preview = message.message.split("\n", 1)[0] if message.message else ""
    created_at_str = (
        message.created_at.isoformat()
        if message.created_at
        else datetime.now(timezone.utc).isoformat()
    )
    text_body = (
        f"[CAO:{worker_name}] {message_preview[:_TEXT_PREVIEW_CHARS]}\n\n"
        f"---\nMessage {message.id} ready. Drain: list_messages -> ack_messages"
    )
    summary_raw = f"{worker_name}: {message_preview[:80]}"
    summary = summary_raw[:_SUMMARY_MAX_CHARS]

    entry = {
        "type": "message",
        "from": _TEAMMATE_FROM,
        "text": text_body,
        "timestamp": created_at_str,
        "summary": summary,
        "read": False,
        "msgV": 1,
        "msg_id": msg_id,
    }

    # Ensure parent directory exists
    try:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return NativeInboxWriteResult(kind="retryable_failure", reason=f"mkdir: {e}")

    lock_path = Path(str(inbox_path) + ".lock")
    fd = _acquire_lockfile_deadline(lock_path, deadline_mono)
    if fd is None:
        return NativeInboxWriteResult(kind="retryable_failure", reason="lock_timeout")

    try:
        # Read existing array
        entries: List[Dict[str, Any]] = []
        if inbox_path.exists():
            try:
                raw = inbox_path.read_text(encoding="utf-8")
                if raw.strip():
                    parsed = json.loads(raw)
                    if not isinstance(parsed, list):
                        return NativeInboxWriteResult(
                            kind="retryable_failure", reason="inbox_not_array"
                        )
                    entries = parsed
            except (json.JSONDecodeError, OSError) as e:
                return NativeInboxWriteResult(kind="retryable_failure", reason=f"read: {e}")

        # D12 step 4-6: search for existing entry with same msg_id
        for existing in entries:
            if existing.get("msg_id") == msg_id:
                # Check immutable content match
                if (
                    existing.get("text") == entry["text"]
                    and existing.get("timestamp") == entry["timestamp"]
                ):
                    # D12 step 5: reconfirm durability (fsync file + parent)
                    try:
                        _fsync_path(inbox_path)
                        _fsync_dir(inbox_path.parent)
                    except OSError as e:
                        return NativeInboxWriteResult(
                            kind="retryable_failure",
                            reason=f"reconfirm_fsync: {e}",
                        )
                    return NativeInboxWriteResult(kind="already_present")
                else:
                    # D12 step 6: identity conflict
                    return NativeInboxWriteResult(
                        kind="identity_conflict",
                        reason=f"msg_id={msg_id} content_mismatch",
                    )

        # D12 step 7: append and atomic durable write
        entries.append(entry)
        tmp_path = inbox_path.with_suffix(".tmp")
        try:
            tmp_fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                data = json.dumps(entries, indent=2).encode("utf-8")
                os.write(tmp_fd, data)
                os.fsync(tmp_fd)
            finally:
                os.close(tmp_fd)
            os.replace(str(tmp_path), str(inbox_path))
            _fsync_dir(inbox_path.parent)
        except OSError as e:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return NativeInboxWriteResult(kind="retryable_failure", reason=f"write: {e}")

        return NativeInboxWriteResult(kind="written")
    finally:
        _release_lockfile(fd, lock_path)


# ---------------------------------------------------------------------------
# File durability helpers
# ---------------------------------------------------------------------------


def _fsync_path(path: Path) -> None:
    """Open, fsync, and close a file path."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(dir_path: Path) -> None:
    """Fsync a directory for metadata durability."""
    fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Deadline-aware lockfile (D14)
# ---------------------------------------------------------------------------


def _acquire_lockfile_deadline(lock_path: Path, deadline_mono: float | None) -> Optional[int]:
    """Acquire lockfile with optional monotonic deadline.

    Returns fd on success, None on timeout/failure.
    """
    while True:
        if deadline_mono is not None and time.monotonic() >= deadline_mono:
            return None
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            return fd
        except FileExistsError:
            try:
                stat_result = os.stat(str(lock_path))
                age = time.time() - stat_result.st_mtime
                if age > _LOCK_STALE_SECONDS:
                    try:
                        os.unlink(str(lock_path))
                    except FileNotFoundError:
                        pass
                    try:
                        fd = os.open(
                            str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                        )
                    except FileExistsError:
                        _backoff_deadline(deadline_mono)
                        continue
                    # TOCTOU verify
                    try:
                        fd_stat = os.fstat(fd)
                        path_stat = os.stat(str(lock_path))
                        if (
                            fd_stat.st_ino != path_stat.st_ino
                            or fd_stat.st_dev != path_stat.st_dev
                        ):
                            os.close(fd)
                            _backoff_deadline(deadline_mono)
                            continue
                    except OSError:
                        os.close(fd)
                        _backoff_deadline(deadline_mono)
                        continue
                    return fd
            except FileNotFoundError:
                continue
            except OSError:
                pass
            _backoff_deadline(deadline_mono)
        except OSError as e:
            logger.debug(f"f136_lockfile_error: {e}")
            _backoff_deadline(deadline_mono)


def _backoff_deadline(deadline_mono: float | None) -> None:
    """Random backoff respecting deadline."""
    if deadline_mono is not None:
        remaining = deadline_mono - time.monotonic()
        if remaining <= 0:
            return
        max_ms = min(_LOCK_BACKOFF_MAX_MS, int(remaining * 1000))
        if max_ms < _LOCK_BACKOFF_MIN_MS:
            return
        ms = random.randint(_LOCK_BACKOFF_MIN_MS, max_ms)
    else:
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


# ---------------------------------------------------------------------------
# Legacy compat (retained for existing test imports / backward compat)
# ---------------------------------------------------------------------------

# Legacy lockfile API alias for tests
_LOCK_MAX_RETRIES = 10
_LOCK_BACKOFF_MIN_MS_LEGACY = 5
_LOCK_BACKOFF_MAX_MS_LEGACY = 100


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
    """Read last_notified_inbox_id from dedicated DB column (F175: clobber-proof).

    Falls back to in-memory dict for hot-path speed when DB has no value yet.
    """
    try:
        stored = get_terminal_last_notified_inbox_id(terminal_id)
        if stored > 0:
            return stored
    except Exception:
        pass
    return _last_notified.get(terminal_id, 0)


def _persist_last_notified_id(terminal_id: str, message_id: int) -> None:
    """Persist last_notified_inbox_id in dedicated DB column (F175: clobber-proof)."""
    _last_notified[terminal_id] = message_id
    try:
        set_terminal_last_notified_inbox_id(terminal_id, message_id)
    except Exception as e:
        logger.debug(f"teammate_push: best-effort persist failed for {terminal_id}: {e}")


def _build_entry(worker_name: str, message_preview: str, msg_count: int, *, mailbox_id: str = "", first_row_id: int = 0) -> Dict[str, Any]:
    """Build a CC inbox entry per the pinned schema (msgV:1).

    F175: msg_id is deterministic from (mailbox_id, first_row_id) when provided,
    so duplicate appends for the same logical notification become already_present
    at the file level even when the high-water was clobbered.
    """
    text_body = (
        f"[CAO:{worker_name}] {message_preview[:_TEXT_PREVIEW_CHARS]}\n\n"
        f"---\n{msg_count} message(s) ready. Drain: list_messages -> ack_messages"
    )
    summary_raw = f"{worker_name}: {message_preview[:80]}"
    summary = summary_raw[:_SUMMARY_MAX_CHARS]

    # F175: deterministic msg_id when mailbox context is available
    if mailbox_id and first_row_id > 0:
        msg_id = callback_notification_id(mailbox_id, first_row_id)
    else:
        msg_id = str(uuid.uuid4())

    return {
        "type": "message",
        "from": _TEAMMATE_FROM,
        "text": text_body,
        "timestamp": _iso_now(),
        "summary": summary,
        "read": False,
        "msgV": 1,
        "msg_id": msg_id,
    }


def _iso_now() -> str:
    """Return current UTC time in ISO8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _write_inbox_entry(inbox_path: Path, entry: Dict[str, Any]) -> bool:
    """Write an entry to the CC inbox file under lockfile protection (legacy).

    F175: deduplicates by msg_id — if an entry with the same msg_id already
    exists in the file, returns True (idempotent success) without appending.
    """
    # F178-S1: resolve symlinks so os.replace targets the real file, not the link.
    inbox_path = inbox_path.resolve()
    lock_path = Path(str(inbox_path) + ".lock")
    try:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"teammate_push: cannot create inbox dir {inbox_path.parent}: {e}")
        return False
    fd = _acquire_lockfile_deadline(lock_path, time.monotonic() + 1.0)
    if fd is None:
        logger.warning(f"teammate_push: failed to acquire lock {lock_path} after retries")
        return False
    try:
        entries_list: List[Dict[str, Any]] = []
        if inbox_path.exists():
            try:
                raw = inbox_path.read_text(encoding="utf-8")
                if raw.strip():
                    parsed = json.loads(raw)
                    if not isinstance(parsed, list):
                        return False
                    entries_list = parsed
            except (json.JSONDecodeError, OSError):
                return False

        # F175: file-level dedup by msg_id — prevent duplicate appends
        entry_msg_id = entry.get("msg_id")
        if entry_msg_id:
            for existing in entries_list:
                if existing.get("msg_id") == entry_msg_id:
                    # Already present — idempotent success
                    return True

        entries_list.append(entry)
        tmp_path = inbox_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(entries_list, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(inbox_path))
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True
    finally:
        _release_lockfile(fd, lock_path)


# Legacy aliases kept for tests
def _acquire_lockfile(lock_path: Path, deadline_mono: float | None = None) -> Optional[int]:
    """Legacy-compatible lockfile acquire with bounded retries when no deadline."""
    if deadline_mono is None:
        # Legacy behavior: bounded by ~500ms (10 retries * 50ms max)
        deadline_mono = time.monotonic() + 0.5
    return _acquire_lockfile_deadline(lock_path, deadline_mono)


_backoff = lambda: _backoff_deadline(None)


# ---------------------------------------------------------------------------
# fx158 D4: Reporting push outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushOutcome:
    """Structured result of a teammate push attempt (fx158 D4)."""

    pushed: bool
    reason: str  # closed set: empty_batch, no_inbox_path, already_notified, consumed, write_failed, pushed
    message_ids: tuple  # diagnostic only (N1)


def attempt_teammate_push_reported(terminal_id: str, messages: List[InboxMessage], *, mailbox_id: str = "") -> PushOutcome:
    """Push a notification entry to the CC native inbox with structured outcome.

    Contains the body formerly in attempt_teammate_push, with each early return
    labelled by a reason from a closed set (fx158 D4).
    """
    ids = tuple(m.id for m in messages)
    if not messages:
        return PushOutcome(pushed=False, reason="empty_batch", message_ids=ids)
    inbox_path = _resolve_inbox_path(terminal_id)
    if inbox_path is None:
        return PushOutcome(pushed=False, reason="no_inbox_path", message_ids=ids)
    last_notified_id = _get_last_notified_id(terminal_id)
    new_messages = [m for m in messages if m.id > last_notified_id]
    if not new_messages:
        return PushOutcome(pushed=False, reason="already_notified", message_ids=ids)
    # D3 (fx157): send-time recount against consumption cursor
    cursor = get_mailbox_consumption_cursor(terminal_id)
    if cursor is not None:
        new_messages = [m for m in new_messages if m.id > cursor]
        if not new_messages:
            return PushOutcome(pushed=False, reason="consumed", message_ids=ids)
    first_msg = new_messages[0]
    worker_name = first_msg.sender_id
    message_preview = first_msg.message.split("\n", 1)[0] if first_msg.message else ""
    # F175: derive mailbox_id for deterministic msg_id when not explicitly passed
    _mbid = mailbox_id or getattr(first_msg, "logical_receiver_id", "") or ""
    entry = _build_entry(worker_name, message_preview, len(new_messages),
                         mailbox_id=_mbid, first_row_id=first_msg.id)
    success = _write_inbox_entry(inbox_path, entry)
    if success:
        max_id = max(m.id for m in new_messages)
        _persist_last_notified_id(terminal_id, max_id)
        return PushOutcome(pushed=True, reason="pushed", message_ids=tuple(m.id for m in new_messages))
    return PushOutcome(pushed=False, reason="write_failed", message_ids=ids)


def attempt_teammate_push(terminal_id: str, messages: List[InboxMessage]) -> bool:
    """Legacy: push a notification entry to the CC native inbox.

    Retained for backward compat with inbox_service pull-gate path.
    F136 callback runner replaces this for the main notification flow.
    """
    return attempt_teammate_push_reported(terminal_id, messages).pushed


def attempt_teammate_push_on_insert(terminal_id: str, messages: List[InboxMessage]) -> bool:
    """F136: RETIRED. Returns False unconditionally.

    The F123 direct-append path is replaced by the F136 callback runner.
    Signature preserved to prevent import errors at call sites being migrated.
    """
    # D1: No other service imports a private file primitive. Insert path
    # now signals request_delivery instead of appending directly.
    return False


# ---------------------------------------------------------------------------
# F178: Mark CC inbox entries as read on CAO-side ack
# ---------------------------------------------------------------------------


def mark_cc_inbox_entries_read(
    *,
    inbox_path: Path,
    mailbox_id: str,
    acked_row_ids: List[int],
) -> int:
    """Mark CC inbox entries corresponding to acked CAO rows as read.

    Correlates via deterministic msg_id = callback_notification_id(mailbox_id, row_id).
    Fail-safe: never truncates or rewrites entries it did not author (shared file).
    Returns the number of entries marked read.

    Safety invariants:
    - Entry absent: no-op (count unaffected).
    - File locked: uses existing lockfile discipline with deadline.
    - Malformed file: returns 0 (fail-safe, never loses data).
    - Only matches on msg_id from _TEAMMATE_FROM; foreign entries untouched.
    """
    if not acked_row_ids:
        return 0

    # F178-S1: resolve symlinks so os.replace targets the real file, not the link.
    inbox_path = inbox_path.resolve()

    # Build set of msg_ids to mark read
    target_msg_ids = {
        callback_notification_id(mailbox_id, row_id)
        for row_id in acked_row_ids
    }

    lock_path = Path(str(inbox_path) + ".lock")
    fd = _acquire_lockfile_deadline(lock_path, time.monotonic() + 2.0)
    if fd is None:
        logger.debug("f178: lock timeout marking cc inbox entries read")
        return 0

    try:
        if not inbox_path.exists():
            return 0

        try:
            raw = inbox_path.read_text(encoding="utf-8")
            if not raw.strip():
                return 0
            entries: List[Dict[str, Any]] = json.loads(raw)
            if not isinstance(entries, list):
                return 0
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("f178: malformed cc inbox file, skipping: %s", e)
            return 0

        # Mark matching entries as read
        marked = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_msg_id = entry.get("msg_id")
            if entry_msg_id and entry_msg_id in target_msg_ids:
                if entry.get("from") == _TEAMMATE_FROM and not entry.get("read"):
                    entry["read"] = True
                    marked += 1

        if marked == 0:
            return 0

        # Atomic durable write (same discipline as _write_inbox_entry)
        tmp_path = inbox_path.with_suffix(".tmp")
        try:
            tmp_fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                data = json.dumps(entries, indent=2).encode("utf-8")
                os.write(tmp_fd, data)
                os.fsync(tmp_fd)
            finally:
                os.close(tmp_fd)
            os.replace(str(tmp_path), str(inbox_path))
            _fsync_dir(inbox_path.parent)
        except OSError as e:
            logger.debug("f178: write failed marking entries read: %s", e)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return 0

        logger.debug("f178: marked %d cc inbox entries read for mailbox %s", marked, mailbox_id)
        return marked
    finally:
        _release_lockfile(fd, lock_path)
