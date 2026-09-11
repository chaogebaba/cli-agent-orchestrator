"""``cao gate ask|answer|escalate|questions|question`` over a scratch DB (slice B1).

Driven through the ``--db`` path on a temp database: the acceptance evidence for
this slice has to be producible with no server, and pointing the live server at a
scratch file is not something a verb should be able to do.  The served path is
the same service call, reached over HTTP — the S13 contract test is what pins
that the two surfaces name one route.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.cli.commands.gate import gate


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = tmp_path / "gate.db"
    result, pool = migrate(path, busy_timeout_ms=5000)
    assert result.ok and pool is not None
    return str(path)


def _run(db: str, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(gate, [*args, "--db", db])
    return result.exit_code, result.output


def test_ask_then_answer_walks_pending_to_answered(db: str) -> None:
    code, out = _run(
        db,
        "ask",
        "accept, re-round or override?",
        "--dispatch",
        "w1",
        "--option",
        "accept",
        "--option",
        "re-round",
        "--request-id",
        "cr1",
        "--json",
    )
    assert code == 0, out
    asked = json.loads(out)
    assert asked["state"] == "PENDING" and asked["options"] == ["accept", "re-round"]

    code, out = _run(db, "questions", "--json")
    assert code == 0 and json.loads(out)["items"][0]["question_id"] == asked["question_id"]

    code, out = _run(db, "answer", asked["question_id"], "accept", "--json")
    assert code == 0, out
    assert json.loads(out)["state"] == "ANSWERED"

    code, out = _run(db, "question", asked["question_id"], "--json")
    assert code == 0 and json.loads(out)["is_open"] is False


def test_a_repeated_request_id_returns_the_same_question(db: str) -> None:
    args = ("ask", "accept?", "--dispatch", "w1", "--request-id", "cr1", "--json")
    first = json.loads(_run(db, *args)[1])
    second = json.loads(_run(db, *args)[1])
    assert second["question_id"] == first["question_id"]


def test_a_second_open_question_on_one_dispatch_is_refused_with_its_code(db: str) -> None:
    _run(db, "ask", "accept?", "--dispatch", "w1", "--request-id", "cr1", "--json")
    code, out = _run(db, "ask", "other?", "--dispatch", "w1", "--request-id", "cr2", "--json")
    assert code != 0
    assert "E_QUESTION_OPEN" in out


def test_escalate_keeps_the_id_and_the_slot(db: str) -> None:
    asked = json.loads(
        _run(db, "ask", "accept?", "--dispatch", "w1", "--request-id", "cr1", "--json")[1]
    )
    code, out = _run(db, "escalate", asked["question_id"], "--json")
    assert code == 0, out
    escalated = json.loads(out)
    assert escalated["question_id"] == asked["question_id"]
    assert escalated["state"] == "ESCALATED" and escalated["is_open"] is True


def test_a_non_blocking_ask_without_a_default_is_refused(db: str) -> None:
    code, out = _run(
        db, "ask", "accept?", "--dispatch", "w1", "--request-id", "cr1", "--non-blocking"
    )
    assert code != 0
    assert "E_DEFAULT_REQUIRED" in out


def test_question_shows_a_human_line_without_json(db: str) -> None:
    asked = json.loads(
        _run(db, "ask", "accept?", "--dispatch", "w1", "--request-id", "cr1", "--json")[1]
    )
    code, out = _run(db, "question", asked["question_id"])
    assert code == 0
    assert asked["question_id"] in out and "[PENDING]" in out and "accept?" in out


def test_questions_says_so_when_there_are_none(db: str) -> None:
    code, out = _run(db, "questions")
    assert code == 0 and "no questions" in out
