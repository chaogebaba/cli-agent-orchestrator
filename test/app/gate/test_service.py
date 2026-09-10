"""GateRoundService command arms (WP-ARCH Amendment A, slice 2a).

Over the real SQLite store (the service holds no SQL, so a fake would only
re-test the port shape).  Arms: max-rounds exhaustion refused, the full
open->freeze->adjudicate->accept path, accept's two-hash check reached through
the service, and run_effective_state computing AWAITING_ANSWER as a projection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.gate import SqliteGateStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.app.gate.service import GateRoundService
from cli_agent_orchestrator.core import gate as g


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _svc(tmp_path: Path) -> GateRoundService:
    _res, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    return GateRoundService(SqliteGateStore(pool, clock=FakeClock()), clock=FakeClock())


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


def test_open_run_rejects_max_rounds_zero(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(g.GateError):
        svc.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=0)


def test_exhaustion_refuses_extra_round(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    run = svc.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=1)
    svc.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    with pytest.raises(g.GateError):
        svc.open_round(
            run_id=run.run_id,
            build_inputs=_manifest(),
            execution_target=g.ExecutionTarget(host="laptop"),
        )


def test_full_round_to_accept(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    run = svc.open_run(wp="F791", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    rnd = svc.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="grok-box-002"),
    )
    snap = _manifest()
    svc.freeze_review_snapshot(rnd.round_id, snap)
    svc.begin_adjudication(rnd.round_id)
    subject = g.compute_artifact_sha(snap)
    settled = svc.settle_adjudication(
        rnd.round_id,
        verdict_yes=True,
        subject_sha=subject,
        report_bytes_sha="RB",
        verdict_report_sha="VR",
    )
    assert settled.state is g.RoundState.YES
    # A YES permits accept; the two-hash check runs against the CAS bytes-sha.
    svc.accept_round(
        rnd.round_id,
        declared_subject_sha=subject,
        declared_report_bytes_sha="RB",
        cas_report_bytes_sha="RB",
    )
    with pytest.raises(g.GateError):
        svc.accept_round(
            rnd.round_id,
            declared_subject_sha="STALE",
            declared_report_bytes_sha="RB",
            cas_report_bytes_sha="RB",
        )


def test_run_effective_state_projects_awaiting_answer(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    run = svc.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    rnd = svc.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    assert svc.run_effective_state(run.run_id) is g.RunState.OPEN
    # A dispatch suspended AWAITING_ANSWER projects the run as awaiting an answer,
    # without AWAITING_ANSWER ever being written on the run row.
    svc.record_dispatch(
        g.Dispatch(
            dispatch_id="D1",
            round_id=rnd.round_id,
            role=g.DispatchRole.BUILDER,
            position="dev",
            request_id="req1",
            state=g.DispatchState.AWAITING_ANSWER,
        )
    )
    assert svc.run_effective_state(run.run_id) is g.RunState.AWAITING_ANSWER
    reloaded = svc.project_round(rnd.round_id)
    assert reloaded is not None and reloaded.run.state is g.RunState.OPEN  # not written
