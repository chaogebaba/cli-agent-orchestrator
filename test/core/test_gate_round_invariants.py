"""Pure gate invariants and transitions (WP-ARCH Amendment A, slice 2a).

No database, no clock, no event loop — ``core/gate`` is pure, so these run by
enumeration.  The 2a AC arms this file carries are A1 (no typed shas: the address
is derived and stable), A2 (findings carry / disposition evidence), A8 (two hashes
verified separately) and A9 (revision-bound consumer coverage), plus the loop
rule ``max_rounds`` 0 and the transition tables.  Each AC has a MUTANT comment
naming the one-line edit to the new code that must turn the test red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cli_agent_orchestrator.core import gate as g

#: The fixed "now" every slice-B1 question arm is written against.
_T0 = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _manifest(
    *, head: str = "h" * 12, entries: tuple[g.DiffEntry, ...] | None = None
) -> g.ArtifactManifest:
    return g.ArtifactManifest(
        manifest_version=1,
        repo_bindings=(g.RepoBinding(name="fork", commit="c" * 12),),
        base_sha="b" * 12,
        head_sha=head,
        branch="cao/x",
        worktree_path="/w",
        entries=entries or (g.DiffEntry(post_path="a.py", post_object_id="o1"),),
        blueprint_sha="bp" * 4,
        ac_list_sha="ac" * 4,
    )


def _round(**overrides: object) -> g.GateRound:
    base: dict[str, object] = dict(
        round_id="R1",
        run_id="RUN1",
        round_no=1,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
        state=g.RoundState.OPEN,
        generation=0,
        row_version=1,
        created_at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    base.update(overrides)
    return g.GateRound(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# AC-A1 — no typed shas: the address is DERIVED and deterministic.
# MUTANT: let the template accept a caller-supplied head_sha (here modelled as
# compute_artifact_sha ignoring the diff order or a field). A digest that does
# not fold a field is caught by these equalities.
# --------------------------------------------------------------------------


def test_ac_a1_artifact_sha_is_deterministic_and_content_bound() -> None:
    m = _manifest()
    assert g.compute_artifact_sha(m) == g.compute_artifact_sha(m)  # stable


def test_ac_a1_artifact_sha_changes_with_blob() -> None:
    m1 = _manifest(entries=(g.DiffEntry(post_path="a.py", post_object_id="o1"),))
    m2 = _manifest(entries=(g.DiffEntry(post_path="a.py", post_object_id="o2"),))
    assert g.compute_artifact_sha(m1) != g.compute_artifact_sha(m2)


def test_ac_a1_artifact_sha_is_order_sensitive() -> None:
    e1 = g.DiffEntry(post_path="a.py", post_object_id="o1")
    e2 = g.DiffEntry(post_path="b.py", post_object_id="o2")
    assert g.compute_artifact_sha(_manifest(entries=(e1, e2))) != g.compute_artifact_sha(
        _manifest(entries=(e2, e1))
    )


def test_ac_a1_artifact_sha_distinguishes_deletion_from_empty() -> None:
    deleted = g.DiffEntry(pre_path="a.py", pre_object_id="o1", deleted=True)
    emptied = g.DiffEntry(pre_path="a.py", post_path="a.py", pre_object_id="o1", post_object_id="e")
    assert g.compute_artifact_sha(_manifest(entries=(deleted,))) != g.compute_artifact_sha(
        _manifest(entries=(emptied,))
    )


def test_max_rounds_zero_is_refused() -> None:
    # MUTANT: make validate_max_rounds accept <= 0 (e.g. `return max_rounds`).
    with pytest.raises(g.GateError):
        g.validate_max_rounds(0)
    assert g.validate_max_rounds(1) == 1


# --------------------------------------------------------------------------
# AC-A3 — scratch paths are GENERATED from the host.
# MUTANT: hard-code one scratch root (return the laptop path for every host).
# --------------------------------------------------------------------------


def test_ac_a3_scratch_root_generated_from_host() -> None:
    assert (
        g.render_scratch_root(g.ExecutionTarget(host="grok-box-001"), "R1") == "~/box-scratch/R1/"
    )
    assert g.render_scratch_root(g.ExecutionTarget(host="laptop"), "R1") == "/data/cao-scratch/R1/"


def test_ac_a3_unknown_host_is_refused_not_defaulted() -> None:
    # The pre-runner put a laptop path on a box; an unknown host must not silently
    # get the laptop root.
    with pytest.raises(g.GateError):
        g.render_scratch_root(g.ExecutionTarget(host="mystery"), "R1")


# --------------------------------------------------------------------------
# AC-A2 — findings carry; disposition evidence is required.
# MUTANT: allow FIXED with no disposition evidence.
# --------------------------------------------------------------------------


def test_ac_a2_fixed_requires_killer_evidence() -> None:
    with pytest.raises(g.GateError):
        g.validate_disposition(
            g.Disposition(kind=g.DispositionKind.FIXED, at=datetime(2026, 9, 9, tzinfo=UTC))
        )


def test_ac_a2_fixed_requires_reviewed_artifact_sha() -> None:
    with pytest.raises(g.GateError):
        g.validate_disposition(
            g.Disposition(
                kind=g.DispositionKind.FIXED,
                killer_test="t.py::x",
                killer_mutant="m1",
                at=datetime(2026, 9, 9, tzinfo=UTC),
            )
        )


def test_ac_a2_withdrawn_requires_actor_and_reason() -> None:
    with pytest.raises(g.GateError):
        g.validate_disposition(
            g.Disposition(kind=g.DispositionKind.WITHDRAWN, at=datetime(2026, 9, 9, tzinfo=UTC))
        )


def test_ac_a2_valid_dispositions_pass() -> None:
    fixed = g.Disposition(
        kind=g.DispositionKind.FIXED,
        reviewed_artifact_sha="abc",
        killer_test="t.py::x",
        killer_mutant="m1",
        at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    withdrawn = g.Disposition(
        kind=g.DispositionKind.WITHDRAWN,
        actor="conv1",
        reason="not a real defect",
        at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert g.validate_disposition(fixed) is fixed
    assert g.validate_disposition(withdrawn) is withdrawn


def test_ac_a2_open_finding_is_open_until_disposed() -> None:
    finding = g.OpenFinding(
        finding_id="F1", raised_in_round="R1", severity=g.Severity.BLOCKER, statement="x"
    )
    assert finding.is_open
    disposed = finding.model_copy(
        update={
            "dispositions": (
                g.Disposition(
                    kind=g.DispositionKind.WITHDRAWN,
                    actor="a",
                    reason="r",
                    at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )
        }
    )
    assert not disposed.is_open


# --------------------------------------------------------------------------
# AC-A8 — two hashes verified SEPARATELY.
# MUTANT: verify one hash and infer the other.
# --------------------------------------------------------------------------


def test_ac_a8_accept_requires_both_hashes() -> None:
    snap = _manifest()
    subject = g.compute_artifact_sha(snap)
    rnd = _round(state=g.RoundState.YES, review_snapshot=snap)
    # both correct -> ok
    g.may_accept_round(
        rnd,
        declared_subject_sha=subject,
        declared_report_bytes_sha="RB",
        cas_report_bytes_sha="RB",
    )


def test_ac_a8_bad_subject_refused_even_with_good_bytes() -> None:
    snap = _manifest()
    rnd = _round(state=g.RoundState.YES, review_snapshot=snap)
    with pytest.raises(g.GateError):
        g.may_accept_round(
            rnd,
            declared_subject_sha="STALE",  # findings §B2 class
            declared_report_bytes_sha="RB",
            cas_report_bytes_sha="RB",
        )


def test_ac_a8_bad_bytes_refused_even_with_good_subject() -> None:
    snap = _manifest()
    subject = g.compute_artifact_sha(snap)
    rnd = _round(state=g.RoundState.YES, review_snapshot=snap)
    with pytest.raises(g.GateError):
        g.may_accept_round(
            rnd,
            declared_subject_sha=subject,
            declared_report_bytes_sha="RB",
            cas_report_bytes_sha="DIFFERENT",
        )


def test_ac_a8_accept_refused_before_yes() -> None:
    snap = _manifest()
    rnd = _round(state=g.RoundState.BUILT, review_snapshot=snap)
    with pytest.raises(g.GateError):
        g.may_accept_round(
            rnd,
            declared_subject_sha=g.compute_artifact_sha(snap),
            declared_report_bytes_sha="RB",
            cas_report_bytes_sha="RB",
        )


# --------------------------------------------------------------------------
# AC-A9 — consumer coverage is revision-bound (a value type, enforced by the
# model's required xref_sha + reviewed_artifact_sha).
# MUTANT: accept a completeness claim from AST alone (coverage with no revision
# binding) — the required fields make that unconstructable.
# --------------------------------------------------------------------------


def test_ac_a9_consumer_coverage_requires_revision_binding() -> None:
    with pytest.raises(Exception):
        g.ConsumerCoverage(xref_sha="", reviewed_artifact_sha="")  # type: ignore[call-arg]
    cov = g.ConsumerCoverage(
        xref_sha="x1",
        reviewed_artifact_sha="rev1",
        consumers=(
            g.ConsumerDisposition(symbol="f", site="p.py:1", state=g.ConsumerState.UPDATED),
        ),
        unresolved_dynamic=("getattr(obj, name)",),
    )
    assert cov.xref_sha and cov.reviewed_artifact_sha


# --------------------------------------------------------------------------
# Transition tables and the AWAITING_ANSWER projection.
# --------------------------------------------------------------------------


def test_round_transition_table() -> None:
    assert g.next_round_state(g.RoundState.OPEN, g.RoundState.BUILT) is g.RoundState.BUILT
    assert (
        g.next_round_state(g.RoundState.BUILT, g.RoundState.ADJUDICATING)
        is g.RoundState.ADJUDICATING
    )
    assert g.next_round_state(g.RoundState.ADJUDICATING, g.RoundState.YES) is g.RoundState.YES
    with pytest.raises(g.GateError):
        g.next_round_state(g.RoundState.OPEN, g.RoundState.YES)  # skip the build+adjudicate
    with pytest.raises(g.GateError):
        g.next_round_state(g.RoundState.YES, g.RoundState.NO)  # terminal


def test_run_awaiting_answer_is_not_a_writable_state() -> None:
    with pytest.raises(g.GateError):
        g.next_run_state(g.RunState.OPEN, g.RunState.AWAITING_ANSWER)


def test_run_awaiting_answer_projection() -> None:
    assert not g.run_awaiting_answer([g.DispatchState.DISPATCHED, g.DispatchState.RETURNED])
    assert g.run_awaiting_answer([g.DispatchState.DISPATCHED, g.DispatchState.AWAITING_ANSWER])


def test_epoch_supersedes_is_strict() -> None:
    # MUTANT: accept a non-increasing epoch (>= instead of >).
    assert g.epoch_supersedes(3, 4)
    assert not g.epoch_supersedes(4, 4)
    assert not g.epoch_supersedes(5, 4)


# -- slice B1: the question primitive's pure rules ---------------------------


def _question(**overrides: object) -> g.RoundQuestion:
    """A PENDING question an hour from expiry, with fields overridable by name."""
    base: dict[str, object] = {
        "question_id": "Q1",
        "dispatch_id": "d1",
        "client_request_id": "cr1",
        "owner_conversation": "c1",
        "owner_epoch": 3,
        "continuation_kind": g.ContinuationKind.ASSIGNMENT,
        "continuation_ref": "d1",
        "asked_at": _T0,
        "expires_at": _T0 + timedelta(hours=1),
        "question": "accept, re-round or override?",
        "row_version": 1,
    }
    base.update(overrides)
    return g.RoundQuestion(**base)  # type: ignore[arg-type]


def test_question_transition_table_is_enumerated() -> None:
    """Every (from, to) pair, decided here rather than discovered in the store.

    Enumerated rather than spot-checked because the table is small and the
    dangerous cells are the ones nobody thinks to write a test for: a settled
    question re-settling would let an expiry overwrite an answer the asker has
    already consumed.
    """
    S = g.QuestionState
    legal = {
        (S.PENDING, S.ESCALATED),
        (S.PENDING, S.ANSWERED),
        (S.PENDING, S.EXPIRED),
        (S.ESCALATED, S.ANSWERED),
        (S.ESCALATED, S.EXPIRED),
    }
    for current in S:
        for target in S:
            if (current, target) in legal:
                assert g.next_question_state(current, target) is target
            else:
                with pytest.raises(g.GateQuestionError) as exc:
                    g.next_question_state(current, target)
                assert exc.value.code is g.QuestionRefusal.ILLEGAL_TRANSITION


def test_escalated_is_not_reachable_from_escalated_or_settled() -> None:
    """Escalation is one-way into the open slot, never a way back out of it."""
    for origin in (g.QuestionState.ESCALATED, g.QuestionState.ANSWERED, g.QuestionState.EXPIRED):
        with pytest.raises(g.GateQuestionError):
            g.next_question_state(origin, g.QuestionState.ESCALATED)


def test_validate_ask_requires_a_default_only_when_non_blocking() -> None:
    """The rule that matters: a caller who does not wait must say what it does instead."""
    window = {"asked_at": _T0, "expires_at": _T0 + timedelta(hours=1)}
    g.validate_ask(blocking=True, default_answer=None, question="go?", **window)
    g.validate_ask(blocking=False, default_answer="no", question="go?", **window)
    with pytest.raises(g.GateQuestionError) as exc:
        g.validate_ask(blocking=False, default_answer=None, question="go?", **window)
    assert exc.value.code is g.QuestionRefusal.DEFAULT_REQUIRED
    with pytest.raises(g.GateQuestionError) as empty_default:
        g.validate_ask(blocking=False, default_answer="", question="go?", **window)
    assert empty_default.value.code is g.QuestionRefusal.DEFAULT_REQUIRED


def test_validate_ask_refuses_a_question_born_expired() -> None:
    """An ``expires_at`` at or before ``asked_at`` would settle before anyone saw it."""
    for expires in (_T0, _T0 - timedelta(seconds=1)):
        with pytest.raises(g.GateQuestionError) as exc:
            g.validate_ask(
                blocking=True,
                default_answer=None,
                question="go?",
                asked_at=_T0,
                expires_at=expires,
            )
        assert exc.value.code is g.QuestionRefusal.QUESTION_EXPIRED


def test_validate_ask_refuses_empty_question_text() -> None:
    with pytest.raises(g.GateQuestionError) as exc:
        g.validate_ask(
            blocking=True,
            default_answer=None,
            question="   ",
            asked_at=_T0,
            expires_at=_T0 + timedelta(hours=1),
        )
    assert exc.value.code is g.QuestionRefusal.QUESTION_EMPTY


def test_answer_admissible_accepts_the_current_owner() -> None:
    verdict = g.answer_admissible(
        _question(),
        caller_conversation="c1",
        caller_epoch=3,
        now=_T0 + timedelta(minutes=1),
    )
    assert verdict.admissible and verdict.code is None


def test_answer_admissible_refuses_a_superseded_epoch_and_reuses_the_pure_rule() -> None:
    """AC-A12: an answer from a conversation a later claim overtook is refused.

    The caller's epoch is BEHIND the row's, which is what ``claim_ownership``
    leaves behind after it rewrites an open question.  The check is
    :func:`epoch_supersedes` itself, so a second spelling of "ownership is
    monotonic" cannot come to disagree with the store's.
    """
    verdict = g.answer_admissible(
        _question(owner_epoch=5),
        caller_conversation="c1",
        caller_epoch=4,
        now=_T0 + timedelta(minutes=1),
    )
    assert not verdict.admissible
    assert verdict.code is g.QuestionRefusal.EPOCH_SUPERSEDED
    assert g.epoch_supersedes(4, 5) is True


def test_answer_admissible_admits_an_epoch_ahead_of_the_row() -> None:
    """A claim that has not yet rewritten THIS row is not a stale answer."""
    verdict = g.answer_admissible(
        _question(owner_epoch=3),
        caller_conversation="c1",
        caller_epoch=9,
        now=_T0 + timedelta(minutes=1),
    )
    assert verdict.admissible


def test_answer_admissible_refuses_a_different_conversation() -> None:
    verdict = g.answer_admissible(
        _question(),
        caller_conversation="c2",
        caller_epoch=3,
        now=_T0 + timedelta(minutes=1),
    )
    assert verdict.code is g.QuestionRefusal.OWNER_MISMATCH


def test_answer_admissible_refuses_past_the_deadline_before_any_sweep_runs() -> None:
    """Wall-clock expiry is checked here, not only by the sweep's cadence.

    A row still reading PENDING past its deadline is refused at the instant an
    answer arrives, so an answer and an expiry cannot both be admitted through
    the window a 30-second sweep period would otherwise open.
    """
    verdict = g.answer_admissible(
        _question(),
        caller_conversation="c1",
        caller_epoch=3,
        now=_T0 + timedelta(hours=1),
    )
    assert verdict.code is g.QuestionRefusal.QUESTION_EXPIRED


def test_answer_admissible_refuses_a_settled_question() -> None:
    for state in (g.QuestionState.ANSWERED, g.QuestionState.EXPIRED):
        verdict = g.answer_admissible(
            _question(state=state),
            caller_conversation="c1",
            caller_epoch=3,
            now=_T0 + timedelta(minutes=1),
        )
        assert verdict.code is g.QuestionRefusal.QUESTION_SETTLED


def test_a_question_is_open_in_exactly_the_two_slot_holding_states() -> None:
    """AC-A10's slot: PENDING and ESCALATED, and nothing else."""
    assert _question(state=g.QuestionState.PENDING).is_open
    assert _question(state=g.QuestionState.ESCALATED).is_open
    assert not _question(state=g.QuestionState.ANSWERED).is_open
    assert not _question(state=g.QuestionState.EXPIRED).is_open


def test_awaiting_answer_stays_a_projection_with_questions_in_play() -> None:
    """No ``TerminalStatus`` member: the wait is the dispatch state, projected.

    Re-asserted in the question arms because B1 is where somebody would be
    tempted to add one — the question now exists, so "what is the lane doing"
    has an answer that must keep coming from the rows.
    """
    assert g.run_awaiting_answer([g.DispatchState.AWAITING_ANSWER]) is True
    assert g.run_awaiting_answer([g.DispatchState.DISPATCHED]) is False
    with pytest.raises(g.GateError):
        g.next_run_state(g.RunState.OPEN, g.RunState.AWAITING_ANSWER)
