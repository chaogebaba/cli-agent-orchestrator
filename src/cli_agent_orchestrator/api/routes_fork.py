"""Fork-only API routes kept separate from the upstream-owned route table."""

import asyncio
import logging
import time
from typing import Any, Dict, List, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.core.timing import (
    GATE_QUESTION_EXPIRY_S,
    GATE_QUESTION_WAIT_CAP_S,
)
from cli_agent_orchestrator.models.terminal import TerminalId
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services.terminal_service import MAX_PEEK_TERMINAL_LINES

logger = logging.getLogger(__name__)

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

    ``notify=True``: this is the SERVER, so it is the process that owes the seat
    a message when a question is asked or expires. Every other caller of this
    builder (the offline CLI path) deliberately does not.

    Built per call rather than held as a module global: ``api`` is on the
    ``one-gate-writer`` forbidden list, so the only legal way to a gate store is
    through ``bootstrap``, and a cached handle would outlive a test that points
    the composition root somewhere else.
    """
    return bootstrap.build_gate_question_service(notify=True)


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


#: How often a bounded wait re-reads the question.  A second is far below any
#: human answering latency and far above the cost of one read-only open, so the
#: poll is invisible to the asker and negligible to the database.
_WAIT_POLL_S = 1.0


def _read_settlement(question_id: str) -> Optional[Dict]:
    """One READ-ONLY read of a question and its answer; nothing stays open."""
    result = bootstrap.read_gate_question_settlement(question_id)
    return None if result is None else dict(result)


def _still_open(settlement: Dict) -> bool:
    """Whether the waiting asker should keep waiting."""
    question = settlement.get("question")
    return bool(isinstance(question, dict) and question.get("is_open"))


def _sweep_once() -> Any:
    """Expire what is overdue and retry what never landed, through the gate writer.

    ONE service, so the ``one-gate-writer`` contract holds: the daemon does not
    get its own path to the rows, it makes the same call the ``/gate/sweep``
    route makes.
    """
    service = _gate_question_service()
    return service.sweep()


async def gate_question_expiry_daemon() -> None:
    """Settle overdue questions and re-send lost notices, every period (AC-A7).

    Two obligations the ask transaction creates and cannot itself discharge: a
    question nobody answers must become exactly one anomaly rather than a lane
    that goes quiet, and a notice the transport lost must eventually arrive.

    It is cheap when there is nothing to do, which matters because it runs
    alongside the status monitor: the sweep READS first and opens a write
    transaction only when it has rows to settle, so an idle fleet costs one
    SELECT per period and takes no lock at all.  Never raises — a sweep that
    failed must not take the server's lifespan down with it, and the next period
    retries from the same durable rows.
    """
    period = bootstrap.gate_question_sweep_period_s()
    logger.info("Gate question expiry daemon started (period=%ss)", period)
    while True:
        try:
            expired, retried = await asyncio.to_thread(_sweep_once)
            if expired or retried:
                logger.info(
                    "gate question sweep: expired=%d notices_retried=%d",
                    len(expired),
                    len(retried),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gate question sweep failed")
        await asyncio.sleep(period)


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
    wait: int = Query(default=0, ge=0, le=GATE_QUESTION_WAIT_CAP_S),
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """One question, with its recorded answer once it has settled.

    ``wait`` turns this into a BOUNDED long poll, which is how a blocking asker
    suspends without spinning a model loop: the worker's tool call sits in one
    HTTP request instead of waking up to ask again.  Bounded and capped well
    under ``MCP_REQUEST_TIMEOUT`` on purpose — a client that times out first
    cannot tell "still waiting" from "the server died", and would then retry an
    ask it has already made.  A longer wait is several of these in sequence.

    Each poll iteration opens the database READ-ONLY, reads, and closes before
    sleeping again, so a waiting lane holds no connection, no transaction and no
    pool between iterations and can never be the reason a writer blocks.
    """
    settlement = await asyncio.to_thread(_read_settlement, question_id)
    if settlement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "E_QUESTION_NOT_FOUND", "message": f"no such question: {question_id}"},
        )
    if wait <= 0 or not _still_open(settlement):
        return settlement

    deadline = time.monotonic() + float(wait)
    while time.monotonic() < deadline:
        await asyncio.sleep(min(_WAIT_POLL_S, max(0.0, deadline - time.monotonic())))
        settlement = await asyncio.to_thread(_read_settlement, question_id)
        if settlement is None:  # pragma: no cover - a deleted row mid-wait
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "E_QUESTION_NOT_FOUND",
                    "message": f"no such question: {question_id}",
                },
            )
        if not _still_open(settlement):
            break
    return settlement


@router.post("/gate/questions/{question_id}/consume")
async def consume_gate_answer_endpoint(
    question_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Record that the ASKER received the answer, releasing its dispatch (R28).

    A separate call from reading the answer, because ANSWERED is not proof of
    receipt: the window between a committed answer and a lane that actually has
    it is a real crash window, and it is only visible because consumption is its
    own record.  Idempotent — a second consume of the same event settles nothing
    further.
    """
    from cli_agent_orchestrator.core.gate import GateError

    def _consume() -> Any:
        service = _gate_question_service()
        question = service.require(question_id)
        if question.answer_event_id is not None:
            service.consume_answer(question.answer_event_id)
        return service.require(question_id)

    try:
        question = await asyncio.to_thread(_consume)
    except GateError as exc:
        _raise_gate_question_error(exc)
    return _gate_question_payload(question)


@router.post("/gate/sweep")
async def sweep_gate_questions_endpoint(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Run one expiry-and-retry sweep now, and report what it touched.

    The same call the daemon makes every period, exposed so an operator (and an
    acceptance run) can force one rather than wait out a cadence.
    """
    expired, retried = await asyncio.to_thread(_sweep_once)
    return {
        "expired": [_gate_question_payload(q) for q in expired],
        "notices_retried": [q.question_id for q in retried],
    }


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
