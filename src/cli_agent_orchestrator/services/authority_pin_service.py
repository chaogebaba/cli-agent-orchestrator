"""Process-bound authority-pin registry operations."""

from __future__ import annotations

import hashlib
import errno
import os
import re
import stat
from collections.abc import Mapping, Sequence
from typing import Any, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from cli_agent_orchestrator.clients import database as dbmod

_TERMINAL_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUSY_TIMEOUT_MS = 1000
_Result = TypeVar("_Result")


class AuthorityPinError(ValueError):
    """Stable domain error returned by the MCP wrappers."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _terminal_principal() -> str:
    principal = os.environ.get("CAO_TERMINAL_ID")
    if not principal:
        raise AuthorityPinError("missing_terminal_id")
    if not _TERMINAL_ID_RE.fullmatch(principal):
        raise AuthorityPinError("missing_terminal_id")
    return principal


def _validate_worker_terminal_id(worker_terminal_id: str) -> str:
    if not isinstance(worker_terminal_id, str) or not _TERMINAL_ID_RE.fullmatch(worker_terminal_id):
        raise AuthorityPinError("unknown_worker")
    return worker_terminal_id


def _validate_path(file_path: Any) -> str:
    if not isinstance(file_path, str) or not file_path or not os.path.isabs(file_path):
        raise AuthorityPinError("path_not_absolute")
    return file_path


def _validate_sha256(sha256: Any) -> str:
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise AuthorityPinError("invalid_sha256")
    return sha256


def _validate_pins(pins: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    if not isinstance(pins, Sequence) or isinstance(pins, (str, bytes)) or not pins:
        raise AuthorityPinError("empty_pin_list")
    validated: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pin in pins:
        if not isinstance(pin, Mapping):
            raise AuthorityPinError("invalid_sha256")
        file_path = _validate_path(pin.get("file_path"))
        sha256 = _validate_sha256(pin.get("sha256"))
        if file_path in seen:
            raise AuthorityPinError("duplicate_path")
        seen.add(file_path)
        validated.append((file_path, sha256))
    return validated


def _run_immediate(operation: Callable[[Any], _Result]) -> _Result:
    """Run one serialized SQLite write with the pinned one-second busy bound."""
    db = dbmod.SessionLocal()
    prior_timeout: int | None = None
    try:
        prior_timeout = int(db.execute(text("PRAGMA busy_timeout")).scalar() or 0)
        db.execute(text(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}"))
        db.execute(text("BEGIN IMMEDIATE"))
        result = operation(db)
        db.commit()
        return result
    except OperationalError as exc:
        db.rollback()
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise AuthorityPinError("db_busy") from exc
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        if prior_timeout is not None:
            try:
                db.execute(text(f"PRAGMA busy_timeout={prior_timeout}"))
            except Exception:
                pass
        db.close()


def _assert_owner(db: Any, principal: str, worker_terminal_id: str) -> None:
    worker = db.query(dbmod.TerminalModel).filter_by(id=worker_terminal_id).one_or_none()
    if worker is None:
        raise AuthorityPinError("unknown_worker")
    if not dbmod.callback_barrier_dispatch_allowed_in_db(db, principal, worker_terminal_id):
        raise AuthorityPinError("not_owner")


def _chain(db: Any, task_key: str, file_path: str) -> list[dict[str, Any]]:
    rows = (
        db.query(dbmod.AuthorityPinModel)
        .filter_by(task_key=task_key, file_path=file_path)
        .order_by(dbmod.AuthorityPinModel.version.asc())
        .all()
    )
    return [
        {
            "version": row.version,
            "sha256": row.sha256,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def pin_authority(
    worker_terminal_id: str,
    pins: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Register version-one pins atomically for one owned worker."""
    worker_terminal_id = _validate_worker_terminal_id(worker_terminal_id)
    validated = _validate_pins(pins)
    principal = _terminal_principal()

    def operation(db: Any) -> dict[str, Any]:
        _assert_owner(db, principal, worker_terminal_id)
        for file_path, _ in validated:
            if (
                db.query(dbmod.AuthorityPinModel.id)
                .filter_by(task_key=worker_terminal_id, file_path=file_path)
                .first()
                is not None
            ):
                raise AuthorityPinError("already_pinned")
        for file_path, sha256 in validated:
            db.add(
                dbmod.AuthorityPinModel(
                    task_key=worker_terminal_id,
                    file_path=file_path,
                    sha256=sha256,
                    version=1,
                    registered_by=principal,
                )
            )
        db.flush()
        return {
            "task_key": worker_terminal_id,
            "results": [
                {
                    "file_path": file_path,
                    "current_version": 1,
                    "chain": _chain(db, worker_terminal_id, file_path),
                }
                for file_path, _ in validated
            ],
        }

    return _run_immediate(operation)


