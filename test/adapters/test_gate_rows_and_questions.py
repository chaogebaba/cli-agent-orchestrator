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

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.gate import SqliteGateStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.core import gate as g

TEST_BUSY_TIMEOUT_MS = 5000

#: The fixed "now" the slice-B1 question arms are written against.
_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


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


# -- slice B1: the question transactions ------------------------------------


def _dispatch(
    store: SqliteGateStore,
    dispatch_id: str,
    *,
    round_id: str | None = None,
    state: g.DispatchState = g.DispatchState.DISPATCHED,
    role: g.DispatchRole = g.DispatchRole.OTHER,
    position: str = "dev",
) -> None:
    """Record a dispatch. DISPATCHED by default: a lane that is actually running.

    The state matters now that a release RESTORES it rather than fabricating
    ``DISPATCHED``, so the arms below say which state they mean.
    """
    store.record_dispatch(
        g.Dispatch(
            dispatch_id=dispatch_id,
            round_id=round_id,
            role=role,
            position=position,
            request_id=dispatch_id,
            state=state,
        )
    )


def _ask(
    store: SqliteGateStore,
    *,
    dispatch_id: str = "d1",
    request_id: str = "cr1",
    round_id: str | None = None,
    owner: str = "c1",
    epoch: int = 1,
    ttl_s: int = 3600,
) -> g.RoundQuestion:
    record, _replayed = store.ask_question(
        dispatch_id=dispatch_id,
        round_id=round_id,
        client_request_id=request_id,
        owner_conversation=owner,
        owner_epoch=epoch,
        continuation_kind=g.ContinuationKind.ASSIGNMENT,
        continuation_ref=dispatch_id,
        question="accept, re-round or override?",
        options=("accept", "re-round"),
        blocking=True,
        asked_at=_NOW,
        expires_at=_NOW + timedelta(seconds=ttl_s),
    )
    return record


def test_ask_suspends_the_dispatch_and_records_the_notice_intent(
    store: SqliteGateStore,
) -> None:
    """One transaction: the question, the suspension and the obligation to tell.

    All three or none.  The dispatch reading ``awaiting_answer`` is what makes
    ``run_awaiting_answer`` true, and the PENDING notice intent is what stops a
    question from being something nobody will ever hear about.
    """
    _dispatch(store, "d1")
    question = _ask(store)
    assert question.state is g.QuestionState.PENDING
    conn = store._pool.connection()  # noqa: SLF001
    assert (
        conn.execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'").fetchone()[0]
        == g.DispatchState.AWAITING_ANSWER.value
    )
    notice = conn.execute(
        "SELECT state, msg_id, attempts FROM question_notice_intent WHERE question_id = ?",
        (question.question_id,),
    ).fetchone()
    assert notice["state"] == "PENDING" and notice["msg_id"] is None and notice["attempts"] == 0


def test_ask_is_idempotent_by_client_request_id(store: SqliteGateStore) -> None:
    """A retried ask returns ITS OWN question, never "you already have one open".

    The order inside the transaction is what this pins: if the open-slot check ran
    first, a blocking lane retrying after a transport timeout would be refused for
    the question it asked itself, which turns a recoverable retry into a wedge.
    """
    _dispatch(store, "d1")
    first = _ask(store)
    second = _ask(store)
    assert second.question_id == first.question_id
    count = (
        store._pool.connection()  # noqa: SLF001
        .execute("SELECT COUNT(*) FROM round_question")
        .fetchone()[0]
    )
    assert count == 1


def test_ac_a10_a_second_open_question_is_a_typed_refusal(store: SqliteGateStore) -> None:
    """The index still decides; the adapter makes it branchable (AC-A10)."""
    _dispatch(store, "d1")
    _ask(store)
    with pytest.raises(g.GateQuestionError) as exc:
        _ask(store, request_id="cr2")
    assert exc.value.code is g.QuestionRefusal.QUESTION_OPEN


