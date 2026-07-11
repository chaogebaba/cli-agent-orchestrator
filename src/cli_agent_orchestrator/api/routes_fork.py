"""Fork-only API routes kept separate from the upstream-owned route table."""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from cli_agent_orchestrator.models.terminal import TerminalId
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)

router = APIRouter()


class TerminalPeekResponse(BaseModel):
    terminal_id: str
    lines: int
    output: str


class CodexReviewRequest(BaseModel):
    """Request body for launching an async headless Codex review."""

    requester_id: TerminalId = Field(description="Terminal that receives completion inbox push")
    instructions: Optional[str] = Field(
        default=None,
        description=(
            "Custom review instructions. Mutually exclusive with scope; "
            "instructions-only reviews the working-tree diff."
        ),
    )
    scope: Optional[str] = Field(
        default=None,
        description=(
            "Review scope: uncommitted, base, or commit. Mutually exclusive with instructions."
        ),
    )
    target: Optional[str] = Field(
        default=None,
        description="Base branch for scope=base or commit SHA for scope=commit",
    )
    cwd: Optional[str] = Field(default=None, description="Required repository to review")


@router.post("/codex-review")
async def codex_review_endpoint(
    request: Request,
    review_request: CodexReviewRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Launch headless ``codex review`` and push completion to requester inbox."""
    # Resolve through api.main at call time to preserve its established patch seam.
    from cli_agent_orchestrator.api import main as api_main

    if not api_main.get_terminal_metadata(review_request.requester_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Terminal '{review_request.requester_id}' not found",
        )
    try:
        return api_main.codex_review_service.start_codex_review(
            requester_id=review_request.requester_id,
            instructions=review_request.instructions,
            scope=review_request.scope,
            target=review_request.target,
            cwd=review_request.cwd,
            registry=api_main.get_plugin_registry(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/provider-sessions/{session_uuid}/owner")
async def get_provider_session_owner(
    session_uuid: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> Dict[str, object]:
    from cli_agent_orchestrator.api import main as api_main

    return api_main.terminal_service.provider_session_owner(session_uuid)


@router.get("/terminals/{terminal_id}/peek", response_model=TerminalPeekResponse)
async def peek_terminal(
    terminal_id: TerminalId,
    lines: int = Query(default=40, ge=1, le=200),
) -> TerminalPeekResponse:
    from cli_agent_orchestrator.api import main as api_main

    try:
        output = api_main.terminal_service.peek_terminal(terminal_id, lines)
        return TerminalPeekResponse(terminal_id=terminal_id, lines=lines, output=output)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to peek terminal: {str(e)}",
        )


@router.get("/messages/{message_id}/trace")
async def get_message_trace_endpoint(
    message_id: int,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> Dict:
    from cli_agent_orchestrator.api import main as api_main

    trace = api_main.get_message_trace(message_id)
    if trace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return trace
