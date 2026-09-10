"""The gate rows over SQLite (WP-ARCH Amendment A, slice 2a).

Blueprint name ``test/adapters/store/test_gate_rows_and_questions.py``; placed
flat under ``test/adapters/`` so it inherits this package's ``conftest`` fixtures
(``db_path``, ``FakeClock``) rather than duplicating them — the repo's phase-1
store tests already live flat here (``test_queue_store.py`` etc.).

The 2a row-level arms: A2 (findings carry across rounds, disposition evidence at
the row), A8 (both verification hashes stored and retrievable separately), A10
(one open question per dispatch — the partial unique index), A12
(``claim_ownership`` rewrites rows monotonically), and the row-version conflict
fault arm (a stale write is a typed refusal, not a silent overwrite).  The
question PRIMITIVE is 2b; here we exercise the ROWS and the ownership transaction.
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
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


@pytest.fixture
def store(tmp_path: Path) -> SqliteGateStore:
    path = tmp_path / "gate.db"
    _result, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    return SqliteGateStore(pool, clock=FakeClock())


def _manifest(head: str = "h" * 12) -> g.ArtifactManifest:
    return g.ArtifactManifest(
        manifest_version=1,
        repo_bindings=(g.RepoBinding(name="fork", commit="c" * 12),),
        base_sha="b" * 12,
        head_sha=head,
        branch="cao/x",
        worktree_path="/w",
        entries=(g.DiffEntry(post_path="a.py", post_object_id="o1"),),
        blueprint_sha="bp" * 4,
        ac_list_sha="ac" * 4,
    )


def _open_run_and_round(store: SqliteGateStore) -> tuple[g.GateRun, g.GateRound]:
    run = store.open_run(
        wp="F791", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=3
    )
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    return run, rnd


def test_open_run_rejects_max_rounds_zero(store: SqliteGateStore) -> None:
    with pytest.raises(g.GateError):
        store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=0)


def test_round_numbers_increment_per_run(store: SqliteGateStore) -> None:
    run, r1 = _open_run_and_round(store)
    r2 = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    assert (r1.round_no, r2.round_no) == (1, 2)


# -- AC-A2: findings carry across rounds, disposition closes ----------------


def test_ac_a2_finding_carries_until_disposed(store: SqliteGateStore) -> None:
    run, rnd = _open_run_and_round(store)
    store.raise_finding(
        raised_in_round=rnd.round_id, severity=g.Severity.BLOCKER, statement="X breaks"
    )
    assert len(store.open_findings_for_run(run.run_id)) == 1

    finding = store.open_findings_for_run(run.run_id)[0]
    store.append_disposition(
        finding.finding_id,
        g.Disposition(
            kind=g.DispositionKind.FIXED,
            reviewed_artifact_sha="rev1",
            killer_test="t.py::x",
            killer_mutant="m1",
            at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
    )
    assert store.open_findings_for_run(run.run_id) == []


def test_ac_a2_disposition_round_trips(store: SqliteGateStore) -> None:
    run, rnd = _open_run_and_round(store)
    finding = store.raise_finding(
        raised_in_round=rnd.round_id, severity=g.Severity.SHOULD, statement="Y"
    )
    reloaded = store.append_disposition(
        finding.finding_id,
        g.Disposition(
            kind=g.DispositionKind.WITHDRAWN,
            actor="conv1",
            reason="not a defect",
            at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
    )
    assert reloaded.dispositions[0].kind is g.DispositionKind.WITHDRAWN
    assert reloaded.dispositions[0].actor == "conv1"


# -- AC-A8: both hashes stored and retrievable separately -------------------


def test_ac_a8_both_hashes_persist_separately(store: SqliteGateStore) -> None:
    _run, rnd = _open_run_and_round(store)
    snap = _manifest()
    built = store.freeze_review_snapshot(rnd.round_id, snap, expected_row_version=rnd.row_version)
    adj = store.transition_round(
        built.round_id,
        g.RoundState.ADJUDICATING,
        expected_row_version=built.row_version,
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    stamped = store.set_round_report(
        adj.round_id,
        subject_sha="SUBJ",
        report_bytes_sha="BYTES",
        verdict_report_sha="VR",
        expected_row_version=adj.row_version,
    )
    assert stamped.subject_sha == "SUBJ"
    assert stamped.report_bytes_sha == "BYTES"
    assert stamped.subject_sha != stamped.report_bytes_sha


# -- row-version conflict is a typed refusal (fault arm) --------------------


def test_row_version_conflict_is_refused(store: SqliteGateStore) -> None:
    _run, rnd = _open_run_and_round(store)
    snap = _manifest()
    store.freeze_review_snapshot(rnd.round_id, snap, expected_row_version=rnd.row_version)
    # A second writer holding the STALE version is refused, not silently applied.
    with pytest.raises(g.GateError):
        store.transition_round(
            rnd.round_id,
            g.RoundState.ADJUDICATING,
            expected_row_version=rnd.row_version,  # stale: freeze already bumped it
            now=datetime(2026, 9, 9, tzinfo=UTC),
        )


def test_freeze_review_snapshot_refuses_refreeze(store: SqliteGateStore) -> None:
    _run, rnd = _open_run_and_round(store)
    snap = _manifest()
    built = store.freeze_review_snapshot(rnd.round_id, snap, expected_row_version=rnd.row_version)
    with pytest.raises(g.GateError):
        store.freeze_review_snapshot(rnd.round_id, snap, expected_row_version=built.row_version)


# -- AC-A10: one open question per dispatch (the partial unique index) ------


def test_ac_a10_one_open_question_per_dispatch(store: SqliteGateStore) -> None:
    # Rows-only in 2a: insert directly to prove the index. Two PENDING rows on one
    # dispatch must violate ux_question_open; ESCALATED still holds the slot.
    _run, rnd = _open_run_and_round(store)
    conn = store._pool.connection()  # noqa: SLF001 — a rows-level index assertion
    conn.execute(
        "INSERT INTO round_question (question_id, dispatch_id, round_id, client_request_id, "
        "owner_conversation, owner_epoch, continuation_kind, continuation_ref, asked_at, "
        "expires_at, question, blocking, state, row_version) "
        "VALUES ('q1','d1',?, 'r1','c1',1,'ASSIGNMENT','a', '2026-09-09','2026-09-09','?',1,"
        "'PENDING',1)",
        (rnd.round_id,),
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO round_question (question_id, dispatch_id, round_id, client_request_id, "
            "owner_conversation, owner_epoch, continuation_kind, continuation_ref, asked_at, "
            "expires_at, question, blocking, state, row_version) "
            "VALUES ('q2','d1',?, 'r2','c1',1,'ASSIGNMENT','a','2026-09-09','2026-09-09','?',1,"
            "'ESCALATED',1)",
            (rnd.round_id,),
        )


# -- AC-A12: claim_ownership rewrites rows monotonically --------------------


def _seed_question(
    store: SqliteGateStore, *, dispatch_id: str, owner: str, round_id: str | None
) -> None:
    conn = store._pool.connection()  # noqa: SLF001
    conn.execute(
        "INSERT INTO round_question (question_id, dispatch_id, round_id, client_request_id, "
        "owner_conversation, owner_epoch, continuation_kind, continuation_ref, asked_at, "
        "expires_at, question, blocking, state, row_version) "
        "VALUES (?, ?, ?, ?, ?, 1, 'ASSIGNMENT', 'a', '2026-09-09', '2026-09-09', '?', 1, "
        "'PENDING', 1)",
        (f"q-{dispatch_id}", dispatch_id, round_id, f"cr-{dispatch_id}", owner),
    )


def test_ac_a12_claim_rewrites_runs_and_questions(store: SqliteGateStore) -> None:
    run, rnd = _open_run_and_round(store)
    _seed_question(store, dispatch_id="d1", owner="c1", round_id=rnd.round_id)
    # A round-free (NULL round) non-gate question under the same owner (P1/DESIGN r2 C1).
    _seed_question(store, dispatch_id="d2", owner="c1", round_id=None)

    result = store.claim_ownership(
        prior_conversation="c1",
        new_conversation="c2",
        new_epoch=2,
        client_request_id="req1",
        claimed_by="c2",
    )
    assert result.accepted
    assert result.runs_rewritten == 1
    assert result.questions_rewritten == 2  # round-bound AND round-free

    reloaded = store.get_run(run.run_id)
    assert reloaded is not None
    assert reloaded.owner_conversation == "c2" and reloaded.owner_epoch == 2


def test_ac_a12_non_increasing_epoch_refused(store: SqliteGateStore) -> None:
    store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=5, max_rounds=2)
    result = store.claim_ownership(
        prior_conversation="c1",
        new_conversation="c2",
        new_epoch=5,  # equal, not strictly greater
        client_request_id="req1",
        claimed_by="c2",
    )
    assert not result.accepted


def test_ac_a12_claim_is_idempotent_by_request_id(store: SqliteGateStore) -> None:
    store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    first = store.claim_ownership(
        prior_conversation="c1",
        new_conversation="c2",
        new_epoch=2,
        client_request_id="req1",
        claimed_by="c2",
    )
    second = store.claim_ownership(
        prior_conversation="c1",
        new_conversation="c2",
        new_epoch=2,
        client_request_id="req1",
        claimed_by="c2",
    )
    assert first.transfer_id == second.transfer_id
    assert second.reason == "idempotent replay"


def test_ac_a12_run_id_narrows_the_claim(store: SqliteGateStore) -> None:
    run_a = store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    result = store.claim_ownership(
        prior_conversation="c1",
        new_conversation="c2",
        new_epoch=2,
        run_id=run_a.run_id,
        client_request_id="req1",
        claimed_by="c2",
    )
    assert result.accepted and result.runs_rewritten == 1
