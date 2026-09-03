"""Claim attacked: ready completion at the quiescence deadline has one branch.

Round: WPM4-A diff gate r9.
Expected post-fix semantics: a successful decided commit racing the exact
budget edge yields either joined-ready success or mutation-in-flight followed
by one late reconciliation, never dual/neither ownership or a false timeout.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r9/test_ready_deadline_edge_probe.py
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.services import terminal_service as terminals


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'deadline-edge.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.slow  # F254 D19: exceeds unit budget
async def test_ready_completion_at_deadline_has_one_lawful_owner(
    isolated_db,
    monkeypatch,
):
    original_do_commit = isolated_db.dialect.do_commit
    outcomes: list[str] = []
    monkeypatch.setattr(terminals, "_confirm_launch_health", AsyncMock())

    # F153 (#17): the branch taken on each iteration is CHOSEN, not raced.
    # The probe previously started a threading.Timer with a 5/10/15 ms delay
    # against a 10 ms quiesce budget and asserted only that both branches
    # appeared at least 15 times in 60 runs — a distribution that depends on
    # scheduler jitter and flaked under load. Release sequencing now decides the
    # branch up front:
    #   joined_ready       — the blocked commit is released BEFORE quiesce runs
    #                        and quiesce is given a budget it cannot miss, so it
    #                        must join the decided commit.
    #   mutation_in_flight — the commit stays blocked across a 10 ms budget, so
    #                        quiesce must time out with the mutation in flight.
    # Every post-condition the probe cared about is unchanged and now asserted
    # per iteration against the branch that was chosen, which is strictly
    # stronger than the old aggregate bound.
    branches = ["joined_ready", "mutation_in_flight"] * 30

    for iteration, branch in enumerate(branches):
        terminal_id = f"ready-edge-{iteration}"
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
        entered = threading.Event()
        release = threading.Event()

        def blocked_commit(connection):
            entered.set()
            release.wait(1)
            original_do_commit(connection)

        monkeypatch.setattr(isolated_db.dialect, "do_commit", blocked_commit)
        provider = SimpleNamespace(
            initialize=AsyncMock(),
            supports_reauth_rebind=False,
            shell_baseline=None,
        )
        terminals._schedule_deferred_init(
            provider,
            terminal_id,
            None,
            None,
            None,
            caller_snapshot={
                "caller_id": "caller",
                "agent_profile": "developer",
                "provider": "grok_cli",
                "init_deadline_s": 3.0,
            },
        )
        assert await asyncio.to_thread(entered.wait, 1)
        record = terminals._deferred_tasks_by_terminal[terminal_id]
        registered_call = record.current_call
        assert registered_call is not None

        if branch == "joined_ready":
            release.set()
            timeout_s = 5.0
        else:
            timeout_s = 0.010
        try:
            await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=timeout_s)
        except RuntimeError as exc:
            assert str(exc) == "quiesce_timeout_mutation_in_flight"
            outcomes.append("mutation_in_flight")
        else:
            outcomes.append("joined_ready")
        finally:
            release.set()
        assert outcomes[-1] == branch, f"iteration {iteration} took the unchosen branch"

        # F153 (#17): settling is asynchronous on BOTH branches — on the
        # mutation_in_flight branch the row is written by the late reconciler,
        # which runs after the call future resolves. The old probe waited a
        # fixed 200 x 1 ms only for the future and then read the row, so a
        # reconciler that had not yet committed failed the run (observed at
        # iteration 25 of 60 on an unloaded laptop). Wait for the settled state
        # itself, bounded generously: the probe's claim is that exactly one
        # lawful owner settles the row, not that it settles within 200 ms.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if registered_call.future.done():
                db.invalidate_terminal_metadata_cache(terminal_id)
                if db.get_terminal_metadata(terminal_id)["init_state"] == "ready":
                    break
            await asyncio.sleep(0.001)
        assert registered_call.future.done()
        assert registered_call.future.exception() is None
        assert registered_call.future.result() is True
        db.invalidate_terminal_metadata_cache(terminal_id)
        assert db.get_terminal_metadata(terminal_id)["init_state"] == "ready"

    assert outcomes == branches
