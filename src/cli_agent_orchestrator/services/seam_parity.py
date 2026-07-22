"""Durable shadow-parity collection and evidence-gated seam promotion."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, TypeAlias, cast

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cli_agent_orchestrator.clients.database import (
    SeamActivationModel,
    SeamParityMismatchModel,
    SeamParityModel,
    SessionLocal,
)
from cli_agent_orchestrator.constants import SEAM_PARITY_POISON_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

ParityPhase: TypeAlias = Literal["collecting", "confirming", "done"]

PARITY_CONSUMER_OPS = (
    "watchdog.cached_status",
    "watchdog.waiting_inbox_gate",
    "watchdog.ready_backlog_gate",
    "agent_step.status_reads",
    "delivery.admission_status",
)

BUILD_IDENTITY_MODULES = (
    "services/receiver_state_view.py",
    "services/seam_activation.py",
    "services/status_monitor.py",
    "services/agent_step.py",
    "services/inbox_service.py",
    "services/stalled_callback_watchdog.py",
    "clients/database.py",
    "api/main.py",
    "services/seam_parity.py",
)

DEFAULT_CLEAN_SAMPLES = 500
DEFAULT_WINDOW_AGE_SECONDS = 3600.0
CLEAN_FLUSH_SAMPLES = 25
CLEAN_FLUSH_SECONDS = 5.0


@dataclass(frozen=True)
class ParityThresholds:
    clean_samples: int = DEFAULT_CLEAN_SAMPLES
    window_age_seconds: float = DEFAULT_WINDOW_AGE_SECONDS


@dataclass(frozen=True)
class ParityState:
    consumer_op: str
    build_id: str
    phase: ParityPhase
    window_started_at: str
    window_nonce: str
    clean_samples: int
    mismatch_count: int
    last_sample_at: str | None
    last_mismatch_detail: str | None


@dataclass(frozen=True)
class MismatchRecord:
    consumer_op: str
    build_id: str
    window_nonce: str
    phase: ParityPhase
    acted_answer: str
    shadow_answer: str
    detail: str
    created_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "consumer_op": self.consumer_op,
            "build_id": self.build_id,
            "window_nonce": self.window_nonce,
            "phase": self.phase,
            "acted_answer": self.acted_answer,
            "shadow_answer": self.shadow_answer,
            "detail": self.detail,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SeamStatus:
    consumer_op: str
    authority: str
    accepted_version: int
    active_version: int
    rollback_version: int
    build_id: str
    phase: str
    window_started_at: str
    window_nonce: str
    clean_samples: int
    mismatch_count: int
    last_sample_at: str | None
    last_mismatch_detail: str | None
    poisoned: bool
    inhibited: bool
    mismatches: tuple[dict[str, object], ...]


_lock = threading.RLock()
_clean_buffer: dict[tuple[str, str, str, str], int] = {}
_clean_buffer_started: dict[tuple[str, str, str, str], float] = {}
_promotion_inhibited: set[str] = set()
_build_id_cache: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_window() -> tuple[str, str]:
    return _now(), str(uuid.uuid4())


def _answer_value(answer: TerminalStatus | None) -> str:
    return "none" if answer is None else answer.value


def build_identity() -> str:
    """Bind parity evidence to the installed package and behavior surface."""

    try:
        package_version = importlib.metadata.version("cli-agent-orchestrator")
        package_root = Path(__file__).resolve().parents[1]
        digest = hashlib.sha256()
        for relative in BUILD_IDENTITY_MODULES:
            path = package_root / relative
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"{package_version}:{digest.hexdigest()[:16]}"
    except Exception:
        logger.error("Failed to resolve seam parity build identity", exc_info=True)
        return "unknown"


def running_build_id() -> str:
    global _build_id_cache
    if _build_id_cache is None:
        _build_id_cache = build_identity()
    return _build_id_cache


def _thresholds() -> ParityThresholds:
    """Read optional top-level ``[seam_parity]`` providers.toml overrides."""

    try:
        from cli_agent_orchestrator.services import settings_service

        if not settings_service.PROVIDER_DEFAULTS_FILE.exists():
            return ParityThresholds()
        data = tomllib.loads(settings_service.PROVIDER_DEFAULTS_FILE.read_text(encoding="utf-8"))
        section = data.get("seam_parity")
        if not isinstance(section, dict):
            return ParityThresholds()
        clean = section.get("clean_samples", DEFAULT_CLEAN_SAMPLES)
        age = section.get("window_age_seconds", DEFAULT_WINDOW_AGE_SECONDS)
        if not isinstance(clean, int) or isinstance(clean, bool) or clean < 1:
            clean = DEFAULT_CLEAN_SAMPLES
        if not isinstance(age, (int, float)) or isinstance(age, bool) or age < 0:
            age = DEFAULT_WINDOW_AGE_SECONDS
        return ParityThresholds(clean, float(age))
    except Exception:
        logger.warning("Failed to read seam parity thresholds; using defaults", exc_info=True)
        return ParityThresholds()


def _state(row: SeamParityModel) -> ParityState:
    return ParityState(
        consumer_op=str(row.consumer_op),
        build_id=str(row.build_id),
        phase=cast(ParityPhase, str(row.phase)),
        window_started_at=str(row.window_started_at),
        window_nonce=str(row.window_nonce),
        clean_samples=int(row.clean_samples),
        mismatch_count=int(row.mismatch_count),
        last_sample_at=cast(str | None, row.last_sample_at),
        last_mismatch_detail=cast(str | None, row.last_mismatch_detail),
    )


def parity_state(consumer_op: str) -> ParityState | None:
    if consumer_op not in PARITY_CONSUMER_OPS:
        return None
    try:
        with SessionLocal() as db:
            row = db.get(SeamParityModel, consumer_op)
            return None if row is None else _state(row)
    except Exception:
        logger.warning("Failed to read parity state for %s", consumer_op, exc_info=True)
        return None


def discard_buffer(consumer_op: str) -> None:
    with _lock:
        for key in [item for item in _clean_buffer if item[0] == consumer_op]:
            _clean_buffer.pop(key, None)
            _clean_buffer_started.pop(key, None)


def _flush_key(key: tuple[str, str, str, str]) -> None:
    count = _clean_buffer.pop(key, 0)
    _clean_buffer_started.pop(key, None)
    if count <= 0:
        return
    consumer_op, build_id, phase, window_nonce = key
    with SessionLocal() as db:
        result = db.execute(
            update(SeamParityModel)
            .where(
                SeamParityModel.consumer_op == consumer_op,
                SeamParityModel.build_id == build_id,
                SeamParityModel.phase == phase,
                SeamParityModel.window_nonce == window_nonce,
            )
            .values(
                clean_samples=SeamParityModel.clean_samples + count,
                last_sample_at=_now(),
            )
        )
        if result.rowcount == 1:
            db.commit()
        else:
            db.rollback()


def flush_clean_samples() -> None:
    with _lock:
        for key in list(_clean_buffer):
            _flush_key(key)


def _poison_path(consumer_op: str) -> Path:
    return SEAM_PARITY_POISON_DIR / consumer_op


def _inhibit_path(consumer_op: str) -> Path:
    return SEAM_PARITY_POISON_DIR / ".inhibit" / consumer_op


def _write_poison(record: MismatchRecord) -> None:
    SEAM_PARITY_POISON_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(SEAM_PARITY_POISON_DIR, 0o700)
    final = _poison_path(record.consumer_op)
    temp = SEAM_PARITY_POISON_DIR / f".{record.consumer_op}.{uuid.uuid4().hex}.tmp"
    payload = (json.dumps(record.as_dict(), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, final)
        dir_fd = os.open(SEAM_PARITY_POISON_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)


def _write_durable_inhibit(consumer_op: str) -> None:
    inhibit_dir = SEAM_PARITY_POISON_DIR / ".inhibit"
    inhibit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(inhibit_dir, 0o700)
    final = _inhibit_path(consumer_op)
    temp = inhibit_dir / f".{consumer_op}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        payload = b"inhibited\n"
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, final)
        dir_fd = os.open(inhibit_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        parent_fd = os.open(SEAM_PARITY_POISON_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temp.unlink(missing_ok=True)


def _remove_poison(consumer_op: str) -> None:
    path = _poison_path(consumer_op)
    path.unlink(missing_ok=True)
    if SEAM_PARITY_POISON_DIR.exists():
        dir_fd = os.open(SEAM_PARITY_POISON_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _remove_durable_inhibit(consumer_op: str) -> None:
    path = _inhibit_path(consumer_op)
    path.unlink(missing_ok=True)
    inhibit_dir = path.parent
    if inhibit_dir.exists():
        dir_fd = os.open(inhibit_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _inhibition_state(consumer_op: str) -> tuple[bool, bool, bool]:
    poisoned = _poison_path(consumer_op).exists()
    durable = _inhibit_path(consumer_op).exists()
    memory_only = consumer_op in _promotion_inhibited
    return poisoned, durable, memory_only


def _persist_collecting_mismatch(record: MismatchRecord) -> bool:
    started_at, nonce = _fresh_window()
    with SessionLocal() as db:
        db.add(
            SeamParityMismatchModel(
                **record.as_dict(),
                source="live",
            )
        )
        result = db.execute(
            update(SeamParityModel)
            .where(
                SeamParityModel.consumer_op == record.consumer_op,
                SeamParityModel.build_id == record.build_id,
                SeamParityModel.phase == "collecting",
                SeamParityModel.window_nonce == record.window_nonce,
            )
            .values(
                window_started_at=started_at,
                window_nonce=nonce,
                clean_samples=0,
                mismatch_count=0,
                last_sample_at=record.created_at,
                last_mismatch_detail=record.detail,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        db.commit()
        return True


def record_comparison(
    consumer_op: str,
    phase: ParityPhase,
    acted: TerminalStatus | None,
    shadow: TerminalStatus | None,
    *,
    rs_sourced: bool,
) -> Literal["match", "mismatch", "rs_unavailable", "ignored"]:
    """Record one phase-fenced comparison without perturbing the caller's answer."""

    if consumer_op not in PARITY_CONSUMER_OPS or phase == "done":
        return "ignored"
    with _lock:
        state = parity_state(consumer_op)
        if state is None or state.phase != phase:
            return "ignored"
        if not rs_sourced:
            return "rs_unavailable"
        acted_value = _answer_value(acted)
        shadow_value = _answer_value(shadow)
        if acted_value == shadow_value:
            if state.build_id == "unknown" or running_build_id() == "unknown":
                return "match"
            key = (consumer_op, state.build_id, state.phase, state.window_nonce)
            _clean_buffer[key] = _clean_buffer.get(key, 0) + 1
            started = _clean_buffer_started.setdefault(key, time.monotonic())
            if (
                _clean_buffer[key] >= CLEAN_FLUSH_SAMPLES
                or time.monotonic() - started >= CLEAN_FLUSH_SECONDS
            ):
                _flush_key(key)
            return "match"

        detail = f"acted={acted_value} shadow={shadow_value}"
        record = MismatchRecord(
            consumer_op=consumer_op,
            build_id=state.build_id,
            window_nonce=state.window_nonce,
            phase=state.phase,
            acted_answer=acted_value,
            shadow_answer=shadow_value,
            detail=detail,
            created_at=_now(),
        )
        marker_written = False
        try:
            _write_poison(record)
            marker_written = True
        except Exception:
            _promotion_inhibited.add(consumer_op)
            logger.error("Failed to write parity poison marker for %s", consumer_op, exc_info=True)

        discard_buffer(consumer_op)
        persisted = False
        try:
            if phase == "collecting":
                persisted = _persist_collecting_mismatch(record)
            else:
                from cli_agent_orchestrator.services.seam_activation import (
                    RolledBack,
                    rollback_with_mismatch,
                )

                with SessionLocal() as db:
                    activation = db.get(SeamActivationModel, consumer_op)
                    active_version = -1 if activation is None else int(activation.active_version)
                persisted = isinstance(
                    rollback_with_mismatch(
                        cast(Any, consumer_op), active_version, record.as_dict()
                    ),
                    RolledBack,
                )
                if persisted:
                    logger.warning(
                        "seam_confirmation_mismatch op=%s build=%s detail=%s",
                        consumer_op,
                        state.build_id,
                        detail,
                    )
        except Exception:
            logger.error("Failed to persist parity mismatch for %s", consumer_op, exc_info=True)
        if persisted:
            if marker_written:
                _remove_poison(consumer_op)
        elif not marker_written:
            _promotion_inhibited.add(consumer_op)
        return "mismatch"