def test_ac_a10_an_escalated_question_still_holds_the_slot(store: SqliteGateStore) -> None:
    """ESCALATED is open: escalating does not free the dispatch to ask again."""
    _dispatch(store, "d1")
    first = _ask(store)
    escalated = store.escalate_question(first.question_id, now=_NOW)
    assert escalated.state is g.QuestionState.ESCALATED
    assert store.open_question_for_dispatch("d1") is not None
    with pytest.raises(g.GateQuestionError) as exc:
        _ask(store, request_id="cr2")
    assert exc.value.code is g.QuestionRefusal.QUESTION_OPEN


def test_ask_refuses_an_unknown_dispatch(store: SqliteGateStore) -> None:
    with pytest.raises(g.GateQuestionError) as exc:
        _ask(store, dispatch_id="nope")
    assert exc.value.code is g.QuestionRefusal.DISPATCH_UNKNOWN


def test_answer_records_the_event_and_its_delivery_intent(store: SqliteGateStore) -> None:
    """ANSWERED is not proof of receipt: the delivery intent is a separate row (R28)."""
    _dispatch(store, "d1")
    question = _ask(store)
    settled, event = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(minutes=1),
    )
    assert settled.state is g.QuestionState.ANSWERED
    assert settled.answer_event_id == event.answer_event_id
    conn = store._pool.connection()  # noqa: SLF001
    intent = conn.execute(
        "SELECT state, settled_at FROM answer_delivery_intent WHERE answer_event_id = ?",
        (event.answer_event_id,),
    ).fetchone()
    assert intent["state"] == "PENDING" and intent["settled_at"] is None
    # The question no longer holds the slot, but the ASKER has not received the
    # answer yet — so the dispatch is still suspended.
    assert store.open_question_for_dispatch("d1") is None
    assert (
        conn.execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'").fetchone()[0]
        == g.DispatchState.AWAITING_ANSWER.value
    )


def test_consuming_the_answer_is_what_releases_the_dispatch(store: SqliteGateStore) -> None:
    _dispatch(store, "d1")
    question = _ask(store)
    _settled, event = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(minutes=1),
    )
    store.mark_answer_consumed(event.answer_event_id, now=_NOW + timedelta(minutes=2))
    conn = store._pool.connection()  # noqa: SLF001
    assert (
        conn.execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'").fetchone()[0]
        == g.DispatchState.DISPATCHED.value
    )
    assert (
        conn.execute(
            "SELECT state FROM answer_delivery_intent WHERE answer_event_id = ?",
            (event.answer_event_id,),
        ).fetchone()[0]
        == "CONSUMED"
    )
    reloaded = store.get_question(question.question_id)
    assert reloaded is not None and reloaded.consumed_at is not None


def test_an_identical_answer_retry_returns_the_recorded_one(store: SqliteGateStore) -> None:
    _dispatch(store, "d1")
    question = _ask(store)
    _first, event = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(minutes=1),
    )
    _second, replay = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(minutes=2),
    )
    assert replay.answer_event_id == event.answer_event_id
    count = (
        store._pool.connection()  # noqa: SLF001
        .execute("SELECT COUNT(*) FROM question_answer")
        .fetchone()[0]
    )
    assert count == 1


def test_a_different_answer_under_the_same_request_id_is_a_conflict(
    store: SqliteGateStore,
) -> None:
    """Overwriting would change a decision the asker may already have acted on."""
    _dispatch(store, "d1")
    question = _ask(store)
    store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(minutes=1),
    )
    with pytest.raises(g.GateQuestionError) as exc:
        store.answer_question(
            question_id=question.question_id,
            answer="re-round",
            answered_by="seat",
            client_request_id="ar1",
            caller_conversation="c1",
            caller_epoch=1,
            now=_NOW + timedelta(minutes=2),
        )
    assert exc.value.code is g.QuestionRefusal.ANSWER_CONFLICT


