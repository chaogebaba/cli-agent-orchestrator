"""F875 desk-service API routes (shadow mode, R1a).

Thin HTTP surface over ``services.desk_service`` for the ``desk`` /
``desk_status`` / ``desk_notice_slot`` MCP tools. The server owns all state and
logic; these handlers only translate request/response and typed errors. NOT
wired into the live seat create path or the hooks in R1a — the cutover (D7) is
R1b.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)

router = APIRouter()


class DeskRequest(BaseModel):
    """Body for a ``desk`` lookup admission or handle replay."""

    conversation_id: str = Field(description="The supervisor conversation identity_key")
    question: str = Field(default="", description="Read-only lookup; may name the decision itself")
    decision: Optional[str] = Field(default=None)
    scope: Optional[str] = Field(default=None)
    request_id: Optional[str] = Field(
        default=None, description="A prior handle to replay; dispatches no second job"
    )
    incarnation: Optional[str] = Field(default=None)


class NoticeSlotRequest(BaseModel):
    conversation_id: str = Field(description="identity_key; the counter is keyed by this alone")
    slot_kind: str = Field(default="operational")
    incarnation: str = Field(default="*")


@router.post("/desk")
async def desk_endpoint(
    body: DeskRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Admit a read-only lookup (or replay a handle) and return the current
    outcome: a cited answer, a PENDING line + handle, or BUSY_QUEUE_FULL."""
    from cli_agent_orchestrator.services import desk_service as ds

    try:
        admit = ds.admit_query(
            body.conversation_id,
            body.question,
            incarnation=body.incarnation,
            decision=body.decision,
            scope=body.scope,
            request_id=body.request_id,
        )
    except ds.NoBindingError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    if admit.kind == "queue_full":
        # Inline refusal: no handle, no deadline started (AC-19).
        return {"outcome": "BUSY_QUEUE_FULL", "state": admit.state}

    wait = ds.wait_for(admit.request_id)  # type: ignore[arg-type]
    return {
        "outcome": wait.kind,
        "request_id": wait.request_id,
        "replayed": admit.replayed,
        "answer_lines": list(wait.answer_lines) if wait.answer_lines else None,
        "artifact_ref": wait.artifact_ref,
        "pending_line": wait.pending_line,
        "query_outcome": wait.outcome,
    }


@router.get("/desk/status/{conversation_id}")
async def desk_status_endpoint(
    conversation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Return the DeskUsage projection plus the current binding state, its typed
    degraded cause and its recovery deadline."""
    from cli_agent_orchestrator.clients.database import DeskBindingModel, SessionLocal
    from cli_agent_orchestrator.services import desk_service as ds

    usage = ds.project_usage(conversation_id)
    with SessionLocal() as db:
        binding = (
            db.query(DeskBindingModel).filter_by(conversation_id=conversation_id).one_or_none()
        )
        binding_view = (
            None
            if binding is None
            else {
                "state": binding.state,
                "degraded_cause": binding.degraded_cause,
                "retry_deadline": (
                    binding.retry_deadline.isoformat() if binding.retry_deadline else None
                ),
                "provider_binding": binding.provider_binding,
                "queue_depth": binding.queue_depth,
            }
        )
    return {"usage": usage.as_dict(), "binding": binding_view}


@router.post("/desk/notice-slot")
async def desk_notice_slot_endpoint(
    body: NoticeSlotRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Request one server-owned notice slot (D4/B6). Emits nothing on a refused
    or unavailable grant (AC-9)."""
    from cli_agent_orchestrator.services import desk_service as ds

    grant = ds.request_notice_slot(
        body.conversation_id, slot_kind=body.slot_kind, incarnation=body.incarnation
    )
    return {
        "granted": grant.granted,
        "slots_used": grant.slots_used,
        "slots_remaining": grant.slots_remaining,
        "reason": grant.reason,
    }