def _reset_values(build_id: str, phase: ParityPhase) -> dict[str, object]:
    started_at, nonce = _fresh_window()
    return {
        "build_id": build_id,
        "phase": phase,
        "window_started_at": started_at,
        "window_nonce": nonce,
        "clean_samples": 0,
        "mismatch_count": 0,
        "last_sample_at": None,
        "last_mismatch_detail": None,
    }


def _repair_matrix() -> None:
    build_id = running_build_id()
    with SessionLocal() as db:
        for consumer_op in PARITY_CONSUMER_OPS:
            activation = db.get(SeamActivationModel, consumer_op)
            if activation is None:
                raise RuntimeError(f"missing seam activation row: {consumer_op}")
            authority = str(activation.active_authority)
            row = db.get(SeamParityModel, consumer_op)
            if row is None:
                phase: ParityPhase = "collecting" if authority == "legacy" else "confirming"
                db.add(
                    SeamParityModel(
                        consumer_op=consumer_op,
                        **_reset_values(build_id, phase),
                    )
                )
                continue

            phase = cast(ParityPhase, str(row.phase))
            target: ParityPhase | None = None
            if authority == "legacy" and phase in {"confirming", "done"}:
                target = "collecting"
            elif authority == "receiver_state" and phase == "collecting":
                target = "confirming"
            elif str(row.build_id) != build_id:
                target = "confirming" if phase == "done" else phase
            if target is not None:
                for key, value in _reset_values(build_id, target).items():
                    setattr(row, key, value)
                discard_buffer(consumer_op)
        db.commit()