def update_pin(worker_terminal_id: str, file_path: str, sha256: str) -> dict[str, Any]:
    """Append a new version for one existing authority pin."""
    worker_terminal_id = _validate_worker_terminal_id(worker_terminal_id)
    file_path = _validate_path(file_path)
    sha256 = _validate_sha256(sha256)
    principal = _terminal_principal()

    def operation(db: Any) -> dict[str, Any]:
        _assert_owner(db, principal, worker_terminal_id)
        current = (
            db.query(dbmod.AuthorityPinModel)
            .filter_by(task_key=worker_terminal_id, file_path=file_path)
            .order_by(dbmod.AuthorityPinModel.version.desc())
            .first()
        )
        if current is None:
            raise AuthorityPinError("already_pinned")
        next_version = current.version + 1
        db.add(
            dbmod.AuthorityPinModel(
                task_key=worker_terminal_id,
                file_path=file_path,
                sha256=sha256,
                version=next_version,
                registered_by=principal,
            )
        )
        db.flush()
        return {
            "task_key": worker_terminal_id,
            "file_path": file_path,
            "current_version": next_version,
            "chain": _chain(db, worker_terminal_id, file_path),
        }

    return _run_immediate(operation)


def _hash_file(file_path: str) -> tuple[str | None, str | None]:
    """Hash through the OS path resolver and classify filesystem failures."""
    try:
        file_stat = os.stat(file_path)
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        if getattr(exc, "errno", None) in {errno.ELOOP, errno.ENOENT}:
            return None, "missing"
        return None, "unreadable"
    if not stat.S_ISREG(file_stat.st_mode):
        return None, "not_regular"
    digest = hashlib.sha256()
    try:
        with open(file_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return None, "missing"
    except IsADirectoryError:
        return None, "not_regular"
    except OSError:
        return None, "unreadable"
    return digest.hexdigest(), None


def verify_pin(file_path: str) -> dict[str, Any]:
    """Hash one file locally and return the stateless current-pin verdict."""
    file_path = _validate_path(file_path)
    principal = _terminal_principal()
    with dbmod.SessionLocal() as db:
        rows = (
            db.query(dbmod.AuthorityPinModel)
            .filter_by(task_key=principal, file_path=file_path)
            .order_by(dbmod.AuthorityPinModel.version.asc())
            .all()
        )
    if not rows:
        return {"verdict": "UNPINNED"}

    observed, reason = _hash_file(file_path)
    current = rows[-1]
    chain = [
        {"version": row.version, "sha256": row.sha256, "created_at": row.created_at.isoformat()}
        for row in rows
    ]
    if reason is not None:
        return {
            "verdict": "DRIFT",
            "expected_sha": current.sha256,
            "observed_sha": None,
            "reason": reason,
        }
    assert observed is not None
    if observed == current.sha256:
        if current.version == 1:
            return {"verdict": "VALID", "version": 1}
        return {
            "verdict": "SUPERSEDED",
            "chain": chain,
            "current_sha": current.sha256,
            "current_version": current.version,
        }
    return {
        "verdict": "DRIFT",
        "expected_sha": current.sha256,
        "observed_sha": observed,
        "reason": "content",
    }
