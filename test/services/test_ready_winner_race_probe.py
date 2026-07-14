"""Claim attacked: ready commit and timeout must select exactly one winner.

Round: WPM4-A diff gate r8.
Expected post-fix semantics: 100 forced timeout wins leave init_pending and
100 forced commit decisions make ready; no iteration has dual or unset ownership.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r8/test_ready_winner_race_probe.py
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'winner-race.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield
    engine.dispose()


def _pending(terminal_id: str) -> None:
    db.create_terminal(
        terminal_id,
        "cao-s",
        terminal_id,
        "grok_cli",
        "developer",
        caller_id="caller",
        init_state="init_pending",
        init_started_at=db._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=17.0,
    )


def test_200_real_thread_ready_winner_races(isolated_db):
    outcomes: list[tuple[str, bool, str]] = []

    for iteration in range(200):
        terminal_id = f"race-{iteration}"
        _pending(terminal_id)
        lock = threading.Lock()
        winner = ["open"]
        hook_entered = threading.Event()
        allow_hook = threading.Event()

        def should_commit() -> bool:
            with lock:
                return winner[0] != "timeout"

        def decide_commit() -> bool:
            hook_entered.set()
            allow_hook.wait(1)
            with lock:
                if winner[0] == "timeout":
                    return False
                winner[0] = "commit_decided"
                return True

        result: list[bool] = []
        errors: list[BaseException] = []

        def commit_thread() -> None:
            try:
                result.append(
                    db.mark_terminal_init_ready(
                        terminal_id,
                        should_commit=should_commit,
                        decide_commit=decide_commit,
                        commit_is_decided=lambda: winner[0] == "commit_decided",
                    )
                )
            except BaseException as exc:  # pragma: no cover - probe evidence
                errors.append(exc)

        thread = threading.Thread(target=commit_thread)
        thread.start()
        assert hook_entered.wait(1)

        if iteration < 100:
            with lock:
                assert winner[0] == "open"
                winner[0] = "timeout"
            allow_hook.set()
        else:
            allow_hook.set()
            thread.join(1)

        thread.join(1)
        assert not thread.is_alive()
        assert errors == []
        state = db.get_terminal_metadata(terminal_id)["init_state"]
        outcomes.append((winner[0], result[0], state))

    assert outcomes.count(("timeout", False, "init_pending")) == 100
    assert outcomes.count(("commit_decided", True, "ready")) == 100

