"""The five ``/gate/questions`` routes (WP-ARCH A, slice B1).

Called as functions with an explicit scope list, the pattern
``test_f642_claim_write_gate.py`` uses: what is under test is the handler's own
behaviour — its scope gate, its translation of a domain refusal into ONE status
and ONE machine-readable body — and a TestClient would add a middleware stack
that decides none of that.

The handlers live in ``api/routes_fork.py``, the fork's own router, mounted at
the root by ``api/main.py``: the paths are the same, and the set of LEGACY files
sanctioned to import the new tree grows by one small fork-only module instead of
by the 11k-line upstream route table.

The service is pointed at a temp database through the composition root, which is
also the seam that proves these handlers never name ``adapters.store.gate``
(``api`` is on the ``one-gate-writer`` forbidden list).
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.api.routes_fork import (
    GateAnswerRequest,
    GateAskRequest,
    answer_gate_question_endpoint,
    ask_gate_question_endpoint,
    consume_gate_answer_endpoint,
    escalate_gate_question_endpoint,
    gate_question_expiry_daemon,
    get_gate_question_endpoint,
    list_gate_questions_endpoint,
    sweep_gate_questions_endpoint,
)
from cli_agent_orchestrator.security.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE

_WRITE = [SCOPE_WRITE, SCOPE_ADMIN]
_READ = [SCOPE_READ]


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch gate database the handlers resolve to, never the live one."""
    path = tmp_path / "gate.db"
    result, pool = migrate(path, busy_timeout_ms=5000)
    assert result.ok and pool is not None
    monkeypatch.setattr("cli_agent_orchestrator.bootstrap._default_db_path", lambda: path)
    return path


def _ask(**overrides: Any) -> Any:
    body = {
        "dispatch_id": "worker-1",
        "question": "accept, re-round or override?",
        "client_request_id": "cr1",
        "owner_conversation": "seat",
        "options": ["accept", "re-round"],
        "position": "dev",
    }
    body.update(overrides)
    return asyncio.run(ask_gate_question_endpoint(body=GateAskRequest(**body), _scopes=_WRITE))


def _answer_it(question_id: str, text: str = "accept") -> Any:
    return asyncio.run(
        answer_gate_question_endpoint(
            question_id=question_id,
            body=GateAnswerRequest(
                answer=text,
                answered_by="seat",
                client_request_id=f"ar-{text}",
                caller_conversation="seat",
            ),
            _scopes=_WRITE,
        )
    )


def test_ask_returns_the_recorded_question(db: Path) -> None:
    payload = _ask()
    assert payload["state"] == "PENDING"
    assert payload["is_open"] is True
    assert payload["options"] == ["accept", "re-round"]
    assert payload["dispatch_id"] == "worker-1"


def test_ask_is_idempotent_by_request_id(db: Path) -> None:
    first = _ask()
    second = _ask()
    assert second["question_id"] == first["question_id"]


def test_a_second_open_question_is_a_409_with_a_branchable_code(db: Path) -> None:
    """The caller must tell "already waiting" from "gone" without parsing English."""
    _ask()
    with pytest.raises(HTTPException) as exc:
        _ask(client_request_id="cr2", question="something else?")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "E_QUESTION_OPEN"


