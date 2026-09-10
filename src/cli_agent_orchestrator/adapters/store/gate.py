"""The gate-record store adapter (WP-ARCH Amendment A, slice 2a; §10.2).

The one module that knows the gate rows are SQLite.  It satisfies
``app.gate.ports.GateStore`` structurally; ``bootstrap.py`` builds it and hands
it to ``app`` as that Protocol, so ``app.gate`` never names this file — the
``one-gate-writer`` contract (§10.3 DoD) forbids everyone but the composition
root from importing ``adapters.store.gate``.

Each mutator is ONE ``BEGIN IMMEDIATE`` transaction (the write lock taken up
front, phase 1's ``connection.py`` policy), so two writers serialise rather than
discover a conflict half-way.  Optimistic concurrency is a version match inside
that transaction: a stale ``expected_row_version`` changes zero rows, which the
adapter reads as a typed refusal (``GateError``), never as "not found".  The
``ArtifactManifest`` values and the JSON array columns are (de)serialised through
pydantic so the round trip is total and a malformed row is a loud parse error,
not a silent partial read.

``claim_ownership`` is the one multi-table transaction: it verifies epoch
monotonicity, rewrites every ``gate_run`` the outgoing conversation owns and
every PENDING/ESCALATED ``round_question`` whose ``owner_conversation`` matches
(``run_id`` narrows to one run), and appends one ``ownership_transfer`` row — all
under a single ``BEGIN IMMEDIATE``.  Identical retries by ``client_request_id``
return the recorded transfer rather than re-applying it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from cli_agent_orchestrator.adapters.store.connection import (
    SqliteConnectionSource,
    immediate_transaction,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.gate import (
    ArtifactManifest,
    ClaimOwnershipResult,
    ConsumerCoverage,
    ConsumerDisposition,
    Dispatch,
    DispatchRole,
    DispatchState,
    Disposition,
    DispositionKind,
    EffectIntent,
    EffectKind,
    EffectOutcome,
    EffectResult,
    ExecutionTarget,
    GateError,
    GateRound,
    GateRun,
    OpenFinding,
    RoundProjection,
    RoundState,
    RunState,
    Severity,
    epoch_supersedes,
    next_round_state,
    next_run_state,
    validate_max_rounds,
)
from cli_agent_orchestrator.core.ids import new_ulid
from cli_agent_orchestrator.core.ports import Clock

__all__ = ["SqliteGateStore"]


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SqliteGateStore:
    """``app.gate.ports.GateStore`` over SQLite."""

    def __init__(self, pool: SqliteConnectionSource, *, clock: Clock | None = None) -> None:
        self._pool = pool
        self._clock = clock if clock is not None else _SystemClock()

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
        validate_max_rounds(max_rounds)
        run = GateRun(
            run_id=new_ulid(),
            wp=wp,
            lane=lane,
            workflow_source_sha=workflow_source_sha,
            input_sha=input_sha,
            owner_conversation=owner_conversation,
            owner_epoch=owner_epoch,
            max_rounds=max_rounds,
            row_version=1,
            state=RunState.OPEN,
        )
        now = render_timestamp(self._clock.now())
        conn = self._pool.connection()
        with immediate_transaction(conn):
            conn.execute(
                "INSERT INTO gate_run (run_id, wp, lane, workflow_source_sha, input_sha, "
                "owner_conversation, owner_epoch, max_rounds, row_version, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.wp,
                    run.lane,
                    run.workflow_source_sha,
                    run.input_sha,
                    run.owner_conversation,
                    run.owner_epoch,
                    run.max_rounds,
                    run.row_version,
                    run.state.value,
                    now,
                ),
            )
        return run

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
        conn = self._pool.connection()
        with immediate_transaction(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(round_no), 0) AS m FROM gate_round WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            round_no = int(row["m"]) + 1
            round_ = GateRound(
                round_id=new_ulid(),
                run_id=run_id,
                round_no=round_no,
                predecessor_round_id=predecessor_round_id,
                build_inputs=build_inputs,
                review_snapshot=None,
                execution_target=execution_target,
                test_command=test_command,
                evidence_tier=evidence_tier,
                state=RoundState.OPEN,
                generation=generation,
                row_version=1,
                created_at=self._clock.now(),
            )
            conn.execute(
                "INSERT INTO gate_round (round_id, run_id, round_no, predecessor_round_id, "
                "build_inputs, review_snapshot, execution_target, test_command, evidence_tier, "
                "fixture_corpus_sha, fixture_frame_count, subject_sha, report_bytes_sha, "
                "verdict_report_sha, state, generation, row_version, created_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    round_.round_id,
                    round_.run_id,
                    round_.round_no,
                    round_.predecessor_round_id,
                    build_inputs.model_dump_json(),
                    None,
                    execution_target.model_dump_json(),
                    round_.test_command,
                    round_.evidence_tier,
                    None,
                    None,
                    None,
                    None,
                    None,
                    round_.state.value,
                    round_.generation,
                    round_.row_version,
                    render_timestamp(round_.created_at),
                    None,
                ),
            )
        return round_

    def freeze_review_snapshot(
        self, round_id: str, snapshot: ArtifactManifest, *, expected_row_version: int
    ) -> GateRound:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            current = self._get_round_locked(conn, round_id)
            if current.review_snapshot is not None:
                raise GateError("the review snapshot is already frozen")
            next_round_state(current.state, RoundState.BUILT)
            changed = conn.execute(
                "UPDATE gate_round SET review_snapshot = ?, state = ?, "
                "row_version = row_version + 1 WHERE round_id = ? AND row_version = ?",
                (
                    snapshot.model_dump_json(),
                    RoundState.BUILT.value,
                    round_id,
                    expected_row_version,
                ),
            )
            if changed.rowcount == 0:
                raise GateError(
                    f"row-version conflict freezing round {round_id}: "
                    f"expected {expected_row_version}"
                )
            return self._get_round_locked(conn, round_id)

    def transition_round(
        self, round_id: str, target: RoundState, *, expected_row_version: int, now: datetime
    ) -> GateRound:
        closed = target in (RoundState.YES, RoundState.NO, RoundState.ABANDONED)
        conn = self._pool.connection()
        with immediate_transaction(conn):
            current = self._get_round_locked(conn, round_id)
            next_round_state(current.state, target)
            changed = conn.execute(
                "UPDATE gate_round SET state = ?, row_version = row_version + 1, "
                "closed_at = ? WHERE round_id = ? AND row_version = ?",
                (
                    target.value,
                    render_timestamp(now) if closed else None,
                    round_id,
                    expected_row_version,
                ),
            )
            if changed.rowcount == 0:
                raise GateError(
                    f"row-version conflict transitioning round {round_id}: "
                    f"expected {expected_row_version}"
                )
            return self._get_round_locked(conn, round_id)

    def transition_run(
        self, run_id: str, target: RunState, *, expected_row_version: int
    ) -> GateRun:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            current = self._get_run_locked(conn, run_id)
            next_run_state(current.state, target)
            changed = conn.execute(
                "UPDATE gate_run SET state = ?, row_version = row_version + 1 "
                "WHERE run_id = ? AND row_version = ?",
                (target.value, run_id, expected_row_version),
            )
            if changed.rowcount == 0:
                raise GateError(
                    f"row-version conflict transitioning run {run_id}: "
                    f"expected {expected_row_version}"
                )
            return self._get_run_locked(conn, run_id)

    def set_round_report(
        self,
        round_id: str,
        *,
        subject_sha: str,
        report_bytes_sha: str,
        verdict_report_sha: str,
        expected_row_version: int,
    ) -> GateRound:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            self._get_round_locked(conn, round_id)  # existence
            changed = conn.execute(
                "UPDATE gate_round SET subject_sha = ?, report_bytes_sha = ?, "
                "verdict_report_sha = ?, row_version = row_version + 1 "
                "WHERE round_id = ? AND row_version = ?",
                (
                    subject_sha,
                    report_bytes_sha,
                    verdict_report_sha,
                    round_id,
                    expected_row_version,
                ),
            )
            if changed.rowcount == 0:
                raise GateError(
                    f"row-version conflict recording report on round {round_id}: "
                    f"expected {expected_row_version}"
                )
            return self._get_round_locked(conn, round_id)

    # -- dispatches --------------------------------------------------------

    def record_dispatch(self, dispatch: Dispatch) -> Dispatch:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            conn.execute(
                "INSERT INTO gate_dispatch (dispatch_id, round_id, role, position, "
                "routing_revision, conversation_id, terminal_incarnation, request_id, "
                "effect_id, pins, brief_blob_sha, state, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(dispatch_id) DO UPDATE SET "
                "round_id=excluded.round_id, role=excluded.role, position=excluded.position, "
                "routing_revision=excluded.routing_revision, "
                "conversation_id=excluded.conversation_id, "
                "terminal_incarnation=excluded.terminal_incarnation, "
                "request_id=excluded.request_id, effect_id=excluded.effect_id, "
                "pins=excluded.pins, brief_blob_sha=excluded.brief_blob_sha, "
                "state=excluded.state, outcome=excluded.outcome",
                (
                    dispatch.dispatch_id,
                    dispatch.round_id,
                    dispatch.role.value,
                    dispatch.position,
                    dispatch.routing_revision,
                    dispatch.conversation_id,
                    dispatch.terminal_incarnation,
                    dispatch.request_id,
                    dispatch.effect_id,
                    json.dumps(list(dispatch.pins)),
                    dispatch.brief_blob_sha,
                    dispatch.state.value,
                    dispatch.outcome,
                ),
            )
        return dispatch

    # -- effects -----------------------------------------------------------

    def record_effect_intent(self, intent: EffectIntent) -> EffectIntent:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            # Idempotent on effect_id (the dedup key, P1): a repeat is a no-op that
            # returns the recorded intent, never a second row or an error.
            conn.execute(
                "INSERT INTO gate_effect_intent (effect_id, kind, round_id, dispatch_id, "
                "approval_ref, requested_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(effect_id) DO NOTHING",
                (
                    intent.effect_id,
                    intent.kind.value,
                    intent.round_id,
                    intent.dispatch_id,
                    intent.approval_ref,
                    render_timestamp(intent.requested_at),
                ),
            )
            row = conn.execute(
                "SELECT effect_id, kind, round_id, dispatch_id, approval_ref, requested_at "
                "FROM gate_effect_intent WHERE effect_id = ?",
                (intent.effect_id,),
            ).fetchone()
        return _row_to_intent(row)

    def record_effect_result(self, result: EffectResult) -> EffectResult:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            exists = conn.execute(
                "SELECT 1 FROM gate_effect_intent WHERE effect_id = ?",
                (result.effect_id,),
            ).fetchone()
            if exists is None:
                raise GateError(
                    f"no effect intent for {result.effect_id}; the intent is recorded "
                    "before the operation, so a result without one is a lost intent"
                )
            conn.execute(
                "INSERT INTO gate_effect_result (effect_id, outcome, evidence_ref, settled_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(effect_id) DO UPDATE SET "
                "outcome=excluded.outcome, evidence_ref=excluded.evidence_ref, "
                "settled_at=excluded.settled_at",
                (
                    result.effect_id,
                    result.outcome.value,
                    result.evidence_ref,
                    render_timestamp(result.settled_at) if result.settled_at else None,
                ),
            )
        return result

    # -- findings ----------------------------------------------------------

    def raise_finding(
        self, *, raised_in_round: str, severity: Severity, statement: str
    ) -> OpenFinding:
        conn = self._pool.connection()
        finding_id = new_ulid()
        with immediate_transaction(conn):
            run_row = conn.execute(
                "SELECT run_id FROM gate_round WHERE round_id = ?", (raised_in_round,)
            ).fetchone()
            if run_row is None:
                raise GateError(f"no such round: {raised_in_round}")
            conn.execute(
                "INSERT INTO gate_open_finding (finding_id, raised_in_round, run_id, "
                "severity, statement, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    finding_id,
                    raised_in_round,
                    run_row["run_id"],
                    severity.value,
                    statement,
                    render_timestamp(self._clock.now()),
                ),
            )
        return OpenFinding(
            finding_id=finding_id,
            raised_in_round=raised_in_round,
            severity=severity,
            statement=statement,
            dispositions=(),
        )

    def append_disposition(self, finding_id: str, disposition: Disposition) -> OpenFinding:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            base = conn.execute(
                "SELECT finding_id, raised_in_round, severity, statement "
                "FROM gate_open_finding WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            if base is None:
                raise GateError(f"no such finding: {finding_id}")
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM gate_disposition WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO gate_disposition (finding_id, seq, kind, reviewed_artifact_sha, "
                "killer_test, killer_mutant, actor, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finding_id,
                    int(seq_row["m"]) + 1,
                    disposition.kind.value,
                    disposition.reviewed_artifact_sha,
                    disposition.killer_test,
                    disposition.killer_mutant,
                    disposition.actor,
                    disposition.reason,
                    render_timestamp(disposition.at),
                ),
            )
            return self._load_finding_locked(conn, finding_id)

    def set_consumer_coverage(self, round_id: str, coverage: ConsumerCoverage) -> None:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            exists = conn.execute(
                "SELECT 1 FROM gate_round WHERE round_id = ?", (round_id,)
            ).fetchone()
            if exists is None:
                raise GateError(f"no such round: {round_id}")
            conn.execute(
                "INSERT INTO gate_consumer_coverage (round_id, xref_sha, "
                "reviewed_artifact_sha, consumers, unresolved_dynamic) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(round_id) DO UPDATE SET "
                "xref_sha=excluded.xref_sha, "
                "reviewed_artifact_sha=excluded.reviewed_artifact_sha, "
                "consumers=excluded.consumers, "
                "unresolved_dynamic=excluded.unresolved_dynamic",
                (
                    round_id,
                    coverage.xref_sha,
                    coverage.reviewed_artifact_sha,
                    json.dumps([c.model_dump(mode="json") for c in coverage.consumers]),
                    json.dumps(list(coverage.unresolved_dynamic)),
                ),
            )

    # -- ownership (P2, AC-A12) --------------------------------------------

    def claim_ownership(
        self,
        *,
        prior_conversation: str,
        new_conversation: str,
        new_epoch: int,
        run_id: str | None = None,
        client_request_id: str,
        claimed_by: str,
    ) -> ClaimOwnershipResult:
        conn = self._pool.connection()
        with immediate_transaction(conn):
            # Idempotent: an identical prior claim by request id returns its row.
            prior = conn.execute(
                "SELECT transfer_id, runs_rewritten, questions_rewritten "
                "FROM ownership_transfer WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if prior is not None:
                return ClaimOwnershipResult(
                    accepted=True,
                    runs_rewritten=int(prior["runs_rewritten"]),
                    questions_rewritten=int(prior["questions_rewritten"]),
                    transfer_id=prior["transfer_id"],
                    reason="idempotent replay",
                )

            highest = conn.execute(
                "SELECT MAX(owner_epoch) AS m FROM gate_run WHERE owner_conversation = ?",
                (prior_conversation,),
            ).fetchone()
            prior_epoch = int(highest["m"]) if highest["m"] is not None else -1
            if prior_epoch >= 0 and not epoch_supersedes(prior_epoch, new_epoch):
                return ClaimOwnershipResult(
                    accepted=False,
                    reason=(
                        f"new_epoch {new_epoch} does not exceed the highest recorded "
                        f"epoch {prior_epoch} for {prior_conversation}"
                    ),
                )

            run_filter = "AND run_id = ?" if run_id is not None else ""
            run_params: tuple[object, ...] = (
                (prior_conversation, run_id) if run_id is not None else (prior_conversation,)
            )
            runs_changed = conn.execute(
                "UPDATE gate_run SET owner_conversation = ?, owner_epoch = ?, "
                "row_version = row_version + 1 "
                f"WHERE owner_conversation = ? {run_filter}",
                (new_conversation, new_epoch, *run_params),
            ).rowcount

            # Questions in PENDING/ESCALATED whose owner_conversation matches, round
            # bound or not (a NULL round is reachable, DESIGN r2 C1).  run_id narrows.
            q_filter = "AND round_id IN (SELECT round_id FROM gate_round WHERE run_id = ?)"
            q_params: tuple[object, ...] = (
                (new_conversation, new_epoch, prior_conversation, run_id)
                if run_id is not None
                else (new_conversation, new_epoch, prior_conversation)
            )
            questions_changed = conn.execute(
                "UPDATE round_question SET owner_conversation = ?, owner_epoch = ?, "
                "row_version = row_version + 1 "
                "WHERE owner_conversation = ? AND state IN ('PENDING','ESCALATED') "
                + (q_filter if run_id is not None else ""),
                q_params,
            ).rowcount

            transfer_id = new_ulid()
            conn.execute(
                "INSERT INTO ownership_transfer (transfer_id, prior_conversation, "
                "prior_epoch, new_conversation, new_epoch, run_id, runs_rewritten, "
                "questions_rewritten, claimed_by, claimed_at, client_request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transfer_id,
                    prior_conversation,
                    max(prior_epoch, 0),
                    new_conversation,
                    new_epoch,
                    run_id,
                    runs_changed,
                    questions_changed,
                    claimed_by,
                    render_timestamp(self._clock.now()),
                    client_request_id,
                ),
            )
            return ClaimOwnershipResult(
                accepted=True,
                runs_rewritten=runs_changed,
                questions_rewritten=questions_changed,
                transfer_id=transfer_id,
            )

    # -- reads -------------------------------------------------------------

    def get_run(self, run_id: str) -> GateRun | None:
        row = (
            self._pool.connection()
            .execute(
                "SELECT run_id, wp, lane, workflow_source_sha, input_sha, owner_conversation, "
                "owner_epoch, max_rounds, row_version, state FROM gate_run WHERE run_id = ?",
                (run_id,),
            )
            .fetchone()
        )
        return None if row is None else _row_to_run(row)

    def get_round(self, round_id: str) -> GateRound | None:
        row = (
            self._pool.connection()
            .execute(_ROUND_SELECT + " WHERE round_id = ?", (round_id,))
            .fetchone()
        )
        return None if row is None else _row_to_round(row)

    def rounds_for_run(self, run_id: str) -> list[GateRound]:
        rows = self._pool.connection().execute(
            _ROUND_SELECT + " WHERE run_id = ? ORDER BY round_no", (run_id,)
        )
        return [_row_to_round(r) for r in rows]

    def open_findings_for_run(self, run_id: str) -> list[OpenFinding]:
        conn = self._pool.connection()
        rows = conn.execute(
            "SELECT finding_id FROM gate_open_finding WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        findings = [self._load_finding_locked(conn, r["finding_id"]) for r in rows]
        return [f for f in findings if f.is_open]

    def project_round(self, round_id: str) -> RoundProjection | None:
        conn = self._pool.connection()
        round_row = conn.execute(_ROUND_SELECT + " WHERE round_id = ?", (round_id,)).fetchone()
        if round_row is None:
            return None
        round_ = _row_to_round(round_row)
        run = self.get_run(round_.run_id)
        if run is None:
            return None

        dispatches = tuple(
            _row_to_dispatch(r)
            for r in conn.execute(_DISPATCH_SELECT + " WHERE round_id = ?", (round_id,))
        )
        effect_ids = [d.effect_id for d in dispatches if d.effect_id is not None]
        intents = tuple(
            _row_to_intent(r)
            for r in conn.execute(
                "SELECT effect_id, kind, round_id, dispatch_id, approval_ref, requested_at "
                "FROM gate_effect_intent WHERE round_id = ?",
                (round_id,),
            )
        )
        all_effect_ids = {i.effect_id for i in intents} | set(effect_ids)
        results: list[EffectResult] = []
        for eid in sorted(all_effect_ids):
            rr = conn.execute(
                "SELECT effect_id, outcome, evidence_ref, settled_at "
                "FROM gate_effect_result WHERE effect_id = ?",
                (eid,),
            ).fetchone()
            if rr is not None:
                results.append(_row_to_result(rr))
        # Open findings raised anywhere in the run and still open are carried in.
        open_findings = tuple(self.open_findings_for_run(round_.run_id))
        coverage_rows = conn.execute(
            "SELECT round_id, xref_sha, reviewed_artifact_sha, consumers, unresolved_dynamic "
            "FROM gate_consumer_coverage WHERE round_id = ?",
            (round_id,),
        )
        coverage = tuple(_row_to_coverage(r) for r in coverage_rows)
        return RoundProjection(
            run=run,
            round=round_,
            dispatches=dispatches,
            effect_intents=intents,
            effect_results=tuple(results),
            open_findings=open_findings,
            consumer_coverage=coverage,
        )

    # -- locked helpers ----------------------------------------------------

    def _get_run_locked(self, conn: sqlite3.Connection, run_id: str) -> GateRun:
        row = conn.execute(
            "SELECT run_id, wp, lane, workflow_source_sha, input_sha, owner_conversation, "
            "owner_epoch, max_rounds, row_version, state FROM gate_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise GateError(f"no such run: {run_id}")
        return _row_to_run(row)

    def _get_round_locked(self, conn: sqlite3.Connection, round_id: str) -> GateRound:
        row = conn.execute(_ROUND_SELECT + " WHERE round_id = ?", (round_id,)).fetchone()
        if row is None:
            raise GateError(f"no such round: {round_id}")
        return _row_to_round(row)

    def _load_finding_locked(self, conn: sqlite3.Connection, finding_id: str) -> OpenFinding:
        base = conn.execute(
            "SELECT finding_id, raised_in_round, severity, statement "
            "FROM gate_open_finding WHERE finding_id = ?",
            (finding_id,),
        ).fetchone()
        if base is None:
            raise GateError(f"no such finding: {finding_id}")
        disp_rows = conn.execute(
            "SELECT kind, reviewed_artifact_sha, killer_test, killer_mutant, actor, reason, at "
            "FROM gate_disposition WHERE finding_id = ? ORDER BY seq",
            (finding_id,),
        )
        dispositions = tuple(_row_to_disposition(r) for r in disp_rows)
        return OpenFinding(
            finding_id=base["finding_id"],
            raised_in_round=base["raised_in_round"],
            severity=Severity(base["severity"]),
            statement=base["statement"],
            dispositions=dispositions,
        )


# ---------------------------------------------------------------------------
# Row <-> model mapping
# ---------------------------------------------------------------------------

_ROUND_SELECT = (
    "SELECT round_id, run_id, round_no, predecessor_round_id, build_inputs, "
    "review_snapshot, execution_target, test_command, evidence_tier, fixture_corpus_sha, "
    "fixture_frame_count, subject_sha, report_bytes_sha, verdict_report_sha, state, "
    "generation, row_version, created_at, closed_at FROM gate_round"
)

_DISPATCH_SELECT = (
    "SELECT dispatch_id, round_id, role, position, routing_revision, conversation_id, "
    "terminal_incarnation, request_id, effect_id, pins, brief_blob_sha, state, outcome "
    "FROM gate_dispatch"
)


def _row_to_run(row: sqlite3.Row) -> GateRun:
    return GateRun(
        run_id=row["run_id"],
        wp=row["wp"],
        lane=row["lane"],
        workflow_source_sha=row["workflow_source_sha"],
        input_sha=row["input_sha"],
        owner_conversation=row["owner_conversation"],
        owner_epoch=row["owner_epoch"],
        max_rounds=row["max_rounds"],
        row_version=row["row_version"],
        state=RunState(row["state"]),
    )


def _row_to_round(row: sqlite3.Row) -> GateRound:
    return GateRound(
        round_id=row["round_id"],
        run_id=row["run_id"],
        round_no=row["round_no"],
        predecessor_round_id=row["predecessor_round_id"],
        build_inputs=ArtifactManifest.model_validate_json(row["build_inputs"]),
        review_snapshot=(
            None
            if row["review_snapshot"] is None
            else ArtifactManifest.model_validate_json(row["review_snapshot"])
        ),
        execution_target=ExecutionTarget.model_validate_json(row["execution_target"]),
        test_command=row["test_command"],
        evidence_tier=row["evidence_tier"],
        fixture_corpus_sha=row["fixture_corpus_sha"],
        fixture_frame_count=row["fixture_frame_count"],
        subject_sha=row["subject_sha"],
        report_bytes_sha=row["report_bytes_sha"],
        verdict_report_sha=row["verdict_report_sha"],
        state=RoundState(row["state"]),
        generation=row["generation"],
        row_version=row["row_version"],
        created_at=parse_timestamp(row["created_at"]),
        closed_at=None if row["closed_at"] is None else parse_timestamp(row["closed_at"]),
    )


def _row_to_dispatch(row: sqlite3.Row) -> Dispatch:
    return Dispatch(
        dispatch_id=row["dispatch_id"],
        round_id=row["round_id"],
        role=DispatchRole(row["role"]),
        position=row["position"],
        routing_revision=row["routing_revision"],
        conversation_id=row["conversation_id"],
        terminal_incarnation=row["terminal_incarnation"],
        request_id=row["request_id"],
        effect_id=row["effect_id"],
        pins=tuple(json.loads(row["pins"])),
        brief_blob_sha=row["brief_blob_sha"],
        state=DispatchState(row["state"]),
        outcome=row["outcome"],
    )


def _row_to_intent(row: sqlite3.Row) -> EffectIntent:
    return EffectIntent(
        effect_id=row["effect_id"],
        kind=EffectKind(row["kind"]),
        round_id=row["round_id"],
        dispatch_id=row["dispatch_id"],
        approval_ref=row["approval_ref"],
        requested_at=parse_timestamp(row["requested_at"]),
    )


def _row_to_result(row: sqlite3.Row) -> EffectResult:
    return EffectResult(
        effect_id=row["effect_id"],
        outcome=EffectOutcome(row["outcome"]),
        evidence_ref=row["evidence_ref"],
        settled_at=None if row["settled_at"] is None else parse_timestamp(row["settled_at"]),
    )


def _row_to_disposition(row: sqlite3.Row) -> Disposition:
    return Disposition(
        kind=DispositionKind(row["kind"]),
        reviewed_artifact_sha=row["reviewed_artifact_sha"],
        killer_test=row["killer_test"],
        killer_mutant=row["killer_mutant"],
        actor=row["actor"],
        reason=row["reason"],
        at=parse_timestamp(row["at"]),
    )


def _row_to_coverage(row: sqlite3.Row) -> ConsumerCoverage:
    return ConsumerCoverage(
        xref_sha=row["xref_sha"],
        reviewed_artifact_sha=row["reviewed_artifact_sha"],
        consumers=tuple(
            ConsumerDisposition.model_validate(c) for c in json.loads(row["consumers"])
        ),
        unresolved_dynamic=tuple(json.loads(row["unresolved_dynamic"])),
    )
