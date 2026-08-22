"""Durable token-carrying authority transitions for receiver-state flips."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping, TypeAlias

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from cli_agent_orchestrator.clients.database import (
    SEAM_ACTIVATION_CONSUMER_OPS,
    SeamActivationEvidenceModel,
    SeamActivationModel,
    SeamParityMismatchModel,
    SeamParityModel,
    SessionLocal,
)

logger = logging.getLogger(__name__)

ConsumerOp: TypeAlias = Literal[
    "watchdog.cached_status",
    "watchdog.waiting_inbox_gate",
    "watchdog.ready_backlog_gate",
    "agent_step.status_reads",
    "delivery.admission_status",
    "watchdog.pane_classify",
    "delivery.fresh_probe",
    "delivery.park_identity_probe",
    "auto_responder.frame_classify",
]


@dataclass(frozen=True)
class Accepted:
    acceptance_token: str


@dataclass(frozen=True)
class AcceptConflict:
    kind: Literal["accept_conflict"] = "accept_conflict"


@dataclass(frozen=True)
class DuplicateEvidence:
    kind: Literal["duplicate_evidence"] = "duplicate_evidence"


@dataclass(frozen=True)
class Promoted:
    kind: Literal["promoted"] = "promoted"


@dataclass(frozen=True)
class PromotionConflict:
    kind: Literal["promotion_conflict"] = "promotion_conflict"


@dataclass(frozen=True)
class RolledBack:
    kind: Literal["rolled_back"] = "rolled_back"


@dataclass(frozen=True)
class RollbackConflict:
    kind: Literal["rollback_conflict"] = "rollback_conflict"


AcceptResult: TypeAlias = Accepted | AcceptConflict | DuplicateEvidence
PromoteResult: TypeAlias = Promoted | PromotionConflict
PromoteWithEvidenceResult: TypeAlias = Promoted | PromotionConflict | DuplicateEvidence
RollbackResult: TypeAlias = RolledBack | RollbackConflict

_outage_last_logged: dict[str, float] = {}


def _parse_parity_evidence_ref(evidence_ref: str) -> tuple[str, int, str] | None:
    prefix = "parity:"
    if not evidence_ref.startswith(prefix):
        return None
    try:
        build_id, target_text, window_nonce = evidence_ref[len(prefix) :].rsplit(":", 2)
        target_version = int(target_text)
    except (TypeError, ValueError):
        return None
    if not build_id or not window_nonce or target_version < 1:
        return None
    return build_id, target_version, window_nonce


_EVENT_FRAME_OPS = frozenset({"watchdog.pane_classify", "auto_responder.frame_classify"})


def _event_frame_refused(consumer_op: ConsumerOp) -> bool:
    if consumer_op not in _EVENT_FRAME_OPS:
        return False
    try:
        from cli_agent_orchestrator.backends.registry import get_backend

        return get_backend().supports_event_inbox()
    except Exception:
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def accept(consumer_op: ConsumerOp, evidence_ref: str) -> AcceptResult:
    """Accept one evidence version and mint its promotion token."""

    if _event_frame_refused(consumer_op):
        return AcceptConflict()
    acceptance_token = str(uuid.uuid4())
    with SessionLocal() as db:
        try:
            result = db.execute(
                update(SeamActivationModel)
                .where(
                    SeamActivationModel.consumer_op == consumer_op,
                    SeamActivationModel.active_authority == "legacy",
                    SeamActivationModel.accepted_version == SeamActivationModel.active_version,
                )
                .values(
                    accepted_version=SeamActivationModel.active_version + 1,
                    acceptance_token=acceptance_token,
                    evidence_ref=evidence_ref,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                db.rollback()
                return AcceptConflict()
            db.add(
                SeamActivationEvidenceModel(
                    consumer_op=consumer_op,
                    evidence_ref=evidence_ref,
                    acceptance_token=acceptance_token,
                    created_at=_now(),
                )
            )
            db.commit()
            return Accepted(acceptance_token)
        except IntegrityError:
            db.rollback()
            return DuplicateEvidence()
        except OperationalError:
            db.rollback()
            return AcceptConflict()


def promote(consumer_op: ConsumerOp, acceptance_token: str) -> PromoteResult:
    """Promote exactly the accepted version named by ``acceptance_token``."""

    if _event_frame_refused(consumer_op):
        return PromotionConflict()
    with SessionLocal() as db:
        try:
            result = db.execute(
                update(SeamActivationModel)
                .where(
                    SeamActivationModel.consumer_op == consumer_op,
                    SeamActivationModel.active_authority == "legacy",
                    SeamActivationModel.acceptance_token == acceptance_token,
                    SeamActivationModel.accepted_version == SeamActivationModel.active_version + 1,
                )
                .values(
                    active_authority="receiver_state",
                    active_version=SeamActivationModel.accepted_version,
                    tombstoned_legacy=1,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                db.rollback()
                return PromotionConflict()
            db.commit()
            return Promoted()
        except OperationalError:
            db.rollback()
            return PromotionConflict()


def _promote_with_evidence_in(
    db: Session,
    consumer_op: ConsumerOp,
    evidence_ref: str,
) -> PromoteResult:
    """Compose orphan normalization, accept, evidence, promote, and window reset."""

    expected = _parse_parity_evidence_ref(evidence_ref)
    if expected is None:
        return PromotionConflict()
    expected_build_id, target_version, expected_window_nonce = expected

    db.execute(
        update(SeamActivationModel)
        .where(
            SeamActivationModel.consumer_op == consumer_op,
            SeamActivationModel.active_authority == "legacy",
            SeamActivationModel.active_version == target_version - 1,
            SeamActivationModel.accepted_version == SeamActivationModel.active_version + 1,
        )
        .values(
            accepted_version=SeamActivationModel.active_version,
            acceptance_token=None,
            updated_at=_now(),
        )
    )
    acceptance_token = str(uuid.uuid4())
    accepted = db.execute(
        update(SeamActivationModel)
        .where(
            SeamActivationModel.consumer_op == consumer_op,
            SeamActivationModel.active_authority == "legacy",
            SeamActivationModel.active_version == target_version - 1,
            SeamActivationModel.accepted_version == SeamActivationModel.active_version,
        )
        .values(
            accepted_version=SeamActivationModel.active_version + 1,
            acceptance_token=acceptance_token,
            evidence_ref=evidence_ref,
            updated_at=_now(),
        )
    )
    if accepted.rowcount != 1:
        return PromotionConflict()
    db.add(
        SeamActivationEvidenceModel(
            consumer_op=consumer_op,
            evidence_ref=evidence_ref,
            acceptance_token=acceptance_token,
            created_at=_now(),
        )
    )
    db.flush()
    promoted = db.execute(
        update(SeamActivationModel)
        .where(
            SeamActivationModel.consumer_op == consumer_op,
            SeamActivationModel.active_authority == "legacy",
            SeamActivationModel.acceptance_token == acceptance_token,
            SeamActivationModel.accepted_version == SeamActivationModel.active_version + 1,
        )
        .values(
            active_authority="receiver_state",
            active_version=SeamActivationModel.accepted_version,
            tombstoned_legacy=1,
            updated_at=_now(),
        )
    )
    if promoted.rowcount != 1:
        return PromotionConflict()
    parity = db.execute(
        update(SeamParityModel)
        .where(
            SeamParityModel.consumer_op == consumer_op,
            SeamParityModel.build_id == expected_build_id,
            SeamParityModel.phase == "collecting",
            SeamParityModel.window_nonce == expected_window_nonce,
        )
        .values(
            phase="confirming",
            window_started_at=_now(),
            window_nonce=str(uuid.uuid4()),
            clean_samples=0,
            mismatch_count=0,
            last_sample_at=None,
            last_mismatch_detail=None,
        )
    )
    return Promoted() if parity.rowcount == 1 else PromotionConflict()


def promote_with_evidence(consumer_op: ConsumerOp, evidence_ref: str) -> PromoteWithEvidenceResult:
    """Atomically accept evidence, promote authority, and open confirmation."""

    if _event_frame_refused(consumer_op):
        return PromotionConflict()
    with SessionLocal() as db:
        try:
            result = _promote_with_evidence_in(db, consumer_op, evidence_ref)
            if not isinstance(result, Promoted):
                db.rollback()
                return result
            db.commit()
        except IntegrityError:
            db.rollback()
            return DuplicateEvidence()
        except Exception:
            db.rollback()
            logger.exception("Atomic seam promotion failed for %s", consumer_op)
            return PromotionConflict()
    from cli_agent_orchestrator.services.seam_parity import discard_buffer

    discard_buffer(consumer_op)
    return Promoted()


def rollback(consumer_op: ConsumerOp, expected_active_version: int) -> RollbackResult:
    """Restore legacy authority only for the expected active version."""

    with SessionLocal() as db:
        try:
            result = db.execute(
                update(SeamActivationModel)
                .where(
                    SeamActivationModel.consumer_op == consumer_op,
                    SeamActivationModel.active_authority == "receiver_state",
                    SeamActivationModel.active_version == expected_active_version,
                )
                .values(
                    active_authority="legacy",
                    rollback_version=SeamActivationModel.active_version,
                    accepted_version=SeamActivationModel.active_version,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                db.rollback()
                return RollbackConflict()
            db.commit()
            return RolledBack()
        except OperationalError:
            db.rollback()
            return RollbackConflict()


def _rollback_with_mismatch_in(
    db: Session,
    consumer_op: ConsumerOp,
    expected_active_version: int,
    mismatch_detail: Mapping[str, str],
) -> RollbackResult:
    """Compose mismatch history, rollback, and a fresh collecting window."""

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
    if set(mismatch_detail) != required or mismatch_detail["consumer_op"] != consumer_op:
        return RollbackConflict()
    db.add(
        SeamParityMismatchModel(
            **dict(mismatch_detail),
            source="live",
        )
    )
    db.flush()
    rolled_back = db.execute(
        update(SeamActivationModel)
        .where(
            SeamActivationModel.consumer_op == consumer_op,
            SeamActivationModel.active_authority == "receiver_state",
            SeamActivationModel.active_version == expected_active_version,
        )
        .values(
            active_authority="legacy",
            rollback_version=SeamActivationModel.active_version,
            accepted_version=SeamActivationModel.active_version,
            updated_at=_now(),
        )
    )
    if rolled_back.rowcount != 1:
        return RollbackConflict()
    parity = db.execute(
        update(SeamParityModel)
        .where(
            SeamParityModel.consumer_op == consumer_op,
            SeamParityModel.phase == "confirming",
            SeamParityModel.window_nonce == mismatch_detail["window_nonce"],
        )
        .values(
            build_id=mismatch_detail["build_id"],
            phase="collecting",
            window_started_at=_now(),
            window_nonce=str(uuid.uuid4()),
            clean_samples=0,
            mismatch_count=0,
            last_sample_at=mismatch_detail["created_at"],
            last_mismatch_detail=mismatch_detail["detail"],
        )
    )
    return RolledBack() if parity.rowcount == 1 else RollbackConflict()


def rollback_with_mismatch(
    consumer_op: ConsumerOp,
    expected_active_version: int,
    mismatch_detail: Mapping[str, str],
) -> RollbackResult:
    """Atomically persist a confirmation mismatch and restore legacy authority."""

    with SessionLocal() as db:
        try:
            result = _rollback_with_mismatch_in(
                db,
                consumer_op,
                expected_active_version,
                mismatch_detail,
            )
            if not isinstance(result, RolledBack):
                db.rollback()
                return result
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Atomic seam rollback failed for %s", consumer_op)
            return RollbackConflict()
    from cli_agent_orchestrator.services.seam_parity import discard_buffer

    discard_buffer(consumer_op)
    return RolledBack()


def receiver_state_active(consumer_op: ConsumerOp) -> bool:
    """Read current authority; database outage fails closed to legacy."""

    try:
        with SessionLocal() as db:
            row = db.get(SeamActivationModel, consumer_op)
            return bool(row is not None and row.active_authority == "receiver_state")
    except Exception:
        now_mono = time.monotonic()
        last_logged = _outage_last_logged.get(consumer_op)
        if last_logged is None or now_mono - last_logged >= 60.0:
            _outage_last_logged[consumer_op] = now_mono
            logger.warning(
                "Seam activation read failed for %s; using legacy authority",
                consumer_op,
                exc_info=True,
            )
        return False


def consumer_ops() -> tuple[str, ...]:
    return SEAM_ACTIVATION_CONSUMER_OPS


__all__ = [
    "AcceptConflict",
    "AcceptResult",
    "Accepted",
    "ConsumerOp",
    "DuplicateEvidence",
    "PromoteResult",
    "PromoteWithEvidenceResult",
    "Promoted",
    "PromotionConflict",
    "RollbackConflict",
    "RollbackResult",
    "RolledBack",
    "accept",
    "consumer_ops",
    "promote",
    "promote_with_evidence",
    "receiver_state_active",
    "rollback",
    "rollback_with_mismatch",
]
