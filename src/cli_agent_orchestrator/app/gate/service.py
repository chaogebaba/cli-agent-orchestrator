"""``GateRoundService`` — the transactional gate-round commands (WP-ARCH A, 2a).

The commands the CLI, the MCP surface and (in 2c) the workflow shim all call.
The service holds the ORDER and the pure-rule checks; the store holds the SQL.
Everything it touches is a ``core.ports``/``app.gate.ports`` Protocol, so nothing
here names ``adapters`` or ``services`` — the ``adapters-only-via-composition-root``
and ``new-code-never-imports-legacy`` contracts stay green.

The service is the exclusive WRITER path for the gate rows.  §10.3's DoD is that
the gate commands are the only writer of ``adapters.store.gate``; this class is
the one place those commands live, so a second writer would have to be a second
call site of the store Protocol, which the ``one-gate-writer`` contract forbids at
the import boundary and this module forbids by being the only owner of the
reference.

What is enforced HERE rather than in the store: the pure invariants of
``core/gate``.  ``open_run`` rejects ``max_rounds`` 0 before a row is minted;
``freeze_review_snapshot`` refuses a re-freeze; ``close_finding`` validates the
disposition's evidence before it reaches the row; ``accept_round`` runs the
two-hash check against the CAS bytes-sha the caller read back.  The store re-checks
what it must (row versions, epoch monotonicity) because it owns the transaction,
but the domain rules are decided in ``core`` and applied here so they are testable
without a database.
"""

from __future__ import annotations

from datetime import datetime

from cli_agent_orchestrator.app.gate.ports import (
    ClaimOwnershipResult,
    GateStore,
    RoundProjection,
)
from cli_agent_orchestrator.core.gate import (
    ArtifactManifest,
    ConsumerCoverage,
    Dispatch,
    DispatchState,
    Disposition,
    EffectIntent,
    EffectResult,
    ExecutionTarget,
    GateError,
    GateRound,
    GateRun,
    OpenFinding,
    RoundState,
    RunState,
    Severity,
    may_accept_round,
    next_round_state,
    next_run_state,
    run_awaiting_answer,
    validate_disposition,
    validate_max_rounds,
)
from cli_agent_orchestrator.core.ports import Clock

__all__ = ["GateRoundService"]