def _recover_poison(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "consumer_op",
        "build_id",
        "window_nonce",
        "phase",
        "acted_answer",
        "shadow_answer",
        "detail",
        "created_at",
    }
    if set(payload) != required:
        raise ValueError(f"invalid seam parity poison marker fields: {path}")
    consumer_op = payload["consumer_op"]
    if consumer_op not in PARITY_CONSUMER_OPS or path.name != consumer_op:
        raise ValueError(f"invalid seam parity poison marker op: {path}")
    started_at, nonce = _fresh_window()
    with SessionLocal() as db:
        activation = db.get(SeamActivationModel, consumer_op)
        parity = db.get(SeamParityModel, consumer_op)
        if activation is None or parity is None:
            raise RuntimeError(f"missing seam rows during poison recovery: {consumer_op}")
        db.execute(
            sqlite_insert(SeamParityMismatchModel)
            .values(**payload, source="poison_recovery")
            .on_conflict_do_nothing(
                index_elements=["consumer_op", "window_nonce", "created_at", "source"]
            )
        )
        if activation.active_authority == "receiver_state":
            setattr(activation, "active_authority", "legacy")
            setattr(activation, "rollback_version", activation.active_version)
            setattr(activation, "accepted_version", activation.active_version)
            setattr(activation, "updated_at", _now())
        for key, value in {
            "build_id": running_build_id(),
            "phase": "collecting",
            "window_started_at": started_at,
            "window_nonce": nonce,
            "clean_samples": 0,
            "mismatch_count": 0,
            "last_sample_at": payload["created_at"],
            "last_mismatch_detail": payload["detail"],
        }.items():
            setattr(parity, key, value)
        db.commit()
    discard_buffer(consumer_op)
    _write_durable_inhibit(consumer_op)
    _remove_poison(consumer_op)