def test_the_answer_versus_expiry_race_settles_exactly_once(store: SqliteGateStore) -> None:
    """Whichever settles first wins and the other is refused — never both.

    Both directions matter, because the property is not "expiry loses" but "the
    second writer is refused": an answer arriving after the sweep must not
    resurrect the row, and a sweep running after an answer must not overwrite it.
    """
    # Arm 1: the sweep gets there first; the late answer is refused.
    _dispatch(store, "d1")
    swept_first = _ask(store, dispatch_id="d1", request_id="cr1", ttl_s=60)
    expired = store.expire_due_questions(_NOW + timedelta(seconds=61))
    assert [q.question_id for q in expired] == [swept_first.question_id]
    with pytest.raises(g.GateQuestionError) as late:
        store.answer_question(
            question_id=swept_first.question_id,
            answer="accept",
            answered_by="seat",
            client_request_id="ar1",
            caller_conversation="c1",
            caller_epoch=1,
            now=_NOW + timedelta(seconds=62),
        )
    assert late.value.code is g.QuestionRefusal.QUESTION_SETTLED

    # Arm 2: the answer gets there first; a later sweep leaves it alone.
    _dispatch(store, "d2")
    answered_first = _ask(store, dispatch_id="d2", request_id="cr2", ttl_s=60)
    store.answer_question(
        question_id=answered_first.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar2",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(seconds=1),
    )
    assert store.expire_due_questions(_NOW + timedelta(seconds=61)) == []
    reloaded = store.get_question(answered_first.question_id)
    assert reloaded is not None and reloaded.state is g.QuestionState.ANSWERED


def test_expiry_releases_the_dispatch_and_returns_only_what_it_settled(
    store: SqliteGateStore,
) -> None:
    """AC-A7: one anomaly per settled question, so the sweep returns the rows it wrote."""
    _dispatch(store, "d1")
    question = _ask(store, ttl_s=60)
    first = store.expire_due_questions(_NOW + timedelta(seconds=61))
    second = store.expire_due_questions(_NOW + timedelta(seconds=62))
    assert [q.question_id for q in first] == [question.question_id]
    assert second == []  # a second sweep has nothing to notify about
    assert (
        store._pool.connection()  # noqa: SLF001
        .execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'")
        .fetchone()[0]
        == g.DispatchState.DISPATCHED.value
    )


def test_answer_refuses_after_a_claim_round_bound_and_round_free(
    store: SqliteGateStore,
) -> None:
    """AC-A12 through the ANSWER path, on a round-bound AND a NULL-round question."""
    _run, rnd = _open_run_and_round(store)
    _dispatch(store, "d1", round_id=rnd.round_id)
    _dispatch(store, "d2")
    bound = _ask(store, dispatch_id="d1", request_id="cr1", round_id=rnd.round_id)
    free = _ask(store, dispatch_id="d2", request_id="cr2", round_id=None)

    result = store.claim_ownership(
        prior_conversation="c1",
        new_conversation="c2",
        new_epoch=7,
        client_request_id="claim1",
        claimed_by="c2",
    )
    assert result.accepted and result.questions_rewritten == 2

    for question in (bound, free):
        with pytest.raises(g.GateQuestionError) as exc:
            store.answer_question(
                question_id=question.question_id,
                answer="accept",
                answered_by="c1",
                client_request_id=f"ar-{question.question_id}",
                caller_conversation="c1",
                caller_epoch=1,
                now=_NOW + timedelta(minutes=1),
            )
        # The rewrite moved the row to c2@7, so c1 is now a different owner.
        assert exc.value.code is g.QuestionRefusal.OWNER_MISMATCH
        with pytest.raises(g.GateQuestionError) as stale:
            store.answer_question(
                question_id=question.question_id,
                answer="accept",
                answered_by="c2",
                client_request_id=f"ar2-{question.question_id}",
                caller_conversation="c2",
                caller_epoch=6,
                now=_NOW + timedelta(minutes=1),
            )
        assert stale.value.code is g.QuestionRefusal.EPOCH_SUPERSEDED