class GateRoundService:
    """Transactional commands over a :class:`GateStore` (P3)."""

    def __init__(self, store: GateStore, *, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- run and round lifecycle -------------------------------------------

    def open_run(
        self,
        *,
        wp: str,
        lane: str,
        owner_conversation: str,
        owner_epoch: int,
        max_rounds: int,
        workflow_source_sha: str = "",
        input_sha: str = "",
    ) -> GateRun:
        """Open a run.  ``max_rounds`` 0 is refused BEFORE a row is minted.

        The pure rule is checked here so a caller learns ``max_rounds`` was
        rejected without a database write having happened; the store also rejects
        it (the ``GateRun`` model's ``gt=0`` field), which is the second line the
        blueprint asks for — validate-here, enforce-there.
        """
        validate_max_rounds(max_rounds)
        return self._store.open_run(
            wp=wp,
            lane=lane,
            owner_conversation=owner_conversation,
            owner_epoch=owner_epoch,
            max_rounds=max_rounds,
            workflow_source_sha=workflow_source_sha,
            input_sha=input_sha,
        )

    def open_round(
        self,
        *,
        run_id: str,
        build_inputs: ArtifactManifest,
        execution_target: ExecutionTarget,
        predecessor_round_id: str | None = None,
        test_command: str = "",
        evidence_tier: str = "",
        generation: int = 0,
    ) -> GateRound:
        """Open a round with its build inputs frozen before the builder runs.

        Exhaustion is refused here: a run at ``max_rounds`` does not open an unused
        successor (§10.3).  The count is the number of non-abandoned rounds already
        under the run.
        """
        run = self._store.get_run(run_id)
        if run is None:
            raise GateError(f"no such run: {run_id}")
        if run.state is not RunState.OPEN:
            raise GateError(f"cannot open a round on a {run.state.value} run")
        existing = [
            r for r in self._store.rounds_for_run(run_id) if r.state is not RoundState.ABANDONED
        ]
        if len(existing) >= run.max_rounds:
            raise GateError(
                f"run {run_id} has reached max_rounds={run.max_rounds}; "
                "exhaustion stops without opening an unused successor"
            )
        return self._store.open_round(
            run_id=run_id,
            build_inputs=build_inputs,
            execution_target=execution_target,
            predecessor_round_id=predecessor_round_id,
            test_command=test_command,
            evidence_tier=evidence_tier,
            generation=generation,
        )

    def freeze_review_snapshot(self, round_id: str, snapshot: ArtifactManifest) -> GateRound:
        """Freeze the review snapshot (OPEN -> BUILT), refusing a re-freeze (P1)."""
        round_ = self._require_round(round_id)
        if round_.review_snapshot is not None:
            raise GateError(
                "the review snapshot is already frozen; a change to the reviewed "
                "bytes opens a successor round, it never re-freezes this one"
            )
        # The legal transition is checked purely before the store's own version
        # guard runs, so an illegal freeze is a GateError, not a row-version error.
        next_round_state(round_.state, RoundState.BUILT)
        return self._store.freeze_review_snapshot(
            round_id, snapshot, expected_row_version=round_.row_version
        )

    def begin_adjudication(self, round_id: str) -> GateRound:
        """Move a BUILT round to ADJUDICATING (the HIGH-tier review runs)."""
        round_ = self._require_round(round_id)
        next_round_state(round_.state, RoundState.ADJUDICATING)
        return self._store.transition_round(
            round_id,
            RoundState.ADJUDICATING,
            expected_row_version=round_.row_version,
            now=self._clock.now(),
        )

    def settle_adjudication(
        self,
        round_id: str,
        *,
        verdict_yes: bool,
        subject_sha: str,
        report_bytes_sha: str,
        verdict_report_sha: str,
    ) -> GateRound:
        """Record the verdict hashes and move ADJUDICATING -> YES|NO.

        The two verification hashes are stored on the round in the SAME command
        that settles it, so ``accept_round`` later has both to check.  Storing
        them is not accepting: acceptance is a separate, persisted human decision.
        """
        round_ = self._require_round(round_id)
        target = RoundState.YES if verdict_yes else RoundState.NO
        next_round_state(round_.state, target)
        stamped = self._store.set_round_report(
            round_id,
            subject_sha=subject_sha,
            report_bytes_sha=report_bytes_sha,
            verdict_report_sha=verdict_report_sha,
            expected_row_version=round_.row_version,
        )
        return self._store.transition_round(
            round_id,
            target,
            expected_row_version=stamped.row_version,
            now=self._clock.now(),
        )

    def accept_round(
        self,
        round_id: str,
        *,
        declared_subject_sha: str,
        declared_report_bytes_sha: str,
        cas_report_bytes_sha: str,
    ) -> None:
        """Verify a YES round may be accepted, checking BOTH hashes separately (AC-A8).

        A YES verdict PERMITS this decision and does not itself authorize a merge
        (§10.3): merge/push/redeploy carry their own effect intents.  This command
        is the persisted human accept decision's gate — it raises if either the
        subject hash (against the frozen snapshot) or the report bytes hash
        (against the CAS) does not verify.
        """
        round_ = self._require_round(round_id)
        may_accept_round(
            round_,
            declared_subject_sha=declared_subject_sha,
            declared_report_bytes_sha=declared_report_bytes_sha,
            cas_report_bytes_sha=cas_report_bytes_sha,
        )

    def abandon_run(self, run_id: str) -> GateRun:
        """Tear a run down (OPEN|AWAITING_ANSWER -> ABANDONED)."""
        run = self._store.get_run(run_id)
        if run is None:
            raise GateError(f"no such run: {run_id}")
        next_run_state(run.state, RunState.ABANDONED)
        return self._store.transition_run(
            run_id, RunState.ABANDONED, expected_row_version=run.row_version
        )

    def close_run(self, run_id: str, *, verdict_yes: bool) -> GateRun:
        """Close a run YES or NO (OPEN -> CLOSED_YES|CLOSED_NO)."""
        run = self._store.get_run(run_id)
        if run is None:
            raise GateError(f"no such run: {run_id}")
        target = RunState.CLOSED_YES if verdict_yes else RunState.CLOSED_NO
        next_run_state(run.state, target)
        return self._store.transition_run(run_id, target, expected_row_version=run.row_version)

    # -- dispatches --------------------------------------------------------

    def record_dispatch(self, dispatch: Dispatch) -> Dispatch:
        """Record one assignment aggregate (nullable round id for a non-gate lane)."""
        return self._store.record_dispatch(dispatch)

    # -- effects (intent BEFORE the operation) -----------------------------

    def record_effect_intent(self, intent: EffectIntent) -> EffectIntent:
        """Record an effect intent BEFORE its external operation (P1)."""
        return self._store.record_effect_intent(intent)

    def record_effect_result(self, result: EffectResult) -> EffectResult:
        """Record the settled outcome of a recorded intent."""
        return self._store.record_effect_result(result)

    # -- findings ----------------------------------------------------------

    def raise_finding(
        self, *, raised_in_round: str, severity: Severity, statement: str
    ) -> OpenFinding:
        """Raise a finding that will carry into the next brief until closed (AC-A2)."""
        return self._store.raise_finding(
            raised_in_round=raised_in_round, severity=severity, statement=statement
        )

    def close_finding(self, finding_id: str, disposition: Disposition) -> OpenFinding:
        """Close a finding, VALIDATING its evidence first (P7, AC-A2).

        A FIXED with no killer test/mutant, or a WITHDRAWN with no actor/reason, is
        refused before the row is touched — the mutant AC-A2 kills is a store that
        accepts a FIXED with no disposition evidence.
        """
        validate_disposition(disposition)
        return self._store.append_disposition(finding_id, disposition)

    def set_consumer_coverage(self, round_id: str, coverage: ConsumerCoverage) -> None:
        """Bind revision-bound consumer coverage to a round (AC-A9)."""
        self._store.set_consumer_coverage(round_id, coverage)

    # -- ownership (P2, AC-A12; rows-and-transaction in 2a) ----------------

    def claim_ownership(
        self,
        *,
        prior_conversation: str,
        new_conversation: str,
        new_epoch: int,
        client_request_id: str,
        claimed_by: str,
        run_id: str | None = None,
    ) -> ClaimOwnershipResult:
        """Transfer ownership monotonically in the epoch (P2, DESIGN r2 C1)."""
        return self._store.claim_ownership(
            prior_conversation=prior_conversation,
            new_conversation=new_conversation,
            new_epoch=new_epoch,
            run_id=run_id,
            client_request_id=client_request_id,
            claimed_by=claimed_by,
        )

    # -- reads -------------------------------------------------------------

    def project_round(self, round_id: str) -> RoundProjection | None:
        """Re-project a whole round from rows, zero markdown reads (AC-A1 restart)."""
        return self._store.project_round(round_id)

    def run_effective_state(self, run_id: str) -> RunState:
        """The run's effective state, with AWAITING_ANSWER computed as a projection.

        ``AWAITING_ANSWER`` is never stored on the run row (P1): it is True exactly
        when a dispatch under the run is awaiting an answer.  A stored run in OPEN
        with a suspended dispatch therefore reads as AWAITING_ANSWER here, and can
        never be stale relative to its cause.
        """
        run = self._store.get_run(run_id)
        if run is None:
            raise GateError(f"no such run: {run_id}")
        if run.state is not RunState.OPEN:
            return run.state
        dispatch_states: list[DispatchState] = []
        for round_ in self._store.rounds_for_run(run_id):
            projection = self._store.project_round(round_.round_id)
            if projection is not None:
                dispatch_states.extend(d.state for d in projection.dispatches)
        return RunState.AWAITING_ANSWER if run_awaiting_answer(dispatch_states) else RunState.OPEN

    def _require_round(self, round_id: str) -> GateRound:
        round_ = self._store.get_round(round_id)
        if round_ is None:
            raise GateError(f"no such round: {round_id}")
        return round_
