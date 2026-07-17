"""Durable retry files for deferred-init failure claims that cannot reach SQLite."""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.clients.database import claim_deferred_init_failure
from cli_agent_orchestrator.constants import DEFERRED_DEADLETTER_DIR

logger = logging.getLogger(__name__)


def _ensure_directory() -> Path:
    path = DEFERRED_DEADLETTER_DIR
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("deferred_deadletter_dir_not_directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_deferred_failure_deadletter(payload: dict[str, Any]) -> Path:
    """Atomically persist one 0600 JSON claim replay record."""
    directory = _ensure_directory()
    token = payload.get("failure_token")
    uuid.UUID(str(token))
    target = directory / f"deferred-init-{token}.json"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
            raise RuntimeError("deferred_deadletter_target_not_regular")
        return target
    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(fd)
        fd = -1
        os.replace(temporary, target)
        target.chmod(0o600)
        _fsync_directory(directory)
        return target
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _valid_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        uuid.UUID(str(value.get("failure_token")))
        uuid.UUID(str(value.get("owner_epoch")))
    except (ValueError, TypeError, AttributeError):
        return False
    return (
        isinstance(value.get("terminal_id"), str)
        and isinstance(value.get("notice"), str)
        and value.get("stage") in {"h3_claim", "pre_claim_validation"}
        and (value.get("caller_id") is None or isinstance(value.get("caller_id"), str))
    )


def replay_deferred_failure_deadletters() -> dict[str, int]:
    """Replay valid files through the idempotent failure-claim CAS."""
    directory = DEFERRED_DEADLETTER_DIR
    if not directory.exists():
        return {"replayed": 0, "archived": 0, "failed": 0}
    if directory.is_symlink() or not directory.is_dir():
        logger.critical("deferred_deadletter_replay_invalid_directory path=%s", directory)
        return {"replayed": 0, "archived": 0, "failed": 1}
    counts = {"replayed": 0, "archived": 0, "failed": 0}
    for path in sorted(directory.glob("deferred-init-*.json")):
        if path.is_symlink() or not path.is_file():
            counts["failed"] += 1
            logger.critical("deferred_deadletter_replay_invalid_file path=%s", path)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["failed"] += 1
            logger.critical("deferred_deadletter_replay_unreadable path=%s", path, exc_info=True)
            continue
        if not _valid_payload(payload):
            counts["failed"] += 1
            logger.critical("deferred_deadletter_replay_invalid_schema path=%s", path)
            continue
        try:
            result = claim_deferred_init_failure(
                payload["terminal_id"],
                caller_id=payload.get("caller_id"),
                failure_token=payload["failure_token"],
                notice=payload["notice"],
            )
        except Exception:
            counts["failed"] += 1
            logger.exception("deferred_deadletter_replay_db_failed path=%s", path)
            continue
        counts["replayed"] += 1
        if result.get("status") not in {
            "claimed_notified",
            "claimed_caller_gone",
            "already_claimed",
            "row_missing",
        }:
            counts["failed"] += 1
            logger.error(
                "deferred_deadletter_replay_unknown_result path=%s result=%s", path, result
            )
            continue
        done = path.with_suffix(path.suffix + ".done")
        os.replace(path, done)
        done.chmod(0o600)
        _fsync_directory(directory)
        counts["archived"] += 1
    return counts
