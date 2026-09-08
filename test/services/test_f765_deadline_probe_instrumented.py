"""F765 diagnosis probe (INSTRUMENTED, throwaway) — discriminate H1 vs H2.

Copy of test_ready_deadline_edge_probe's per-iteration logic with logging of
``registered_call.operation`` at trap capture and the future's settled value +
type at the assertion point. NOT a committed regression test — used only to
capture which hypothesis fires under repetition:

  H1 (test-side): the monkeypatched ``blocked_commit`` trap is not selective —
     at capture, ``record.current_call`` is NOT the ready_commit call under test
     (a stale sibling / reconciler commit), so we assert against the wrong call.
  H2 (product-side): the ready_commit callable resolves its future to None (or a
     non-True) instead of True — a deferred-call result-protocol defect.

Prints one DIAG line per iteration to stderr; on any deviation raises with the
captured operation so pytest surfaces it.
"""

from __future__ import annotations

import asyncio
import sys
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
@pytest.mark.slow
async def test_diag_ready_completion_at_deadline(isolated_db, monkeypatch):
    original_do_commit = isolated_db.dialect.do_commit
    outcomes: list[str] = []
    monkeypatch.setattr(terminals, "_confirm_launch_health", AsyncMock())
    branches = ["joined_ready", "mutation_in_flight"] * 30
    AMPLE_S = 30.0
    anomalies: list[str] = []

    for iteration, branch in enumerate(branches):
        terminal_id = f"ready-edge-{iteration}"
        db.create_terminal(
            terminal_id, "cao-s", terminal_id, "grok_cli", "developer",
            caller_id="caller", init_state="init_pending",
            init_started_at=db._utcnow(),
            init_owner_epoch="00000000-0000-0000-0000-000000000001",
            init_deadline_s=17.0,
        )
        entered = threading.Event()
        release = threading.Event()
        # Capture which commit first hits the trap.
        trap_commit_count = {"n": 0}

        def blocked_commit(connection):
            trap_commit_count["n"] += 1
            entered.set()
            release.wait(AMPLE_S)
            original_do_commit(connection)

        monkeypatch.setattr(isolated_db.dialect, "do_commit", blocked_commit)
        provider = SimpleNamespace(
            initialize=AsyncMock(), supports_reauth_rebind=False, shell_baseline=None,
        )
        terminals._schedule_deferred_init(
            provider, terminal_id, None, None, None,
            caller_snapshot={
                "caller_id": "caller", "agent_profile": "developer",
                "provider": "grok_cli", "init_deadline_s": 3.0,
            },
        )
        assert await asyncio.to_thread(entered.wait, AMPLE_S)
        record = terminals._deferred_tasks_by_terminal[terminal_id]
        registered_call = record.current_call
        # ---- H1 discriminator: what op owns the trapped commit at capture? ----
        cap_op = getattr(registered_call, "operation", None)
        cap_type = getattr(registered_call, "call_type", None)
        cap_id = id(registered_call) if registered_call is not None else None
        if registered_call is None or cap_op != "ready_commit":
            anomalies.append(
                f"iter={iteration} branch={branch} H1: captured op={cap_op!r} "
                f"type={cap_type!r} trap_commits={trap_commit_count['n']}"
            )
        assert registered_call is not None

        if branch == "joined_ready":
            release.set()
            timeout_s = AMPLE_S
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

        deadline = time.monotonic() + AMPLE_S
        while time.monotonic() < deadline:
            if registered_call.future.done():
                db.invalidate_terminal_metadata_cache(terminal_id)
                if db.get_terminal_metadata(terminal_id)["init_state"] == "ready":
                    break
            await asyncio.sleep(0.001)

        # ---- H2 discriminator: settled value + type on the ready_commit call ----
        done = registered_call.future.done()
        exc = registered_call.future.exception() if done else "PENDING"
        val = registered_call.future.result() if (done and exc is None) else "N/A"
        sys.stderr.write(
            f"DIAG iter={iteration} branch={branch} outcome={outcomes[-1]} "
            f"cap_op={cap_op!r} cap_id={cap_id} done={done} exc={exc!r} "
            f"result={val!r} result_type={type(val).__name__} "
            f"trap_commits={trap_commit_count['n']}\n"
        )
        if done and exc is None and val is not True:
            anomalies.append(
                f"iter={iteration} branch={branch} H2: result={val!r} "
                f"type={type(val).__name__} cap_op={cap_op!r}"
            )
        # keep going to gather the full distribution rather than bail on first.

    sys.stderr.write(f"DIAG-SUMMARY anomalies={len(anomalies)}\n")
    for a in anomalies:
        sys.stderr.write("DIAG-ANOMALY " + a + "\n")
    assert not anomalies, f"{len(anomalies)} anomalies; first: {anomalies[0]}"
    assert outcomes == branches