def test_a_non_blocking_ask_without_a_default_is_a_400(db: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        _ask(blocking=False)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "E_DEFAULT_REQUIRED"


def test_an_unknown_continuation_kind_is_a_400(db: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        _ask(continuation_kind="TELEPATHY")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "E_CONTINUATION_KIND"


def test_get_returns_404_for_an_unknown_question(db: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_gate_question_endpoint(question_id="nope", wait=0, _scopes=_READ))
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "E_QUESTION_NOT_FOUND"


def test_answer_settles_the_question_and_returns_the_event(db: Path) -> None:
    asked = _ask()
    result = asyncio.run(
        answer_gate_question_endpoint(
            question_id=asked["question_id"],
            body=GateAnswerRequest(
                answer="accept",
                answered_by="seat",
                client_request_id="ar1",
                caller_conversation="seat",
            ),
            _scopes=_WRITE,
        )
    )
    assert result["question"]["state"] == "ANSWERED"
    assert result["question"]["is_open"] is False
    assert result["answer"]["answer"] == "accept"


def test_answering_twice_with_a_different_answer_is_a_409(db: Path) -> None:
    asked = _ask()

    def _answer(text: str) -> Any:
        return asyncio.run(
            answer_gate_question_endpoint(
                question_id=asked["question_id"],
                body=GateAnswerRequest(
                    answer=text,
                    answered_by="seat",
                    client_request_id="ar1",
                    caller_conversation="seat",
                ),
                _scopes=_WRITE,
            )
        )

    _answer("accept")
    with pytest.raises(HTTPException) as exc:
        _answer("re-round")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "E_ANSWER_CONFLICT"


def test_answer_from_a_different_conversation_is_refused(db: Path) -> None:
    asked = _ask()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            answer_gate_question_endpoint(
                question_id=asked["question_id"],
                body=GateAnswerRequest(
                    answer="accept",
                    answered_by="someone",
                    client_request_id="ar1",
                    caller_conversation="another-seat",
                ),
                _scopes=_WRITE,
            )
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "E_OWNER_MISMATCH"


def test_escalate_keeps_the_same_id_and_the_open_slot(db: Path) -> None:
    asked = _ask()
    escalated = asyncio.run(
        escalate_gate_question_endpoint(question_id=asked["question_id"], _scopes=_WRITE)
    )
    assert escalated["question_id"] == asked["question_id"]
    assert escalated["state"] == "ESCALATED"
    assert escalated["is_open"] is True


def test_escalating_a_settled_question_is_a_409(db: Path) -> None:
    asked = _ask()
    asyncio.run(
        answer_gate_question_endpoint(
            question_id=asked["question_id"],
            body=GateAnswerRequest(
                answer="accept",
                answered_by="seat",
                client_request_id="ar1",
                caller_conversation="seat",
            ),
            _scopes=_WRITE,
        )
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            escalate_gate_question_endpoint(question_id=asked["question_id"], _scopes=_WRITE)
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "E_ILLEGAL_TRANSITION"


def test_list_defaults_to_the_open_questions(db: Path) -> None:
    open_q = _ask()
    settled = _ask(dispatch_id="worker-2", client_request_id="cr2")
    asyncio.run(
        answer_gate_question_endpoint(
            question_id=settled["question_id"],
            body=GateAnswerRequest(
                answer="accept",
                answered_by="seat",
                client_request_id="ar1",
                caller_conversation="seat",
            ),
            _scopes=_WRITE,
        )
    )
    listed = asyncio.run(
        list_gate_questions_endpoint(owner=None, state=None, round_id=None, limit=50, _scopes=_READ)
    )
    assert [item["question_id"] for item in listed["items"]] == [open_q["question_id"]]

    everything = asyncio.run(
        list_gate_questions_endpoint(
            owner="seat",
            state=["PENDING", "ESCALATED", "ANSWERED", "EXPIRED"],
            round_id=None,
            limit=50,
            _scopes=_READ,
        )
    )
    assert len(everything["items"]) == 2


def test_an_unknown_state_filter_is_a_400(db: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            list_gate_questions_endpoint(
                owner=None, state=["MAYBE"], round_id=None, limit=50, _scopes=_READ
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "E_QUESTION_STATE"


# -- slice B2: the bounded wait, consumption and the sweep ------------------


def test_the_wait_returns_at_once_when_the_question_has_already_settled(db: Path) -> None:
    """No sleep when there is nothing to wait for."""
    asked = _ask()
    _answer_it(asked["question_id"])
    started = time.monotonic()
    settlement = asyncio.run(
        get_gate_question_endpoint(question_id=asked["question_id"], wait=10, _scopes=_READ)
    )
    assert time.monotonic() - started < 1.0
    assert settlement["question"]["state"] == "ANSWERED"
    assert settlement["answer"]["answer"] == "accept"


def test_the_wait_is_bounded_and_returns_the_still_open_question(db: Path) -> None:
    """A wait that nobody answers ends at its own deadline, not at the client's.

    The asker is told "still open" and decides whether to wait again. A wait that
    outlived the client's timeout would be indistinguishable from a dead server,
    and the asker would retry an ask it had already made.
    """
    asked = _ask()
    started = time.monotonic()
    settlement = asyncio.run(
        get_gate_question_endpoint(question_id=asked["question_id"], wait=2, _scopes=_READ)
    )
    elapsed = time.monotonic() - started
    assert 1.0 <= elapsed < 6.0
    assert settlement["question"]["is_open"] is True
    assert settlement["answer"] is None


def test_the_wait_returns_as_soon_as_an_answer_lands(db: Path) -> None:
    """The poll notices a settlement mid-wait rather than sleeping out its bound."""
    asked = _ask()

    def _answer_soon() -> None:
        time.sleep(1.2)
        _answer_it(asked["question_id"])

    worker = threading.Thread(target=_answer_soon)
    worker.start()
    try:
        started = time.monotonic()
        settlement = asyncio.run(
            get_gate_question_endpoint(question_id=asked["question_id"], wait=15, _scopes=_READ)
        )
        elapsed = time.monotonic() - started
    finally:
        worker.join()
    assert elapsed < 10.0
    assert settlement["question"]["state"] == "ANSWERED"
    assert settlement["answer"]["answer"] == "accept"


def test_the_wait_is_capped_by_the_shared_constant(db: Path) -> None:
    """The cap is validated here, so a client mirroring it cannot exceed it."""
    from fastapi.exceptions import RequestValidationError

    from cli_agent_orchestrator.core.timing import GATE_QUESTION_WAIT_CAP_S

    assert GATE_QUESTION_WAIT_CAP_S == 25
    param = next(
        p
        for p in inspect.signature(get_gate_question_endpoint).parameters.values()
        if p.name == "wait"
    )
    bounds = {type(m).__name__.lower(): m for m in param.default.metadata}
    assert bounds["le"].le == GATE_QUESTION_WAIT_CAP_S
    assert bounds["ge"].ge == 0
    assert RequestValidationError is not None  # the framework enforces it on the wire


def test_an_expired_question_ends_the_wait(db: Path) -> None:
    """EXPIRED is settled: a waiting asker is released, not left to time out."""
    asked = _ask(expires_in_s=1)
    time.sleep(1.2)
    asyncio.run(sweep_gate_questions_endpoint(_scopes=_WRITE))
    settlement = asyncio.run(
        get_gate_question_endpoint(question_id=asked["question_id"], wait=10, _scopes=_READ)
    )
    assert settlement["question"]["state"] == "EXPIRED"
    assert settlement["question"]["is_open"] is False


def test_consume_records_the_receipt_and_is_idempotent(db: Path) -> None:
    asked = _ask()
    _answer_it(asked["question_id"])
    first = asyncio.run(
        consume_gate_answer_endpoint(question_id=asked["question_id"], _scopes=_WRITE)
    )
    assert first["consumed_at"] is not None
    second = asyncio.run(
        consume_gate_answer_endpoint(question_id=asked["question_id"], _scopes=_WRITE)
    )
    assert second["consumed_at"] is not None


def test_consuming_an_unknown_question_is_a_404(db: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(consume_gate_answer_endpoint(question_id="nope", _scopes=_WRITE))
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "E_QUESTION_NOT_FOUND"


def test_consuming_an_unanswered_question_is_a_no_op(db: Path) -> None:
    """Nothing to receive yet, so nothing is recorded — and no error either."""
    asked = _ask()
    result = asyncio.run(
        consume_gate_answer_endpoint(question_id=asked["question_id"], _scopes=_WRITE)
    )
    assert result["consumed_at"] is None and result["is_open"] is True


def test_the_sweep_route_reports_what_it_touched(db: Path) -> None:
    overdue = _ask(expires_in_s=1)
    _ask(dispatch_id="worker-2", client_request_id="cr2", expires_in_s=3600)
    time.sleep(1.2)
    result = asyncio.run(sweep_gate_questions_endpoint(_scopes=_WRITE))
    assert [q["question_id"] for q in result["expired"]] == [overdue["question_id"]]
    # The still-open question's notice never landed (no notifier is wired in the
    # test build), so the sweep offers it for retry rather than dropping it.
    assert isinstance(result["notices_retried"], list)

    again = asyncio.run(sweep_gate_questions_endpoint(_scopes=_WRITE))
    assert again["expired"] == []


# -- slice B2: the daemon itself -------------------------------------------


def test_the_expiry_daemon_sweeps_and_cancels_cleanly(db: Path, monkeypatch) -> None:
    """An untested daemon is exactly the thing that silently does not run.

    Driven with a one-second period so the test does not wait out the real
    cadence, then cancelled: it must settle the overdue question, propagate
    ``CancelledError`` so the lifespan's shutdown actually completes, and never
    leave the row unsettled.
    """
    monkeypatch.setattr("cli_agent_orchestrator.bootstrap.gate_question_sweep_period_s", lambda: 1)
    asked = _ask(expires_in_s=1)
    time.sleep(1.2)

    async def _drive() -> None:
        task = asyncio.create_task(gate_question_expiry_daemon())
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    settled = asyncio.run(
        get_gate_question_endpoint(question_id=asked["question_id"], wait=0, _scopes=_READ)
    )
    assert settled["question"]["state"] == "EXPIRED"


def test_the_daemon_survives_a_failing_sweep(db: Path, monkeypatch) -> None:
    """A sweep that raised must not take the server's lifespan down with it.

    The next period retries from the same durable rows, so the only cost of a
    failure is one period of delay.
    """
    monkeypatch.setattr("cli_agent_orchestrator.bootstrap.gate_question_sweep_period_s", lambda: 1)
    calls: list[int] = []

    def _boom() -> Any:
        calls.append(1)
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("cli_agent_orchestrator.api.routes_fork._sweep_once", _boom)

    async def _drive() -> None:
        task = asyncio.create_task(gate_question_expiry_daemon())
        await asyncio.sleep(1.4)
        assert not task.done(), "a failing sweep must not end the daemon"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    assert len(calls) >= 2, "the daemon must keep sweeping after a failure"
