"""Fork-only API routes kept separate from the upstream-owned route table."""

import asyncio
from typing import Any, Dict, List, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.core.timing import GATE_QUESTION_EXPIRY_S
from cli_agent_orchestrator.models.terminal import TerminalId
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services.terminal_service import MAX_PEEK_TERMINAL_LINES

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
    lines: int = Query(default=40, ge=1, le=MAX_PEEK_TERMINAL_LINES),
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


# ---------------------------------------------------------------------------
# Gate questions (WP-ARCH Amendment A, slice B1; §10.2 P2, A2).
#
# In the FORK router rather than ``api/main.py``, for a reason the hook-point
# contract makes concrete.  These handlers must reach the new tree — the typed
# refusal vocabulary in ``core.gate`` and the one question serialiser in
# ``app.gate.render`` — and ``test/adapters/truth/test_hook_points.py`` holds the
# set of LEGACY files allowed to do that to an EQUALITY, deliberately, so that
# the surface a reviewer must read to see what legacy now depends on stays
# small.  Putting them in the 11k-line ``api/main.py`` would sanction that whole
# file as a new-tree importer; putting them here sanctions 400 fork-only lines,
# which is the same move ``cli/commands/diag.py`` and ``services/queue_carrier.py``
# already made.  The paths are unchanged either way: this router is mounted at
# the root in ``api/main.py``.
#
# They are also deliberately nowhere near the inbox routes — 3c's territory, and
# a question notice is not an inbox concept — and they are the routes the two new
# MCP tools call, which is how standing invariant #535/F680 holds by
# construction: ``ask_supervisor`` and ``cao gate ask`` reach this same handler.
#
# ``api`` is on the ``one-gate-writer`` forbidden list, so nothing here names
# ``adapters.store.gate``: the service arrives from ``bootstrap``, which is the
# sanctioned writer.
# ---------------------------------------------------------------------------


class GateAskRequest(BaseModel):
    """Body of an ask (``POST /gate/questions``).

    ``client_request_id`` is MANDATORY and is the idempotency key.  A blocking
    asker is suspended inside its own tool call, so its retry after a transport
    timeout must return the question it already asked rather than a refusal
    saying it already has one open — and only a caller-supplied key can tell
    those two apart.
    """

    dispatch_id: str
    question: str
    client_request_id: str
    owner_conversation: str
    owner_epoch: int = 0
    round_id: Optional[str] = None
    options: List[str] = Field(default_factory=list)
    blocking: bool = True
    #: Defaulted from ``core.timing``, the one home a duration has (§4c).
    expires_in_s: int = Field(default=GATE_QUESTION_EXPIRY_S, ge=1, le=86400)
    continuation_kind: str = "ASSIGNMENT"
    continuation_ref: str = ""
    answer_schema: Optional[str] = None
    default_answer: Optional[str] = None
    position: Optional[str] = None


class GateAnswerRequest(BaseModel):
    """Body of an answer (``POST /gate/questions/{id}/answer``)."""

    answer: str
    answered_by: str
    client_request_id: str
    caller_conversation: str
    caller_epoch: int = 0


def _gate_question_service() -> Any:
    """The question service, built on demand by the composition root.

    Built per call rather than held as a module global: ``api`` is on the
    ``one-gate-writer`` forbidden list, so the only legal way to a gate store is
    through ``bootstrap``, and a cached handle would outlive a test that points
    the composition root somewhere else.
    """
    return bootstrap.build_gate_question_service()


def _gate_question_payload(question: Any) -> Dict:
    """One question as JSON, through ``app/gate``'s single serialiser."""
    from cli_agent_orchestrator.app.gate.render import question_payload

    return dict(question_payload(question))


def _raise_gate_question_error(exc: Exception) -> NoReturn:
    """Translate a domain refusal into ONE status and ONE machine-readable body.

    A refusal's ``code`` survives the boundary because the caller branches on it:
    a lane retrying an ask needs "you already have a DIFFERENT question open"
    (409) to be distinguishable from "that question is gone" (404) without
    parsing English.  A plain ``GateError`` has no code and is a 400 — bad input,
    not a state conflict.
    """
    from cli_agent_orchestrator.core.gate import (
        GateError,
        GateQuestionError,
        QuestionRefusal,
    )

    if isinstance(exc, GateQuestionError):
        code = exc.code
        if code is QuestionRefusal.QUESTION_NOT_FOUND:
            status_code = status.HTTP_404_NOT_FOUND
        elif code in (QuestionRefusal.DEFAULT_REQUIRED, QuestionRefusal.QUESTION_EMPTY):
            status_code = status.HTTP_400_BAD_REQUEST
        else:
            status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status_code, detail={"code": code.value, "message": str(exc)}
        )
    if isinstance(exc, GateError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "E_GATE", "message": str(exc)},
        )
    raise exc