def test_project_round_carries_the_round_s_questions(store: SqliteGateStore) -> None:
    """AC-A1's re-projection shows what the round is waiting on, from rows alone."""
    _run, rnd = _open_run_and_round(store)
    _dispatch(store, "d1", round_id=rnd.round_id)
    question = _ask(store, round_id=rnd.round_id)
    projection = store.project_round(rnd.round_id)
    assert projection is not None
    assert [q.question_id for q in projection.questions] == [question.question_id]


def test_notice_intent_settles_sent_and_failed_distinctly(store: SqliteGateStore) -> None:
    """FAILED is retryable and deliberately distinct from never-attempted PENDING."""
    _dispatch(store, "d1")
    question = _ask(store)
    store.mark_notice_failed(question.question_id, error="transport down")
    conn = store._pool.connection()  # noqa: SLF001
    row = conn.execute(
        "SELECT state, attempts, last_error FROM question_notice_intent WHERE question_id = ?",
        (question.question_id,),
    ).fetchone()
    assert row["state"] == "FAILED" and row["attempts"] == 1 and row["last_error"]
    store.mark_notice_sent(question.question_id, msg_id="4242")
    row = conn.execute(
        "SELECT state, attempts, msg_id, last_error FROM question_notice_intent "
        "WHERE question_id = ?",
        (question.question_id,),
    ).fetchone()
    assert row["state"] == "SENT" and row["attempts"] == 2 and row["msg_id"] == "4242"
    assert row["last_error"] is None


def test_questions_for_owner_filters_by_owner_state_and_round(store: SqliteGateStore) -> None:
    _run, rnd = _open_run_and_round(store)
    _dispatch(store, "d1", round_id=rnd.round_id)
    _dispatch(store, "d2")
    bound = _ask(store, dispatch_id="d1", request_id="cr1", round_id=rnd.round_id)
    free = _ask(store, dispatch_id="d2", request_id="cr2")
    store.escalate_question(free.question_id, now=_NOW)

    assert {q.question_id for q in store.questions_for_owner(owner_conversation="c1")} == {
        bound.question_id,
        free.question_id,
    }
    assert [
        q.question_id for q in store.questions_for_owner(states=(g.QuestionState.ESCALATED,))
    ] == [free.question_id]
    assert [q.question_id for q in store.questions_for_owner(round_id=rnd.round_id)] == [
        bound.question_id
    ]
    assert store.questions_for_owner(owner_conversation="nobody") == []


# -- B1 r2: the dispatch guard, provisioning, escalation and the replay flag --


@pytest.mark.parametrize(
    "terminal",
    [g.DispatchState.RETURNED, g.DispatchState.FAILED, g.DispatchState.ABANDONED],
)
def test_an_ask_on_a_settled_dispatch_is_refused(
    store: SqliteGateStore, terminal: g.DispatchState
) -> None:
    """A finished lane has nobody left to answer to (review blocker B2).

    Resurrecting it would make ``run_awaiting_answer`` true for the WHOLE run,
    permanently, and there would be no record of what the dispatch had been.
    Reachable from MCP ``ask_supervisor``, which passes the caller's terminal id
    straight through as the dispatch id.

    MUTANT: stop reading ``dispatch_row["state"]`` and this goes red on all three.
    """
    _dispatch(store, "d1", state=terminal)
    with pytest.raises(g.GateQuestionError) as exc:
        _ask(store)
    assert exc.value.code is g.QuestionRefusal.DISPATCH_SETTLED
    # And the refusal changed nothing.
    assert (
        store._pool.connection()  # noqa: SLF001
        .execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'")
        .fetchone()[0]
        == terminal.value
    )


