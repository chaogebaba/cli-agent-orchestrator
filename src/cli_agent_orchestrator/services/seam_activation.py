"""Durable token-carrying authority transitions for receiver-state flips."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeAlias

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients.database import (
    SEAM_ACTIVATION_CONSUMER_OPS,
    SeamActivationEvidenceModel,
    SeamActivationModel,
    SessionLocal,
)

logger = logging.getLogger(__name__)

ConsumerOp: TypeAlias = Literal[
    "watchdog.cached_status",
    "watchdog.waiting_inbox_gate",
    "watchdog.ready_backlog_gate",
    "agent_step.status_reads",
    "delivery.admission_status",
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
RollbackResult: TypeAlias = RolledBack | RollbackConflict

_outage_last_logged: dict[str, float] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def accept(consumer_op: ConsumerOp, evidence_ref: str) -> AcceptResult:
    """Accept one evidence version and mint its promotion token."""

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
    "Promoted",
    "PromotionConflict",
    "RollbackConflict",
    "RollbackResult",
    "RolledBack",
    "accept",
    "consumer_ops",
    "promote",
    "receiver_state_active",
    "rollback",
]