@router.post("/gate/questions")
async def ask_gate_question_endpoint(
    body: GateAskRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Ask a durable question and suspend the asking dispatch (A2, AC-A10)."""
    from cli_agent_orchestrator.core.gate import ContinuationKind, GateError

    try:
        kind = ContinuationKind(body.continuation_kind)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "E_CONTINUATION_KIND",
                "message": f"unknown continuation kind {body.continuation_kind!r}",
            },
        )

    def _ask() -> Any:
        return _gate_question_service().ask(
            dispatch_id=body.dispatch_id,
            question=body.question,
            client_request_id=body.client_request_id,
            owner_conversation=body.owner_conversation,
            owner_epoch=body.owner_epoch,
            round_id=body.round_id,
            options=tuple(body.options),
            blocking=body.blocking,
            expires_in_s=body.expires_in_s,
            continuation_kind=kind,
            continuation_ref=body.continuation_ref,
            answer_schema=body.answer_schema,
            default_answer=body.default_answer,
            position=body.position,
        )

    try:
        question, replayed = await asyncio.to_thread(_ask)
    except GateError as exc:
        _raise_gate_question_error(exc)
    payload = _gate_question_payload(question)
    # Surfaced, not swallowed: callers derive their idempotency keys, so a
    # repeated ask is indistinguishable from a retry unless the server says so.
    payload["replayed"] = replayed
    return payload


@router.get("/gate/questions")
async def list_gate_questions_endpoint(
    owner: Optional[str] = Query(default=None),
    state: Optional[List[str]] = Query(default=None),
    round_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """List questions, newest first.  With no ``state`` filter, the OPEN ones."""
    from cli_agent_orchestrator.core.gate import QuestionState

    if state:
        try:
            states = tuple(QuestionState(value) for value in state)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "E_QUESTION_STATE", "message": str(exc)},
            )
    else:
        states = (QuestionState.PENDING, QuestionState.ESCALATED)

    def _list() -> Any:
        return _gate_question_service().list_open(
            owner_conversation=owner, states=states, round_id=round_id, limit=limit
        )

    questions = await asyncio.to_thread(_list)
    return {"items": [_gate_question_payload(q) for q in questions]}


@router.get("/gate/questions/{question_id}")
async def get_gate_question_endpoint(
    question_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """One question, with its recorded answer when it has settled."""

    def _get() -> Any:
        service = _gate_question_service()
        question = service.get(question_id)
        return question

    question = await asyncio.to_thread(_get)
    if question is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "E_QUESTION_NOT_FOUND", "message": f"no such question: {question_id}"},
        )
    return _gate_question_payload(question)


@router.post("/gate/questions/{question_id}/answer")
async def answer_gate_question_endpoint(
    question_id: str,
    body: GateAnswerRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Record an answer, conditionally on state, expiry AND owner epoch (AC-A12)."""
    from cli_agent_orchestrator.core.gate import GateError

    def _answer() -> Any:
        return _gate_question_service().answer(
            question_id=question_id,
            answer=body.answer,
            answered_by=body.answered_by,
            client_request_id=body.client_request_id,
            caller_conversation=body.caller_conversation,
            caller_epoch=body.caller_epoch,
        )

    try:
        question, event = await asyncio.to_thread(_answer)
    except GateError as exc:
        _raise_gate_question_error(exc)
    return {
        "question": _gate_question_payload(question),
        "answer": {
            "answer_event_id": event.answer_event_id,
            "answer": event.answer,
            "answered_by": event.answered_by,
            "answered_at": event.answered_at.isoformat(),
        },
    }


@router.post("/gate/questions/{question_id}/escalate")
async def escalate_gate_question_endpoint(
    question_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Raise a PENDING question to ESCALATED, keeping the SAME id.

    The id is unchanged on purpose: the user's answer must return on the question
    the lane is already suspended on, and a fresh question would leave the
    original open behind it.
    """
    from cli_agent_orchestrator.core.gate import GateError

    def _escalate() -> Any:
        return _gate_question_service().escalate(question_id)

    try:
        question = await asyncio.to_thread(_escalate)
    except GateError as exc:
        _raise_gate_question_error(exc)
    return _gate_question_payload(question)
