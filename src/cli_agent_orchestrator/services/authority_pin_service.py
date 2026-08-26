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
        if current.frozen:
            raise AuthorityPinError("frozen_pin_immutable")
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



# ─── F129: Frozen-pin registration and validation ───────────────────────────


from dataclasses import dataclass, field
from typing import Literal


@dataclass
class PinCheckResult:
    file_path: str
    verdict: Literal["VALID", "DRIFT"]
    expected: str
    observed: str | None
    reason: str | None  # "content", "missing", "unreadable", "not_regular"


@dataclass
class FrozenPinValidation:
    outcome: Literal["no_frozen_pins", "valid", "drift"]
    drifted: list[PinCheckResult] = field(default_factory=list)
    all_results: list[PinCheckResult] = field(default_factory=list)


def register_frozen_pins(
    db: Any,
    task_key: str,
    authority_files: Sequence[Mapping[str, Any]],
    registered_by: str,
) -> list[dict[str, Any]]:
    """Register frozen (immutable) authority pins inside an existing transaction.

    Called from terminal_service.create_terminal WITHIN the same DB transaction
    that creates the TerminalModel row. Validates each entry, server-hashes each
    file, compares to caller-supplied sha256, and inserts AuthorityPinModel rows
    with frozen=True.

    Raises AuthorityPinError on any validation or hash-verification failure; the
    caller is expected to ROLLBACK the enclosing transaction.
    """
    validated = _validate_pins(authority_files)

    results: list[dict[str, Any]] = []
    for file_path, expected_sha in validated:
        observed, reason = _hash_file(file_path)
        if reason is not None:
            raise AuthorityPinError(f"authority_hash_mismatch")
        if observed != expected_sha:
            raise AuthorityPinError("authority_hash_mismatch")
        db.add(
            dbmod.AuthorityPinModel(
                task_key=task_key,
                file_path=file_path,
                sha256=expected_sha,
                version=1,
                registered_by=registered_by,
                frozen=True,
            )
        )
        results.append({"file_path": file_path, "sha256": expected_sha, "version": 1})
    db.flush()
    return results


def rotate_frozen_pins(
    db: Any,
    task_key: str,
    authority_files: Sequence[Mapping[str, Any]],
    registered_by: str,
) -> list[dict[str, Any]]:
    """Atomically supersede all existing frozen pins for a warm-reused terminal.

    F495: When a warm reviewer is re-dispatched with new authority_files, the
    old frozen pins must be replaced so that validate_frozen_pins checks the
    CURRENT artifact, not a stale one from the previous dispatch.

    Semantics:
      1. Delete ALL existing frozen pins for this task_key (any file_path).
      2. Register new frozen pins at version=1 (fresh chain).
      3. Validate new files against caller-supplied sha256 (same as register).

    Must be called INSIDE the same transaction that dispatches the new task.
    Raises AuthorityPinError on validation/hash failure (caller should ROLLBACK).
    """
    validated = _validate_pins(authority_files)

    # Step 1: Remove all prior frozen pins for this terminal
    db.query(dbmod.AuthorityPinModel).filter_by(
        task_key=task_key, frozen=True
    ).delete(synchronize_session=False)

    # Step 2: Register new frozen pins
    results: list[dict[str, Any]] = []
    for file_path, expected_sha in validated:
        observed, reason = _hash_file(file_path)
        if reason is not None:
            raise AuthorityPinError("authority_hash_mismatch")
        if observed != expected_sha:
            raise AuthorityPinError("authority_hash_mismatch")
        db.add(
            dbmod.AuthorityPinModel(
                task_key=task_key,
                file_path=file_path,
                sha256=expected_sha,
                version=1,
                registered_by=registered_by,
                frozen=True,
            )
        )
        results.append({"file_path": file_path, "sha256": expected_sha, "version": 1})
    db.flush()
    return results


def validate_frozen_pins(db: Any, sender_id: str) -> FrozenPinValidation:
    """Validate all frozen pins for a sender BEFORE inbox row creation.

    Called from create_inbox_message_endpoint BEFORE the mb_/direct branch.
    Only invoked when sender_id resolves to a TerminalModel row (genuine
    terminal principals).

    Returns FrozenPinValidation with verdict and optional attestation/drift info.
    """
    frozen_rows = (
        db.query(dbmod.AuthorityPinModel)
        .filter_by(task_key=sender_id, frozen=True)
        .order_by(dbmod.AuthorityPinModel.file_path, dbmod.AuthorityPinModel.version.desc())
        .all()
    )

    if not frozen_rows:
        return FrozenPinValidation(outcome="no_frozen_pins")

    # Group by file_path, take highest version per file
    current_pins: dict[str, Any] = {}
    for row in frozen_rows:
        if row.file_path not in current_pins:
            current_pins[row.file_path] = row

    # Hash each file and compare
    results: list[PinCheckResult] = []
    for file_path, pin in current_pins.items():
        observed_sha, reason = _hash_file(file_path)
        if reason is not None:
            results.append(PinCheckResult(
                file_path=file_path, verdict="DRIFT",
                expected=pin.sha256, observed=None, reason=reason,
            ))
        elif observed_sha != pin.sha256:
            results.append(PinCheckResult(
                file_path=file_path, verdict="DRIFT",
                expected=pin.sha256, observed=observed_sha, reason="content",
            ))
        else:
            results.append(PinCheckResult(
                file_path=file_path, verdict="VALID",
                expected=pin.sha256, observed=observed_sha, reason=None,
            ))

    drifted = [r for r in results if r.verdict == "DRIFT"]
    if drifted:
        return FrozenPinValidation(outcome="drift", drifted=drifted, all_results=results)

    return FrozenPinValidation(outcome="valid", all_results=results)


def build_attestation(validation: FrozenPinValidation) -> str:
    """Build model-visible attestation block for a VALID frozen-pin check."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"[FROZEN-PIN-ATTESTATION valid_at={now}]"]
    for r in validation.all_results:
        lines.append(f"VALID {r.file_path} sha256={r.expected}")
    lines.append("[/FROZEN-PIN-ATTESTATION]")
    return "\n".join(lines)


def build_frozen_authority_block(pins: list[dict[str, str]]) -> str:
    """Build the [FROZEN-AUTHORITY-PINS] block prepended to initial messages."""
    lines = ["[FROZEN-AUTHORITY-PINS]"]
    for pin in pins:
        lines.append(f"path={pin['file_path']} sha256={pin['sha256']}")
    lines.append("[/FROZEN-AUTHORITY-PINS]")
    return "\n".join(lines)


def format_drift_notice(sender_id: str, validation: FrozenPinValidation) -> str:
    """Format the system-authored drift notice message."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"[FROZEN-PIN-DRIFT terminal={sender_id} detected_at={now}]"]
    for r in validation.drifted:
        reason_str = r.reason or "content"
        expected_short = r.expected[:8] if r.expected else "?"
        observed_short = (r.observed[:8] if r.observed else "none")
        lines.append(
            f"DRIFT {r.file_path} expected={expected_short} "
            f"observed={observed_short} reason={reason_str}"
        )
    lines.append("[/FROZEN-PIN-DRIFT]")
    lines.append("")
    lines.append(
        "Authority-pinned files have drifted since this worker was assigned.\n"
        "The worker's callback payload has been suppressed (stale verdict).\n"
        "Action required: delete this worker and cold-assign a fresh reviewer."
    )
    return "\n".join(lines)