def startup_repair() -> None:
    """Synchronously repair parity state and recover poison before readiness."""

    with _lock:
        _repair_matrix()
        if not SEAM_PARITY_POISON_DIR.exists():
            return
        for path in sorted(SEAM_PARITY_POISON_DIR.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                _recover_poison(path)


def _window_age_seconds(started_at: str, now: datetime) -> float:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (now - started.astimezone(timezone.utc)).total_seconds())


def _open_fresh_window(consumer_op: str, phase: ParityPhase) -> None:
    with SessionLocal() as db:
        row = db.get(SeamParityModel, consumer_op)
        if row is None:
            raise RuntimeError(f"missing parity row: {consumer_op}")
        for key, value in _reset_values(running_build_id(), phase).items():
            setattr(row, key, value)
        db.commit()
    discard_buffer(consumer_op)


def sweep() -> None:
    """Drain clean samples, then promote or confirm eligible windows."""

    from cli_agent_orchestrator.services import seam_activation

    with _lock:
        flush_clean_samples()
        thresholds = _thresholds()
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            rows: dict[str, tuple[ParityState, SeamActivationModel | None]] = {}
            for row in db.execute(
                select(SeamParityModel).where(SeamParityModel.consumer_op.in_(PARITY_CONSUMER_OPS))
            ).scalars():
                op = str(row.consumer_op)
                rows[op] = (_state(row), db.get(SeamActivationModel, op))
        for consumer_op in PARITY_CONSUMER_OPS:
            entry = rows.get(consumer_op)
            if entry is None:
                continue
            state, activation = entry
            poisoned, durable_inhibit, memory_only = _inhibition_state(consumer_op)
            if poisoned or durable_inhibit or memory_only:
                logger.error(
                    "seam parity promotion inhibited op=%s poisoned=%s durable=%s memory_only=%s",
                    consumer_op,
                    poisoned,
                    durable_inhibit,
                    memory_only,
                )
                continue
            if state is None or activation is None or state.phase == "done":
                continue
            if state.build_id == "unknown" or running_build_id() == "unknown":
                continue
            age = _window_age_seconds(state.window_started_at, now)
            if (
                state.clean_samples < thresholds.clean_samples
                or age < thresholds.window_age_seconds
                or state.mismatch_count != 0
            ):
                continue
            if state.phase == "collecting":
                if activation.active_authority != "legacy":
                    continue
                target = int(activation.active_version) + 1
                evidence_ref = f"parity:{state.build_id}:{target}:{state.window_nonce}"
                result = seam_activation.promote_with_evidence(cast(Any, consumer_op), evidence_ref)
                if isinstance(result, seam_activation.Promoted):
                    discard_buffer(consumer_op)
                    logger.info(
                        "seam_promoted op=%s build=%s clean=%s window=%s",
                        consumer_op,
                        state.build_id,
                        state.clean_samples,
                        int(age),
                    )
                elif isinstance(result, seam_activation.DuplicateEvidence):
                    logger.error("Duplicate seam parity evidence for %s", consumer_op)
                    _open_fresh_window(consumer_op, "collecting")
                else:
                    logger.warning("Seam parity promotion conflict for %s", consumer_op)
                continue

            if activation.active_authority != "receiver_state":
                continue
            with SessionLocal() as db:
                update_result = db.execute(
                    update(SeamParityModel)
                    .where(
                        SeamParityModel.consumer_op == consumer_op,
                        SeamParityModel.build_id == state.build_id,
                        SeamParityModel.phase == "confirming",
                        SeamParityModel.window_nonce == state.window_nonce,
                        SeamParityModel.mismatch_count == 0,
                    )
                    .values(phase="done", last_sample_at=_now())
                )
                if update_result.rowcount == 1:
                    db.commit()
                    discard_buffer(consumer_op)
                    logger.info(
                        "seam_confirmed op=%s build=%s clean=%s window=%s",
                        consumer_op,
                        state.build_id,
                        state.clean_samples,
                        int(age),
                    )
                else:
                    db.rollback()


