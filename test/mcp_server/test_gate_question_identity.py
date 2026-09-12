"""The end-to-end owner identity, through the MCP surface (B1 r2, review N7).

The defect this exists to catch is silent and total. ``ask_supervisor`` sets a
question's ``owner_conversation`` from the ASKER's environment — its
``CAO_CALLBACK_TERMINAL_ID``, the supervisor it reports to. ``answer_question``
sets ``caller_conversation`` from the ANSWERER's own ``CAO_TERMINAL_ID``. And
``answer_admissible`` refuses when those two strings differ.

So if the supervisor's terminal id is not the same string the worker was given as
its callback id, EVERY answer is refused with ``E_OWNER_MISMATCH`` and the whole
feature is dead on arrival — with no error at ask time and nothing wrong in any
single unit test. The invariant is a property of the two environments together,
so it can only be tested by driving both tools against one store.

``cao_http`` is stubbed to call the real route handlers against a temp database.
That keeps the thing under test exactly what it should be: which identity each
tool reads, and whether the store accepts the pair.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.adapters.store.migrator import migrate

# Its OWN xdist group, deliberately. This arm drives two MCP tools against a
# real store, and several neighbours in ``test/mcp_server/`` patch process-global
# state (``os.environ``, ``server.requests``) in ways that are not worker-safe.
# Co-scheduling this file with them under ``--dist loadgroup`` made 53 of THEIR
# tests fail while every one passed alone and passed as a whole directory. The
# group pins the scheduling instead of leaving it to collection order.
pytestmark = [
    pytest.mark.xdist_group("gate-question-identity"),
    pytest.mark.usefixtures("_gate_db"),
]

SUPERVISOR = "seat0001"
WORKER = "wrkr0001"


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class _RouteClient:
    """Route MCP HTTP calls straight into the real endpoint coroutines."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post(self, path: str, **kwargs: Any) -> _Response:
        from fastapi import HTTPException

        from cli_agent_orchestrator.api import routes_fork as rf

        self.calls.append(("POST", path))
        body = kwargs.get("json") or {}
        scopes = ["cao:write"]

        async def _dispatch() -> Any:
            if path == "/gate/questions":
                return await rf.ask_gate_question_endpoint(
                    body=rf.GateAskRequest(**body), _scopes=scopes
                )
            if path.endswith("/answer"):
                return await rf.answer_gate_question_endpoint(
                    question_id=path.split("/")[3],
                    body=rf.GateAnswerRequest(**body),
                    _scopes=scopes,
                )
            raise AssertionError(path)  # pragma: no cover - only these two are called

        try:
            # The MCP tool is itself running inside a loop, so the endpoint
            # coroutine gets its OWN loop on a worker thread. A nested
            # ``asyncio.run`` on this thread would raise.
            with ThreadPoolExecutor(max_workers=1) as pool:
                payload = pool.submit(lambda: asyncio.run(_dispatch())).result(timeout=30)
        except HTTPException as exc:
            # FastAPI serialises HTTPException.detail UNDER a "detail" key; the
            # fake has to match, or the tool's error extractor sees a body it
            # does not recognise and the typed code never reaches the caller.
            return _Response({"detail": exc.detail}, status_code=exc.status_code)
        return _Response(payload)


def _ask(server: Any, question: str, request_id: str) -> Any:
    """Call the tool with EVERY parameter given.

    A decorated tool's defaults are ``Field(...)`` descriptors, not values: the
    MCP runtime fills them from the schema, so a direct call has to supply them
    or the body receives a ``FieldInfo``.
    """
    return server.ask_supervisor(
        question=question,
        default_answer="re-round",
        options=["accept", "re-round"],
        expires_in_s=3600,
        client_request_id=request_id,
    )


@pytest.fixture
def _gate_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "gate.db"
    result, pool = migrate(path, busy_timeout_ms=5000)
    assert result.ok and pool is not None
    monkeypatch.setattr("cli_agent_orchestrator.bootstrap._default_db_path", lambda: path)
    return path


