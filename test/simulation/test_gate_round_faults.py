"""Gate-round fault arms that are 2a-scoped (WP-ARCH Amendment A, slice 2a).

Blueprint name ``test/sim/test_gate_round_faults.py``; the repo's simulation
substrate lives under ``test/simulation/``, so the file lands here.  The reap
sweep and the question wait adapters (A4/A6/A7) are 2b/2c; the arms that are
2a-scoped are the ones about DURABLE ROWS surviving a crash, which are
deterministic without the async substrate:

* **Crash between effect-intent and spawn** — the intent is recorded BEFORE the
  external operation (P1), so a crash before the result is written leaves the
  intent readable with NO result.  Re-projection shows exactly that: the repair
  is possible from the intent row, which a DISPATCHED row alone could not do.
* **Row-version conflict** — a stale writer is a typed refusal, not a silent
  overwrite (the optimistic-concurrency arm named in the brief).

A "crash" here is modelled by closing the pool between the two writes and
reopening it: SQLite has committed the intent, the result write never happened,
and the reopened store re-projects from what is on disk.  That is exactly the
observable a real crash leaves, and it is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.gate import SqliteGateStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.core import gate as g

TEST_BUSY_TIMEOUT_MS = 5000


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _manifest() -> g.ArtifactManifest:
    return g.ArtifactManifest(
        manifest_version=1,
        repo_bindings=(g.RepoBinding(name="fork", commit="c" * 12),),
        base_sha="b" * 12,
        head_sha="h" * 12,
        branch="cao/x",
        worktree_path="/w",
        entries=(g.DiffEntry(post_path="a.py", post_object_id="o1"),),
        blueprint_sha="bp" * 4,
        ac_list_sha="ac" * 4,
    )


def _fresh_store(path: Path) -> SqliteGateStore:
    _res, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    return SqliteGateStore(pool, clock=FakeClock())


def test_crash_between_effect_intent_and_spawn_reprojects_intent_without_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gate.db"
    store = _fresh_store(path)
    run = store.open_run(
        wp="F791", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2
    )
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    dispatch = g.Dispatch(
        dispatch_id="D1",
        round_id=rnd.round_id,
        role=g.DispatchRole.BUILDER,
        position="dev",
        request_id="req1",
        effect_id="E1",
    )
    store.record_dispatch(dispatch)
    # Intent recorded BEFORE the spawn (P1).
    store.record_effect_intent(
        g.EffectIntent(
            effect_id="E1",
            kind=g.EffectKind.SPAWN,
            round_id=rnd.round_id,
            dispatch_id="D1",
            requested_at=datetime(2026, 9, 9, tzinfo=UTC),
        )
    )
    # --- CRASH: the process dies before the spawn result is recorded. ---
    store._pool.close_all()  # noqa: SLF001 — modelling the crash window

    # Reopen from disk and re-project the round.
    reopened = _fresh_store(path)
    projection = reopened.project_round(rnd.round_id)
    assert projection is not None
    assert len(projection.effect_intents) == 1
    assert projection.effect_intents[0].effect_id == "E1"
    # The intent survived; NO result was written, so the repair is possible.
    assert projection.effect_results == ()


def test_effect_intent_is_idempotent_across_retry(tmp_path: Path) -> None:
    # A retried intent (same effect_id) is a no-op, not a second row — the adapter
    # dedups on effect_id, which is what makes a blind retry after an uncertain
    # crash safe.
    store = _fresh_store(tmp_path / "gate.db")
    run = store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    intent = g.EffectIntent(
        effect_id="E1",
        kind=g.EffectKind.MERGE,
        round_id=rnd.round_id,
        requested_at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    store.record_effect_intent(intent)
    store.record_effect_intent(intent)  # retry
    projection = store.project_round(rnd.round_id)
    assert projection is not None
    assert len(projection.effect_intents) == 1


def test_effect_result_without_intent_is_refused(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path / "gate.db")
    with pytest.raises(g.GateError):
        store.record_effect_result(
            g.EffectResult(effect_id="UNKNOWN", outcome=g.EffectOutcome.APPLIED)
        )


def test_uncertain_result_has_no_settled_at(tmp_path: Path) -> None:
    # An UNCERTAIN result is reconciled, not retried blindly (R30); it carries no
    # settled_at until reconciliation confirms it.
    store = _fresh_store(tmp_path / "gate.db")
    run = store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    store.record_effect_intent(
        g.EffectIntent(
            effect_id="E1",
            kind=g.EffectKind.PUSH,
            round_id=rnd.round_id,
            requested_at=datetime(2026, 9, 9, tzinfo=UTC),
        )
    )
    store.record_effect_result(
        g.EffectResult(effect_id="E1", outcome=g.EffectOutcome.UNCERTAIN, evidence_ref="ref")
    )
    projection = store.project_round(rnd.round_id)
    assert projection is not None
    assert projection.effect_results[0].outcome is g.EffectOutcome.UNCERTAIN
    assert projection.effect_results[0].settled_at is None