def manual_rollback(consumer_op: str) -> object:
    from cli_agent_orchestrator.services import seam_activation

    if consumer_op not in PARITY_CONSUMER_OPS:
        raise ValueError(f"out-of-scope seam: {consumer_op}")
    with _lock:
        with SessionLocal() as db:
            activation = db.get(SeamActivationModel, consumer_op)
            if activation is None:
                raise ValueError(f"unknown seam: {consumer_op}")
            version = int(activation.active_version)
        result = seam_activation.rollback(cast(Any, consumer_op), version)
        if isinstance(result, seam_activation.RolledBack):
            _open_fresh_window(consumer_op, "collecting")
        return result


def reset(consumer_op: str) -> None:
    if consumer_op not in PARITY_CONSUMER_OPS:
        raise ValueError(f"out-of-scope seam: {consumer_op}")
    with _lock:
        with SessionLocal() as db:
            activation = db.get(SeamActivationModel, consumer_op)
            if activation is None:
                raise ValueError(f"unknown seam: {consumer_op}")
            phase: ParityPhase = (
                "collecting" if activation.active_authority == "legacy" else "confirming"
            )
        _open_fresh_window(consumer_op, phase)
        _remove_poison(consumer_op)
        _remove_durable_inhibit(consumer_op)
        _promotion_inhibited.discard(consumer_op)