def _drive(monkeypatch: pytest.MonkeyPatch, *, callback_id: str, seat_id: str) -> Any:
    """Ask as the worker, then answer as the seat, through the two MCP tools."""
    from cli_agent_orchestrator.mcp_server import server

    client = _RouteClient()
    monkeypatch.setattr(server, "cao_http", client)
    monkeypatch.setattr(server, "_api_headers", lambda: {})
    monkeypatch.setattr(server, "_mcp_timeout", lambda: 5.0)

    # The WORKER's environment: it knows the supervisor only as its callback id.
    monkeypatch.setattr(server, "_current_terminal_id", lambda: WORKER)
    monkeypatch.setattr(server, "_callback_route", lambda: ("http://x", callback_id))
    asked = asyncio.run(_ask(server, "accept, re-round or override?", "cr1"))

    # The SEAT's environment: it knows itself by its own terminal id.
    monkeypatch.setattr(server, "_current_terminal_id", lambda: seat_id)
    return asked, asyncio.run(
        server.answer_question(
            question_id=asked["question_id"], answer="accept", client_request_id="ar1"
        )
    )


def test_the_seat_can_answer_the_question_a_worker_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole feature, in one arm: ask as a worker, answer as its supervisor.

    The worker's callback id and the seat's own terminal id are the SAME string,
    which is the invariant the deployment has to hold. When it does, the answer
    is admitted.
    """
    asked, answered = _drive(monkeypatch, callback_id=SUPERVISOR, seat_id=SUPERVISOR)
    assert asked["owner_conversation"] == SUPERVISOR
    assert asked["state"] == "PENDING"
    assert answered["question"]["state"] == "ANSWERED"
    assert answered["answer"]["answer"] == "accept"
    assert answered["answer"]["answered_by"] == SUPERVISOR


def test_a_mismatched_seat_identity_refuses_every_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure mode, stated so it can never be discovered in production.

    If the seat's terminal id is not the string the worker was handed as its
    callback id, the answer is refused with ``E_OWNER_MISMATCH`` — every time,
    for every question. Nothing fails at ask time, so without this arm the first
    symptom would be a lane that waits out its full expiry for an answer somebody
    believes they gave.
    """
    with pytest.raises(ValueError) as exc:
        _drive(monkeypatch, callback_id=SUPERVISOR, seat_id="some-other-terminal")
    assert "E_OWNER_MISMATCH" in str(exc.value)


def test_the_two_tools_read_the_identity_from_the_documented_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name the two sources, so a future edit to either is a visible change.

    ``ask_supervisor`` reads ``_callback_route()``'s supervisor id; with none, it
    falls back to ``CAO_CALLBACK_TERMINAL_ID`` and then to the literal
    ``"supervisor"``. ``answer_question`` reads ``_current_terminal_id()``. This
    arm pins the fallback chain, because it is what a lane launched outside a
    supervisor session will actually use.
    """
    from cli_agent_orchestrator.mcp_server import server

    client = _RouteClient()
    monkeypatch.setattr(server, "cao_http", client)
    monkeypatch.setattr(server, "_api_headers", lambda: {})
    monkeypatch.setattr(server, "_mcp_timeout", lambda: 5.0)
    monkeypatch.setattr(server, "_current_terminal_id", lambda: WORKER)
    monkeypatch.setattr(server, "_callback_route", lambda: ("http://x", None))
    monkeypatch.setenv("CAO_CALLBACK_TERMINAL_ID", "env-seat")
    asked = asyncio.run(_ask(server, "fallback?", "cr9"))
    assert asked["owner_conversation"] == "env-seat"

    monkeypatch.delenv("CAO_CALLBACK_TERMINAL_ID")
    monkeypatch.setattr(server, "_current_terminal_id", lambda: "wrkr0002")
    asked2 = asyncio.run(_ask(server, "last resort?", "cr10"))
    assert asked2["owner_conversation"] == "supervisor"
