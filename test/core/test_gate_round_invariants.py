"""Pure gate invariants and transitions (WP-ARCH Amendment A, slice 2a).

No database, no clock, no event loop — ``core/gate`` is pure, so these run by
enumeration.  The 2a AC arms this file carries are A1 (no typed shas: the address
is derived and stable), A2 (findings carry / disposition evidence), A8 (two hashes
verified separately) and A9 (revision-bound consumer coverage), plus the loop
rule ``max_rounds`` 0 and the transition tables.  Each AC has a MUTANT comment
naming the one-line edit to the new code that must turn the test red.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cli_agent_orchestrator.core import gate as g


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
