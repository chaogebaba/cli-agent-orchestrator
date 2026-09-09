"""F875 secretary desk service — read-only lookup admission, handles, queue
bound, deadlines, and the DeskUsage projection (D1, D5).

This module owns the *server-side* logic the ``desk`` / ``desk_status`` MCP
tools are thin clients of. It is deliberately pure and DB-driven: every
decision reads or writes the ``desk_bindings`` / ``desk_queries`` /
``desk_events`` tables through the database module's ``SessionLocal`` /
``_utcnow`` seams, so the acceptance tests drive it directly with a tmp sqlite
engine and a ``SimClock`` — no live provider, tmux, or HTTP.

Shadow mode (R1a): nothing here is wired into the live seat create path or the
hooks. The reconciler (``desk_reconciler``) and the MCP tools call in; the
cutover that retires the legacy emitters is R1b/D7 and is NOT performed here.

Contracts (blueprint §3 / §5):

* ``desk(question, decision?, scope?, request_id?)`` — admit a read-only
  lookup, wait at most 20 s, and return either a cited answer (<= 8 lines and
  <= 2048 UTF-8 bytes, citations included) or one PENDING line carrying the
  handle. Passing a prior ``request_id`` retrieves that record and dispatches
  no second job (AC-1, AC-2). With four queries already queued the fifth
  returns ``BUSY_QUEUE_FULL`` inline, enqueuing nothing and starting no
  deadline (AC-19). An over-budget answer becomes a compact status plus an
  artifact pointer, never a truncated quotation (AC-1).
* The completion is addressed to the query record, not the inbox (AC-3): this
  module never enqueues an inbox message; ``complete_query`` writes the answer
  onto the record and callers read it back through the handle.
* Retries, pings, spawns and compaction do not inflate completed retrievals;
  ``NOT_FOUND`` is a completed retrieval with its own outcome; UNKNOWN totals
  stay visible beside the routed share (AC-10).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.clients.database import (
    DeskBindingModel,
    DeskEventModel,
    DeskQueryModel,
    SessionLocal,
    _utcnow,
)

# --- Contract constants (blueprint D1) -------------------------------------
WAIT_SECONDS = 20  # inline wait
JOB_DEADLINE_SECONDS = 120  # then a typed failure
QUEUE_DEPTH_MAX = 4  # the fifth concurrent query is refused inline (B4)
ANSWER_MAX_LINES = 8
ANSWER_MAX_BYTES = 2048  # UTF-8, citations included

# Query states that still occupy a queue slot on a desk.
_ACTIVE_QUERY_STATES = ("ADMITTED", "RUNNING")

# desk_events.kind vocabulary (D5).
EV_QUERY_ADMITTED = "query_admitted"
EV_QUERY_COMPLETED = "query_completed"
EV_QUERY_FAILED = "query_failed"
EV_CITED_ANSWER = "cited_answer"
EV_NATIVE_LOCAL_DISCOVERY = "native_local_discovery"
EV_EXEMPT_READ = "exempt_read"
EV_UNCLASSIFIED_RETRIEVAL = "unclassified_retrieval"
EV_OPAQUE_BASH = "opaque_bash"
EV_EXPLICIT_OVERRIDE = "explicit_override"
EV_EDIT_INTENT_WITHOUT_EDIT = "edit_intent_without_edit"
EV_NOTICE_EMITTED = "notice_emitted"
EV_STATE_ENTER = "state_enter"
EV_TELEMETRY_GAP = "telemetry_gap"


class DeskServiceError(Exception):
    """Base for typed desk-service errors surfaced inline to the tool."""

    code = "desk_error"


class QueueFullError(DeskServiceError):
    """The desk already has QUEUE_DEPTH_MAX active queries (AC-19)."""

    code = "BUSY_QUEUE_FULL"


class NoBindingError(DeskServiceError):
    """No desk binding exists for this conversation."""

    code = "NO_DESK"


@dataclass(frozen=True)
class AdmitResult:
    """Outcome of an admit call. Exactly one of the shape flags is meaningful."""

    kind: str  # "admitted" | "replayed" | "queue_full"
    request_id: Optional[str] = None
    state: Optional[str] = None
    replayed: bool = False


@dataclass(frozen=True)
class WaitResult:
    """Outcome of the 20 s inline wait."""

    kind: str  # "answer" | "pending" | "failed"
    request_id: str
    answer_lines: Optional[Tuple[str, ...]] = None
    artifact_ref: Optional[str] = None
    outcome: Optional[str] = None
    pending_line: Optional[str] = None


@dataclass(frozen=True)
class DeskUsage:
    """Server-owned projection over deduplicated desk_events (D5)."""

    conversation_id: str
    admitted: int = 0
    completed: int = 0
    failed: int = 0
    pending: int = 0
    cited_answers: int = 0
    native_local_discovery: int = 0
    exempt_reads_by_reason: Dict[str, int] = field(default_factory=dict)
    unclassified_retrieval: int = 0
    opaque_bash_calls: int = 0
    opaque_output_bytes: int = 0
    explicit_overrides: int = 0
    edit_intents_without_edit: int = 0
    seconds_by_state: Dict[str, int] = field(default_factory=dict)
    longest_unavailable_s: int = 0
    warm_latency_ms_p50: int = 0
    warm_latency_ms_p95: int = 0
    notices_emitted: int = 0
    notice_bytes_emitted: int = 0
    telemetry_gaps: int = 0
    coverage_note: str = ""
    routed_share: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- Budget helper ----------------------------------------------------------
def enforce_answer_budget(
    lines: List[str],
) -> Tuple[Optional[Tuple[str, ...]], bool]:
    """Return ``(answer_lines, over_budget)``.

    An answer within budget (<= 8 lines AND <= 2048 UTF-8 bytes including the
    newlines that join the lines) is returned verbatim. An over-budget answer
    returns ``(None, True)``: the caller stores a compact status plus an
    artifact pointer rather than a truncated quotation (AC-1).
    """
    joined = "\n".join(lines)
    over = len(lines) > ANSWER_MAX_LINES or len(joined.encode("utf-8")) > ANSWER_MAX_BYTES
    if over:
        return None, True
    return tuple(lines), False


# --- Event recording (deduplicated; D5) ------------------------------------
def record_event(
    conversation_id: str,
    incarnation: str,
    kind: str,
    dedup_key: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    db: Any = None,
) -> bool:
    """Record one desk event, deduplicated on ``(conversation_id, kind,
    dedup_key)``.

    Returns True if a new row was inserted, False if the event was already
    present (a retry/spawn/compaction re-recording the same event is a no-op —
    the property AC-10 depends on). Safe to call inside an existing session
    (pass ``db``) or standalone.
    """

    def _do(session: Any) -> bool:
        existing = (
            session.query(DeskEventModel)
            .filter_by(conversation_id=conversation_id, kind=kind, dedup_key=dedup_key)
            .one_or_none()
        )
        if existing is not None:
            return False
        session.add(
            DeskEventModel(
                conversation_id=conversation_id,
                incarnation=incarnation,
                kind=kind,
                dedup_key=dedup_key,
                payload=payload,
                observed_at=_utcnow(),
            )
        )
        session.flush()
        return True

    if db is not None:
        return _do(db)
    with SessionLocal.begin() as session:
        return _do(session)


# --- Admission (D1, B4) -----------------------------------------------------
def admit_query(
    conversation_id: str,
    question: str,
    *,
    incarnation: Optional[str] = None,
    decision: Optional[str] = None,
    scope: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AdmitResult:
    """Admit a read-only lookup, or replay a prior handle, or refuse inline.

    * A supplied ``request_id`` that names an existing record REPLAYS it and
      dispatches no second job (AC-2).
    * With ``QUEUE_DEPTH_MAX`` active queries already on the desk, returns a
      ``queue_full`` result — enqueuing nothing and starting no deadline
      (AC-19).
    * Otherwise creates a new ``DeskQuery`` with a 20 s wait deadline and a
      120 s job deadline, increments the binding's queue depth, and records a
      deduplicated ``query_admitted`` event.
    """
    with SessionLocal.begin() as db:
        binding = (
            db.query(DeskBindingModel).filter_by(conversation_id=conversation_id).one_or_none()
        )
        if binding is None:
            raise NoBindingError(f"no desk binding for {conversation_id}")
        inc = incarnation if incarnation is not None else str(binding.incarnation)

        # Replay: a known handle retrieves its record; never a second job.
        if request_id is not None:
            existing = db.query(DeskQueryModel).filter_by(request_id=request_id).one_or_none()
            if existing is not None:
                return AdmitResult(
                    kind="replayed",
                    request_id=existing.request_id,
                    state=existing.state,
                    replayed=True,
                )

        # Queue bound: count only queries still occupying a slot (B4).
        active = (
            db.query(DeskQueryModel)
            .filter(
                DeskQueryModel.conversation_id == conversation_id,
                DeskQueryModel.state.in_(_ACTIVE_QUERY_STATES),
            )
            .count()
        )
        if active >= QUEUE_DEPTH_MAX:
            # Refuse inline: enqueue nothing, start no deadline, keep the four
            # queued queries on their own deadlines.
            return AdmitResult(kind="queue_full", state=binding.state)

        now = _utcnow()
        new_id = request_id or f"dq_{uuid.uuid4().hex[:16]}"
        db.add(
            DeskQueryModel(
                request_id=new_id,
                conversation_id=conversation_id,
                incarnation=inc,
                question=question if decision is None else f"{question}\n[decision] {decision}",
                scope=scope,
                state="ADMITTED",
                admitted_at=now,
                wait_deadline=now + timedelta(seconds=WAIT_SECONDS),
                job_deadline=now + timedelta(seconds=JOB_DEADLINE_SECONDS),
            )
        )
        binding.queue_depth = int(binding.queue_depth) + 1
        db.flush()
        record_event(conversation_id, inc, EV_QUERY_ADMITTED, new_id, db=db)
        return AdmitResult(kind="admitted", request_id=new_id, state="ADMITTED")


# --- Completion (addressed to the record, never the inbox; AC-3) ------------
def complete_query(
    request_id: str,
    answer_lines: List[str],
    *,
    outcome: str = "ANSWERED",
    artifact_ref: Optional[str] = None,
) -> DeskQueryModel:
    """Mark a query COMPLETED, writing the cited answer onto the record.

    Enforces the answer budget: an over-budget answer stores a compact status
    plus ``artifact_ref`` instead of a truncated quotation (AC-1). Records a
    deduplicated ``query_completed`` event, plus ``cited_answer`` when the
    outcome is an actual cited answer (NOT_FOUND is a completed retrieval but
    NOT a cited answer — AC-10). Never enqueues an inbox message (AC-3).
    """
    bounded, over = enforce_answer_budget(answer_lines)
    with SessionLocal.begin() as db:
        q = db.query(DeskQueryModel).filter_by(request_id=request_id).one_or_none()
        if q is None:
            raise DeskServiceError(f"unknown request_id {request_id}")
        if over:
            ref = artifact_ref or f"artifact://desk/{request_id}"
            q.answer_lines = [
                f"[desk] answer over budget ({len(answer_lines)} lines); full evidence: {ref}"
            ]
            q.artifact_ref = ref
        else:
            q.answer_lines = list(bounded or ())
            q.artifact_ref = artifact_ref
        q.state = "COMPLETED"
        q.outcome = outcome
        q.completed_at = _utcnow()

        record_event(q.conversation_id, str(q.incarnation), EV_QUERY_COMPLETED, request_id, db=db)
        # A cited answer is a useful completion; NOT_FOUND is completed but not
        # cited, and does not advance last_useful_completion.
        if outcome not in ("NOT_FOUND", "FAILED"):
            record_event(q.conversation_id, str(q.incarnation), EV_CITED_ANSWER, request_id, db=db)
            binding = (
                db.query(DeskBindingModel)
                .filter_by(conversation_id=q.conversation_id)
                .one_or_none()
            )
            if binding is not None:
                binding.last_useful_completion = q.completed_at
        _release_slot(db, q.conversation_id)
        db.flush()
        db.refresh(q)
        db.expunge(q)
        return q


def _release_slot(db: Any, conversation_id: str) -> None:
    binding = db.query(DeskBindingModel).filter_by(conversation_id=conversation_id).one_or_none()
    if binding is not None and int(binding.queue_depth) > 0:
        binding.queue_depth = int(binding.queue_depth) - 1


# --- Inline wait (D1) -------------------------------------------------------
def wait_for(request_id: str) -> WaitResult:
    """Return the query's current outcome as of now (the 20 s inline wait is
    driven by the caller advancing the clock / re-polling).

    * COMPLETED → an answer result carrying the cited lines (or the compact
      over-budget status + artifact pointer).
    * FAILED / EXPIRED → a typed failed result (never an open pending state,
      AC-2).
    * still ADMITTED/RUNNING within the wait → one PENDING line plus the
      handle, with no polling instructions and no automatic seat wake (D1).
    """
    with SessionLocal() as db:
        q = db.query(DeskQueryModel).filter_by(request_id=request_id).one_or_none()
        if q is None:
            raise DeskServiceError(f"unknown request_id {request_id}")
        if q.state == "COMPLETED":
            return WaitResult(
                kind="answer",
                request_id=request_id,
                answer_lines=tuple(q.answer_lines or ()),
                artifact_ref=q.artifact_ref,
                outcome=q.outcome,
            )
        if q.state in ("FAILED", "EXPIRED"):
            return WaitResult(kind="failed", request_id=request_id, outcome=q.outcome or q.state)
        return WaitResult(
            kind="pending",
            request_id=request_id,
            pending_line=f"[desk] PENDING; handle={request_id}; desk(request_id={request_id})",
        )


# --- Deadline sweep (D1, AC-2) ---------------------------------------------
def expire_overdue(conversation_id: Optional[str] = None) -> int:
    """Turn every past-``job_deadline`` non-terminal query into a typed
    FAILURE (never an open pending state, AC-2). Returns the count expired.

    Releases the queue slot each expiry frees, so a stalled query cannot pin a
    slot forever behind a healthy-looking binding (B4).
    """
    now = _utcnow()
    expired = 0
    with SessionLocal.begin() as db:
        query = db.query(DeskQueryModel).filter(
            DeskQueryModel.state.in_(_ACTIVE_QUERY_STATES),
            DeskQueryModel.job_deadline <= now,
        )
        if conversation_id is not None:
            query = query.filter(DeskQueryModel.conversation_id == conversation_id)
        for q in query.all():
            q.state = "EXPIRED"
            q.outcome = "DEADLINE_EXCEEDED"
            q.completed_at = now
            record_event(
                q.conversation_id, str(q.incarnation), EV_QUERY_FAILED, q.request_id, db=db
            )
            _release_slot(db, q.conversation_id)
            expired += 1
    return expired


# --- DeskUsage projection (D5) ---------------------------------------------
def project_usage(conversation_id: str) -> DeskUsage:
    """Compute the DeskUsage projection over deduplicated desk_events plus the
    live query/binding state.

    Totals are keyed by conversation, incarnation and request-or-tool-call id
    via the ``desk_events`` uniqueness constraint, so a retry, a spawn, a
    send_message or a compaction cannot inflate them (AC-10). ``NOT_FOUND``
    counts as a completed retrieval; UNKNOWN totals (opaque Bash) and telemetry
    gaps stay visible beside the routed share.
    """
    with SessionLocal() as db:
        events = db.query(DeskEventModel).filter_by(conversation_id=conversation_id).all()
        # Query-state counts come from the durable query rows (also deduplicated
        # by request_id primary key).
        pending = (
            db.query(DeskQueryModel)
            .filter(
                DeskQueryModel.conversation_id == conversation_id,
                DeskQueryModel.state.in_(_ACTIVE_QUERY_STATES),
            )
            .count()
        )

    kinds: Dict[str, int] = {}
    exempt_by_reason: Dict[str, int] = {}
    opaque_bytes = 0
    for ev in events:
        kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
        if ev.kind == EV_EXEMPT_READ:
            reason = (ev.payload or {}).get("reason", "unknown")
            exempt_by_reason[reason] = exempt_by_reason.get(reason, 0) + 1
        if ev.kind == EV_OPAQUE_BASH:
            opaque_bytes += int((ev.payload or {}).get("output_bytes", 0))

    admitted = kinds.get(EV_QUERY_ADMITTED, 0)
    completed = kinds.get(EV_QUERY_COMPLETED, 0)
    failed = kinds.get(EV_QUERY_FAILED, 0)
    cited = kinds.get(EV_CITED_ANSWER, 0)
    native = kinds.get(EV_NATIVE_LOCAL_DISCOVERY, 0)
    unclassified = kinds.get(EV_UNCLASSIFIED_RETRIEVAL, 0)
    opaque_calls = kinds.get(EV_OPAQUE_BASH, 0)
    overrides = kinds.get(EV_EXPLICIT_OVERRIDE, 0)
    edit_no_edit = kinds.get(EV_EDIT_INTENT_WITHOUT_EDIT, 0)
    notices = kinds.get(EV_NOTICE_EMITTED, 0)
    gaps = kinds.get(EV_TELEMETRY_GAP, 0)

    # Routed share is published ONLY for the measured set: completed desk
    # retrievals over completed desk retrievals plus observed non-exempt native
    # retrievals. UNKNOWN (opaque bash) sits beside it, never folded in (D5).
    denom = completed + native
    routed_share = (completed / denom) if denom > 0 else None
    coverage = f"measured_denominator={denom}; unknown_opaque_bash={opaque_calls}; gaps={gaps}"

    return DeskUsage(
        conversation_id=conversation_id,
        admitted=admitted,
        completed=completed,
        failed=failed,
        pending=pending,
        cited_answers=cited,
        native_local_discovery=native,
        exempt_reads_by_reason=exempt_by_reason,
        unclassified_retrieval=unclassified,
        opaque_bash_calls=opaque_calls,
        opaque_output_bytes=opaque_bytes,
        explicit_overrides=overrides,
        edit_intents_without_edit=edit_no_edit,
        notices_emitted=notices,
        notice_bytes_emitted=0,
        telemetry_gaps=gaps,
        coverage_note=coverage,
        routed_share=routed_share,
    )


# --- Noise budget: server-owned notice-slot counter (D4, B6) ----------------
# The maximum unsolicited seat output is four one-line notices, each <= 192
# UTF-8 bytes, so 768 bytes total per conversation. Three slots are operational
# and one is the reserved summary. The counter is server-owned and keyed by
# identity_key ALONE (r2 fold #4: a resume does NOT buy fresh slots — never keyed
# by (identity_key, generation)). It lives on the deduplicated desk_events log
# (kind='notice_emitted'), so compaction, desk replacement, a repeated Stop or a
# server restart cannot replenish it by construction (AC-8). A refused or
# unavailable grant means emit nothing (AC-9): the hook adapters (R1b) request a
# slot and emit only on a grant.
NOTICE_BUDGET_SLOTS = 4
NOTICE_MAX_BYTES = 192  # per line, including newline


@dataclass(frozen=True)
class SlotGrant:
    """Outcome of a notice-slot request."""

    granted: bool
    slots_used: int
    slots_remaining: int
    reason: Optional[str] = None


def request_notice_slot(
    conversation_id: str,
    *,
    slot_kind: str = "operational",
    incarnation: str = "*",
) -> SlotGrant:
    """Atomically request one notice slot for a conversation.

    Grants only while fewer than ``NOTICE_BUDGET_SLOTS`` notice_emitted events
    exist for this conversation. The grant is recorded as a deduplicated
    ``notice_emitted`` event so the spend is durable and cannot be replayed for
    free. Keyed by ``conversation_id`` (the identity_key) alone — generation is
    never part of the key (r2 fold #4). Returns ``granted=False`` when the
    budget is exhausted; the caller then emits nothing (AC-9).
    """
    with SessionLocal.begin() as db:
        used = (
            db.query(DeskEventModel)
            .filter_by(conversation_id=conversation_id, kind=EV_NOTICE_EMITTED)
            .count()
        )
        if used >= NOTICE_BUDGET_SLOTS:
            return SlotGrant(
                granted=False,
                slots_used=used,
                slots_remaining=0,
                reason="budget_exhausted",
            )
        # The dedup_key is the slot ordinal; a re-request for the same ordinal is
        # a no-op, and a distinct ordinal is a new spend. Ordinal = current count.
        dedup = f"slot:{used}"
        inserted = record_event(
            conversation_id,
            incarnation,
            EV_NOTICE_EMITTED,
            dedup,
            payload={"slot_kind": slot_kind, "ordinal": used},
            db=db,
        )
        if not inserted:
            # Extremely rare race on the same ordinal: treat as no grant.
            return SlotGrant(
                granted=False,
                slots_used=used,
                slots_remaining=NOTICE_BUDGET_SLOTS - used,
                reason="race",
            )
        return SlotGrant(
            granted=True,
            slots_used=used + 1,
            slots_remaining=NOTICE_BUDGET_SLOTS - (used + 1),
        )