@pytest.mark.parametrize("prior", [g.DispatchState.PREPARED, g.DispatchState.DISPATCHED])
def test_a_release_restores_the_state_the_ask_suspended(
    store: SqliteGateStore, prior: g.DispatchState
) -> None:
    """Both release paths restore, rather than fabricating ``DISPATCHED``.

    A PREPARED dispatch that asked a question used to come back DISPATCHED — a
    state it had never been in. The ask records what it suspended and the
    releases put it back.
    """
    _dispatch(store, "d1", state=prior)
    _dispatch(store, "d2", state=prior)
    expiring = _ask(store, dispatch_id="d1", request_id="cr1", ttl_s=60)
    consumed = _ask(store, dispatch_id="d2", request_id="cr2")
    assert store.get_question(expiring.question_id).dispatch_prior_state is prior

    store.expire_due_questions(_NOW + timedelta(seconds=61))
    _settled, event = store.answer_question(
        question_id=consumed.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(seconds=1),
    )
    store.mark_answer_consumed(event.answer_event_id, now=_NOW + timedelta(seconds=2))

    conn = store._pool.connection()  # noqa: SLF001
    for dispatch_id in ("d1", "d2"):
        state = conn.execute(
            "SELECT state FROM gate_dispatch WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()[0]
        assert state == prior.value, f"{dispatch_id} was not restored to {prior.value}"


def test_ensure_dispatch_never_clobbers_an_existing_one(store: SqliteGateStore) -> None:
    """N1/N2: a live BUILDER survives an ask that names another position.

    One statement, so the check and the act cannot be separated. The regression
    this protects is concrete: ``record_dispatch`` UPSERTS, so an unguarded
    provision turned a BUILDER mid-round into a PREPARED ``OTHER`` because a lane
    happened to pass ``position``.
    """
    _dispatch(
        store,
        "b1",
        state=g.DispatchState.DISPATCHED,
        role=g.DispatchRole.BUILDER,
        position="dev",
    )
    inserted = store.ensure_dispatch(
        g.Dispatch(
            dispatch_id="b1",
            role=g.DispatchRole.OTHER,
            position="grunt",
            request_id="b1",
            state=g.DispatchState.PREPARED,
        )
    )
    assert inserted is False
    survivor = store.get_dispatch("b1")
    assert survivor is not None
    assert survivor.role is g.DispatchRole.BUILDER
    assert survivor.position == "dev"
    assert survivor.state is g.DispatchState.DISPATCHED


def test_ensure_dispatch_inserts_when_there_is_none(store: SqliteGateStore) -> None:
    assert store.get_dispatch("fresh") is None
    assert (
        store.ensure_dispatch(
            g.Dispatch(
                dispatch_id="fresh",
                role=g.DispatchRole.OTHER,
                position="dev",
                request_id="fresh",
            )
        )
        is True
    )
    assert store.get_dispatch("fresh") is not None


def test_escalating_an_overdue_question_is_refused(store: SqliteGateStore) -> None:
    """N10: escalating into a state the next sweep expires spends attention for nothing."""
    _dispatch(store, "d1")
    question = _ask(store, ttl_s=60)
    with pytest.raises(g.GateQuestionError) as exc:
        store.escalate_question(question.question_id, now=_NOW + timedelta(seconds=61))
    assert exc.value.code is g.QuestionRefusal.QUESTION_EXPIRED
    reloaded = store.get_question(question.question_id)
    assert reloaded is not None and reloaded.state is g.QuestionState.PENDING


def test_the_ask_reports_whether_it_replayed(store: SqliteGateStore) -> None:
    """N6: callers derive their keys, so a repeat is invisible unless we say so.

    ``gate_round.py`` (slice C) asks "accept | re-round | override?" once per
    round from the same lane; without the flag, round 2 would silently receive
    round 1's answer and nobody would know nothing new had been recorded.
    """
    _dispatch(store, "d1")
    kwargs = dict(
        dispatch_id="d1",
        round_id=None,
        client_request_id="cr1",
        owner_conversation="c1",
        owner_epoch=1,
        continuation_kind=g.ContinuationKind.ASSIGNMENT,
        continuation_ref="d1",
        question="accept?",
        options=(),
        blocking=True,
        asked_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    first, replayed_first = store.ask_question(**kwargs)  # type: ignore[arg-type]
    second, replayed_second = store.ask_question(**kwargs)  # type: ignore[arg-type]
    assert replayed_first is False
    assert replayed_second is True
    assert second.question_id == first.question_id


def test_two_concurrent_asks_race_on_the_index_and_one_is_refused() -> None:
    """N4: a REAL interleaving, not two ordered calls.

    The ``IntegrityError -> E_QUESTION_OPEN`` translation was the one branch the
    suite could not reach, and it is the branch that decides what a lane sees
    when the index — not the SELECT — is what actually rejects it. Two threads
    over one pool, each with its own connection, both asking on the same
    dispatch: exactly one may win, and the loser must get a typed code rather
    than a leaked sqlite error.
    """
    import tempfile
    import threading

    path = Path(tempfile.mkdtemp()) / "race.db"
    _res, pool = migrate(path, busy_timeout_ms=10_000)
    assert pool is not None
    store = SqliteGateStore(pool, clock=FakeClock())
    _dispatch(store, "d1")

    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def _ask_once(request_id: str) -> None:
        barrier.wait()
        try:
            record, replayed = store.ask_question(
                dispatch_id="d1",
                round_id=None,
                client_request_id=request_id,
                owner_conversation="c1",
                owner_epoch=1,
                continuation_kind=g.ContinuationKind.ASSIGNMENT,
                continuation_ref="d1",
                question=f"q {request_id}?",
                options=(),
                blocking=True,
                asked_at=_NOW,
                expires_at=_NOW + timedelta(hours=1),
            )
            with lock:
                results.append((record.question_id, replayed))
        except Exception as exc:  # noqa: BLE001 — the arm is about WHICH exception
            with lock:
                results.append(exc)

    threads = [threading.Thread(target=_ask_once, args=(f"cr{n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    winners = [r for r in results if isinstance(r, tuple)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1, f"exactly one ask may win, got {results}"
    assert len(losers) == 1
    loser = losers[0]
    assert isinstance(loser, g.GateQuestionError), f"leaked {type(loser).__name__}: {loser}"
    assert loser.code is g.QuestionRefusal.QUESTION_OPEN
    # And the database agrees: one open question on that dispatch, not two.
    assert (
        pool.connection()
        .execute("SELECT COUNT(*) FROM round_question WHERE dispatch_id='d1'")
        .fetchone()[0]
        == 1
    )


# -- slice B2: the sweep's shape under a live server -------------------------


def test_an_idle_sweep_takes_no_write_lock(tmp_path: Path) -> None:
    """The property the 30-second daemon period turns on (B2).

    An unconditional ``BEGIN IMMEDIATE`` would take the database's write lock
    twice a minute forever, to discover almost always that nothing had expired —
    beside a status monitor that wants the same lock.  Here a SECOND connection
    holds an exclusive transaction while the sweep runs: a sweep that tried to
    write would block and then raise ``database is locked``, so returning
    cleanly IS the proof that it only read.

    MUTANT: wrap the whole of ``expire_due_questions`` in
    ``immediate_transaction`` and this arm goes red.
    """
    path = tmp_path / "gate.db"
    _res, pool = migrate(path, busy_timeout_ms=200)
    assert pool is not None
    store = SqliteGateStore(pool, clock=FakeClock())
    _dispatch(store, "d1")
    _ask(store, ttl_s=3600)  # open, nowhere near its deadline

    blocker = sqlite3.connect(str(path), isolation_level=None, timeout=0.2)
    blocker.execute("PRAGMA busy_timeout = 200")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert store.expire_due_questions(_NOW) == []
        assert store.notices_to_retry()  # the retry read is lock-free too
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_a_sweep_writes_once_per_period_not_once_per_row(tmp_path: Path) -> None:
    """Ten overdue questions cost ONE update statement, not ten.

    A per-row loop holds the write lock in proportion to the backlog, which is
    exactly backwards: the worst pile-up would cause the longest stall for
    everyone else.  The statement trace is the only way to state this that a
    refactor cannot quietly break.
    """
    path = tmp_path / "gate.db"
    _res, pool = migrate(path, busy_timeout_ms=5000)
    assert pool is not None
    store = SqliteGateStore(pool, clock=FakeClock())
    for n in range(10):
        _dispatch(store, f"d{n}")
        _ask(store, dispatch_id=f"d{n}", request_id=f"cr{n}", ttl_s=60)

    statements: list[str] = []
    pool.connection().set_trace_callback(statements.append)
    try:
        expired = store.expire_due_questions(_NOW + timedelta(seconds=61))
    finally:
        pool.connection().set_trace_callback(None)

    assert len(expired) == 10
    question_updates = [
        s for s in statements if s.strip().upper().startswith("UPDATE ROUND_QUESTION")
    ]
    dispatch_updates = [
        s for s in statements if s.strip().upper().startswith("UPDATE GATE_DISPATCH")
    ]
    assert len(question_updates) == 1, question_updates
    assert len(dispatch_updates) == 1, dispatch_updates
    assert sum(1 for s in statements if s.strip().upper().startswith("BEGIN IMMEDIATE")) == 1


def test_a_question_answered_between_the_read_and_the_write_is_not_expired(
    store: SqliteGateStore,
) -> None:
    """The re-check inside the write transaction, stated directly.

    The sweep reads its candidates without a lock, so between that read and the
    write somebody may answer.  The write re-asserts ``state IN
    ('PENDING','ESCALATED')`` over the selected ids, and the RETURNED list is
    what it actually settled — which is what keeps AC-A7 at exactly one anomaly
    per expiry rather than one per candidate.
    """
    _dispatch(store, "d1")
    question = _ask(store, ttl_s=60)
    store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(seconds=1),
    )
    assert store.expire_due_questions(_NOW + timedelta(seconds=61)) == []
    reloaded = store.get_question(question.question_id)
    assert reloaded is not None and reloaded.state is g.QuestionState.ANSWERED


def test_notices_to_retry_covers_pending_and_failed_but_never_a_settled_question(
    store: SqliteGateStore,
) -> None:
    """Re-announcing an answered question would be worse than never announcing it."""
    _dispatch(store, "d1")
    _dispatch(store, "d2")
    _dispatch(store, "d3")
    never_tried = _ask(store, dispatch_id="d1", request_id="cr1")
    failed = _ask(store, dispatch_id="d2", request_id="cr2")
    settled = _ask(store, dispatch_id="d3", request_id="cr3")
    store.mark_notice_failed(failed.question_id, error="database is locked")
    store.answer_question(
        question_id=settled.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(seconds=1),
    )
    assert {q.question_id for q in store.notices_to_retry()} == {
        never_tried.question_id,
        failed.question_id,
    }
    store.mark_notice_sent(never_tried.question_id, msg_id="7")
    assert [q.question_id for q in store.notices_to_retry()] == [failed.question_id]


def test_notices_to_retry_orders_by_attempt_count(store: SqliteGateStore) -> None:
    """A notice that has failed repeatedly must not starve one never tried."""
    _dispatch(store, "d1")
    _dispatch(store, "d2")
    tried_twice = _ask(store, dispatch_id="d1", request_id="cr1")
    fresh = _ask(store, dispatch_id="d2", request_id="cr2")
    store.mark_notice_failed(tried_twice.question_id, error="x")
    store.mark_notice_failed(tried_twice.question_id, error="x")
    assert [q.question_id for q in store.notices_to_retry()] == [
        fresh.question_id,
        tried_twice.question_id,
    ]


def test_get_answer_returns_the_recorded_event(store: SqliteGateStore) -> None:
    _dispatch(store, "d1")
    question = _ask(store)
    _settled, event = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="c1",
        caller_epoch=1,
        now=_NOW + timedelta(minutes=1),
    )
    recorded = store.get_answer(event.answer_event_id)
    assert recorded is not None and recorded.answer == "accept"
    assert store.get_answer("nope") is None
