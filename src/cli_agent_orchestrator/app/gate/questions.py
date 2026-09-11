"""``GateQuestionService`` — the durable question's commands (WP-ARCH A, slice B1).

How a lane STOPS AND ASKS without dying.  A worker (or, in slice C, a workflow
script) calls ``ask``; the question becomes a row, its dispatch is suspended in
``AWAITING_ANSWER`` and an obligation to tell the supervisor is recorded — all in
one transaction, so there is no instant at which somebody is waiting and nothing
is recorded to wake them.  The supervisor calls ``answer``; the answer is
appended, the question settles and an obligation to hand the answer BACK is
recorded.  Nothing here waits: the bounded long poll and the live seat delivery
are slice B2, and until they land an unanswered question is simply readable.

Three properties are worth stating because they are what the slice is FOR.

* **The waiting state is a gate row, not a terminal status.**  ``QuestionState``
  plus ``DispatchState.AWAITING_ANSWER`` plus
  :func:`~cli_agent_orchestrator.core.gate.run_awaiting_answer` say everything
  about who is waiting on what.  No ``TerminalStatus`` member is added, because
  ``WAITING_USER_ANSWER`` already means a PROVIDER DIALOG in a pane and one word
  cannot mean two waits.
* **ANSWERED is not proof of receipt.**  ``answer`` records a delivery intent and
  ``consume_answer`` settles it; the window between them is a real crash window
  (R28) and is visible precisely because the two are separate records.
* **The notifier is a port, never a call into legacy.**  ``app`` may not import
  ``clients`` (``new-code-never-imports-legacy``), so the thing that turns an
  envelope into a queue row arrives as
  :class:`~cli_agent_orchestrator.core.ports.QuestionNotifier` from the
  composition root.  In B1 no notifier is wired and the notice intent stays
  ``PENDING`` for B2's sweep to pick up — a question whose notice was never sent
  is a row a sweep can find, not a lost obligation.

``app/gate/service.py``'s :class:`GateRoundService` owns the ROUND lifecycle;
this class owns the QUESTION lifecycle.  They are separate because a question has
no round at all when a non-gate lane asks one (``round_id`` NULL), so folding the
question commands into the round service would make the round the thing every
question needs, which is exactly what the nullable column exists to deny.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from cli_agent_orchestrator.app.gate.ports import (
    GateStore,
    QuestionAnswer,
    QuestionNotifier,
    RoundQuestion,
)
from cli_agent_orchestrator.app.gate.render import (
    EnvelopeKind,
    IdentityRef,
    render_anomaly_envelope,
    render_question_envelope,
)
from cli_agent_orchestrator.core.gate import (
    ContinuationKind,
    Dispatch,
    DispatchRole,
    GateQuestionError,
    QuestionRefusal,
    QuestionState,
    validate_ask,
)
from cli_agent_orchestrator.core.ports import Clock
from cli_agent_orchestrator.core.timing import GATE_QUESTION_EXPIRY_S

__all__ = ["GateQuestionService"]

#: Open states — the two that hold the dispatch's one-question slot (AC-A10).
OPEN_STATES: tuple[QuestionState, ...] = (QuestionState.PENDING, QuestionState.ESCALATED)


class GateQuestionService:
    """The question commands over a :class:`GateStore` (P3)."""

    def __init__(
        self,
        store: GateStore,
        *,
        clock: Clock,
        notifier: QuestionNotifier | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._notifier = notifier

    # -- commands ----------------------------------------------------------

    def ask(
        self,
        *,
        dispatch_id: str,
        question: str,
        client_request_id: str,
        owner_conversation: str,
        owner_epoch: int = 0,
        round_id: str | None = None,
        options: Sequence[str] = (),
        blocking: bool = True,
        expires_in_s: int = GATE_QUESTION_EXPIRY_S,
        continuation_kind: ContinuationKind = ContinuationKind.ASSIGNMENT,
        continuation_ref: str = "",
        answer_schema: str | None = None,
        default_answer: str | None = None,
        position: str | None = None,
    ) -> tuple[RoundQuestion, bool]:
        """Ask a durable question and suspend the asking dispatch (A2).

        Returns the question and whether it was REPLAYED — an idempotent hit on
        ``client_request_id`` rather than a new row.  Callers derive that key
        deterministically, so asking the same text twice looks exactly like a
        retry; a caller told nothing would act on an answer to an older question
        (N6).

        ``position`` provisions the dispatch when one does not exist yet.  A
        question is bound to an ASSIGNMENT so the run can project as awaiting an
        answer, but a non-gate lane has no round and may never have been recorded
        as a dispatch at all; rather than refuse that lane (which would make the
        primitive useless to exactly the callers A2 is for), the service records
        the ``OTHER`` dispatch first and then asks.  Without ``position`` an
        unknown dispatch is still a typed refusal, so a caller that BELIEVES it
        has a dispatch is never quietly given a new one.

        The ask itself is one transaction in the store; the provisioning is a
        separate, earlier one.  A crash between them leaves a PREPARED dispatch
        with no question, which is inert — the opposite ordering would leave a
        question bound to nothing.
        """
        now = self._clock.now()
        expires_at = now + timedelta(seconds=int(expires_in_s))
        validate_ask(
            blocking=blocking,
            default_answer=default_answer,
            question=question,
            asked_at=now,
            expires_at=expires_at,
        )
        if position is not None:
            self._ensure_dispatch(dispatch_id, position=position, round_id=round_id)

        record, replayed = self._store.ask_question(
            dispatch_id=dispatch_id,
            round_id=round_id,
            client_request_id=client_request_id,
            owner_conversation=owner_conversation,
            owner_epoch=owner_epoch,
            continuation_kind=continuation_kind,
            continuation_ref=continuation_ref or dispatch_id,
            question=question,
            options=tuple(options),
            blocking=blocking,
            asked_at=now,
            expires_at=expires_at,
            answer_schema=answer_schema,
            default_answer=default_answer,
        )
        if not replayed:
            # A replay has already been announced; announcing it again would put
            # a second copy of one question in front of the seat.
            self._notify(record, kind=EnvelopeKind.QUESTION)
        return record, replayed

    def answer(
        self,
        *,
        question_id: str,
        answer: str,
        answered_by: str,
        client_request_id: str,
        caller_conversation: str,
        caller_epoch: int = 0,
    ) -> tuple[RoundQuestion, QuestionAnswer]:
        """Record an answer, conditionally on state, expiry and owner epoch (AC-A12)."""
        return self._store.answer_question(
            question_id=question_id,
            answer=answer,
            answered_by=answered_by,
            client_request_id=client_request_id,
            caller_conversation=caller_conversation,
            caller_epoch=caller_epoch,
            now=self._clock.now(),
        )

    def escalate(self, question_id: str) -> RoundQuestion:
        """Raise a PENDING question to ESCALATED without releasing its slot.

        Escalation is not "ask the user instead": the id is unchanged, so the
        user's answer returns on the SAME question the lane is suspended on.  A
        new question would be a second thing to answer and would leave the first
        one open behind it.

        It NOTIFIES, and that is the point of escalating at all: a raise the seat
        never hears about has changed nothing.  The store refuses to escalate a
        question already past its deadline, so this can never spend a seat's
        attention on a question the next sweep will expire (N10).
        """
        record = self._store.escalate_question(question_id, now=self._clock.now())
        self._notify(record, kind=EnvelopeKind.QUESTION)
        return record

    def consume_answer(self, answer_event_id: str) -> None:
        """Record that the asker RECEIVED the answer, releasing its dispatch (R28)."""
        self._store.mark_answer_consumed(answer_event_id, now=self._clock.now())

    def sweep_expired(self, now: datetime | None = None) -> list[RoundQuestion]:
        """Expire every overdue question and emit ONE anomaly for each (AC-A7).

        The store returns exactly the rows it settled, so a question expired by a
        concurrent sweep is not notified twice.  A question that times out
        therefore becomes one anomaly envelope rather than a lane that goes quiet,
        which is the whole difference between a bounded wait and a silent idle.
        """
        expired = self._store.expire_due_questions(now if now is not None else self._clock.now())
        for record in expired:
            self._notify(record, kind=EnvelopeKind.CONDITION)
        return expired

    # -- reads -------------------------------------------------------------

    def get(self, question_id: str) -> RoundQuestion | None:
        return self._store.get_question(question_id)

    def require(self, question_id: str) -> RoundQuestion:
        """The question, or a typed not-found refusal the API turns into a 404."""
        record = self._store.get_question(question_id)
        if record is None:
            raise GateQuestionError(
                QuestionRefusal.QUESTION_NOT_FOUND, f"no such question: {question_id}"
            )
        return record

    def list_open(
        self,
        *,
        owner_conversation: str | None = None,
        states: Sequence[QuestionState] = OPEN_STATES,
        round_id: str | None = None,
        limit: int = 100,
    ) -> list[RoundQuestion]:
        """Questions in the given states, newest first (the seat's open list)."""
        return self._store.questions_for_owner(
            owner_conversation=owner_conversation,
            states=tuple(states),
            round_id=round_id,
            limit=limit,
        )

    def is_suspended(self, dispatch_id: str) -> bool:
        """Whether this dispatch is waiting on an answer (the B2 reap predicate)."""
        return self._store.open_question_for_dispatch(dispatch_id) is not None

    # -- internals ---------------------------------------------------------

    def _ensure_dispatch(self, dispatch_id: str, *, position: str, round_id: str | None) -> None:
        """Record an ``OTHER`` dispatch, but ONLY when there is not one already.

        ``record_dispatch`` upserts, so an unguarded call would rewrite a live
        gate dispatch's role, position and state — turning a BUILDER mid-round
        into a PREPARED ``OTHER`` because a lane happened to pass ``position``.
        :meth:`GateStore.ensure_dispatch` is one statement, so the check and the
        act cannot be separated by a concurrent write (N2); the earlier
        ``get_dispatch`` then ``record_dispatch`` pair was check-then-act across
        two transactions.
        """
        self._store.ensure_dispatch(
            Dispatch(
                dispatch_id=dispatch_id,
                round_id=round_id,
                role=DispatchRole.OTHER,
                position=position,
                request_id=dispatch_id,
            )
        )

    def _notify(self, record: RoundQuestion, *, kind: EnvelopeKind) -> None:
        """Render the envelope and hand it to the notifier, AFTER the commit.

        Outside the transaction on purpose: a transport that blocks would hold the
        gate's write lock for as long as it blocks, and a transport that fails
        must not roll back a question the asker is already suspended on.  With no
        notifier wired (slice B1) nothing is attempted and the intent stays
        ``PENDING``, which is a row B2's sweep finds — not a lost obligation.
        """
        if self._notifier is None:
            return
        envelope = (
            render_question_envelope(record, identity=self._identity(record))
            if kind is EnvelopeKind.QUESTION
            else render_anomaly_envelope(record, identity=self._identity(record))
        )
        try:
            msg_id = self._notifier.notify(
                question=record,
                kind=kind.value,
                classification=envelope.classification.value,
                lines=envelope.lines,
            )
        except Exception as exc:  # noqa: BLE001 — a failed notice is a row, never a raise
            self._store.mark_notice_failed(record.question_id, error=repr(exc))
            return
        if msg_id is None:
            self._store.mark_notice_failed(record.question_id, error="notifier returned no id")
            return
        self._store.mark_notice_sent(record.question_id, msg_id=msg_id)

    def _identity(self, record: RoundQuestion) -> IdentityRef:
        """The envelope's identity line, read from rows rather than passed in.

        A non-gate question has ``round_id`` NULL and therefore no run, no wp and
        no round number — which is exactly why the renderer takes an identity
        VALUE instead of a ``RoundProjection`` the way ``render_callback`` does.
        """
        who = "cao-gate"
        context = ""
        if record.round_id is not None:
            projection = self._store.project_round(record.round_id)
            if projection is not None:
                who = projection.run.lane
                context = f"{projection.run.wp} r{projection.round.round_no}"
        return IdentityRef(who=who, context=context, ref=record.question_id)