def status_rows() -> tuple[SeamStatus, ...]:
    with _lock, SessionLocal() as db:
        result: list[SeamStatus] = []
        for consumer_op in PARITY_CONSUMER_OPS:
            activation = db.get(SeamActivationModel, consumer_op)
            parity = db.get(SeamParityModel, consumer_op)
            if activation is None or parity is None:
                continue
            poisoned, durable_inhibit, memory_only = _inhibition_state(consumer_op)
            mismatches = tuple(
                {
                    "id": row.id,
                    "build_id": row.build_id,
                    "window_nonce": row.window_nonce,
                    "phase": row.phase,
                    "acted_answer": row.acted_answer,
                    "shadow_answer": row.shadow_answer,
                    "detail": row.detail,
                    "source": row.source,
                    "created_at": row.created_at,
                }
                for row in db.execute(
                    select(SeamParityMismatchModel)
                    .where(SeamParityMismatchModel.consumer_op == consumer_op)
                    .order_by(SeamParityMismatchModel.id)
                ).scalars()
            )
            result.append(
                SeamStatus(
                    consumer_op=consumer_op,
                    authority=str(activation.active_authority),
                    accepted_version=int(activation.accepted_version),
                    active_version=int(activation.active_version),
                    rollback_version=int(activation.rollback_version),
                    build_id=str(parity.build_id),
                    phase=str(parity.phase),
                    window_started_at=str(parity.window_started_at),
                    window_nonce=str(parity.window_nonce),
                    clean_samples=int(parity.clean_samples),
                    mismatch_count=int(parity.mismatch_count),
                    last_sample_at=cast(str | None, parity.last_sample_at),
                    last_mismatch_detail=cast(str | None, parity.last_mismatch_detail),
                    poisoned=poisoned,
                    inhibited=poisoned or durable_inhibit or memory_only,
                    mismatches=mismatches,
                )
            )
        return tuple(result)


__all__ = [
    "BUILD_IDENTITY_MODULES",
    "PARITY_CONSUMER_OPS",
    "ParityPhase",
    "ParityState",
    "SeamStatus",
    "build_identity",
    "discard_buffer",
    "flush_clean_samples",
    "manual_rollback",
    "parity_state",
    "record_comparison",
    "reset",
    "running_build_id",
    "startup_repair",
    "status_rows",
    "sweep",
]
