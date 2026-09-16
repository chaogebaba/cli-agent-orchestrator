"""F862 (#718) D7/D16 r6 — the durable send-intent record and per-attempt counters.

Amendment C's "Code owed before certification" opens with:

    A durable ``SEND_INTENT`` record written before the composer dispatch, with
    recovery, mint and re-export counters that survive a restart.

and D7/D16 states the invariant it exists to make checkable:

    Persist SEND_INTENT before dispatching Enter/click or any equivalent action
    that can cause the app to send. After that dispatch, never repeat the submit
    action or issue a Python POST, including after a timeout, 401, 403, browser
    crash or missing observation. A crash after intent persistence is uncertain
    unless non-dispatch is demonstrated. A correction before demonstrated
    non-dispatch may use at most one recovery within the original attempt
    deadline; counters survive transitions and restart.

At ``14256c1e`` delivery state was an in-memory :class:`DeliveryState` on
``errors.py`` / ``in_page_transport.py``. Nothing was written to disk, so a crash
between intent and dispatch was unresolvable: on restart the runner could not
tell whether the previous process had pressed Enter, and the only safe answers
were "never resend anything" (loses every recoverable attempt) or "resend"
(duplicates work). This module makes the question answerable.

Shape
-----
One JSON file per attempt, ``send_intent.json`` under the attempt directory,
rewritten atomically (``os.replace`` over a same-directory temp) and **fsynced**
— both the file and its parent directory — before the call returns. An intent
that is not on stable storage before the dispatch is not an intent, so the fsync
is part of the contract, not an optimisation.

State machine (the only legal transitions)::

    PENDING ──record_submit_dispatch()──> DISPATCHED ──record_send_observed()──> OBSERVED
       │                                      │
       │ demonstrate_non_dispatch()           └── terminal for submit purposes:
       ▼                                          may_dispatch_submit() is False
    PENDING (recoveries_used += 1, at most _MAX_RECOVERIES)

``demonstrate_non_dispatch`` is the ONLY way back, and it is what "a correction
before demonstrated non-dispatch" means: the caller must have positive evidence
the app did not send — in the browser path, the composer still holding the
prompt text. It consumes the single shared recovery.

Counters (``submits_dispatched``, ``sends_observed``, ``recoveries_used``,
``mints``, ``re_exports``) are part of the persisted record, so they survive a
restart exactly as D7/D16 requires. ``acceptance_failure`` renders the AC-20
disagreement check NB-6 moved out of AC-18: acceptance fails when the two counts
disagree or when either is absent.

This module is pure stdlib over a directory. It never imports Playwright, the
transport or the provider, so the AC-20 mutants run offline with no browser.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: The file name inside the attempt directory. Stable: a restart finds it by name.
SEND_INTENT_FILENAME = "send_intent.json"

#: D7/D16: "at most one recovery within the original attempt deadline". Shared
#: across every correction reason — there is no per-reason allowance.
_MAX_RECOVERIES = 1

#: Bumped when the on-disk shape changes. A record whose version this process
#: does not understand is treated as UNRESOLVABLE (fail closed: no submit).
_SCHEMA_VERSION = 1


class IntentState(str, Enum):
    """Where this attempt stands relative to the submit-triggering action."""

    #: Intent persisted; the submit action has NOT been dispatched. The only
    #: state in which dispatching is permitted.
    PENDING = "pending"
    #: The submit action was dispatched. Nothing may dispatch again, whatever
    #: happens next — timeout, 401, 403, disconnect or crash (D7/D16).
    DISPATCHED = "dispatched"
    #: A send was correlated. Reads only.
    OBSERVED = "observed"


class AttemptState(str, Enum):
    """Amendment D's durable attempt states.

    ``IntentState`` above remains as the compatibility view used by the shipped
    Amendment C runner.  This enum is the authoritative one-use-mint ledger for
    Amendment D; keeping the two fields separate makes migration fail closed
    without reinterpreting existing records.
    """

    LOCKED = "LOCKED"
    OWNED_BROWSER_READY = "OWNED_BROWSER_READY"
    CONNECTOR_READY = "CONNECTOR_READY"
    INPUT_READY = "INPUT_READY"
    INTERCEPT_ARMED = "INTERCEPT_ARMED"
    SEND_INTENT = "SEND_INTENT"
    RELAY_BOUND_OR_SKIPPED = "RELAY_BOUND_OR_SKIPPED"
    COMPOSER_MINT_TRIGGERED = "COMPOSER_MINT_TRIGGERED"
    REQUEST_HELD = "REQUEST_HELD"
    MINT_RESERVED = "MINT_RESERVED"
    PYTHON_POST_INVOKED = "PYTHON_POST_INVOKED"
    RAW_SSE_RELAY = "RAW_SSE_RELAY"
    GET_VERIFY = "GET_VERIFY"
    BROWSER_FULFIL = "BROWSER_FULFIL"
    VALIDATE_PUBLISH = "VALIDATE/PUBLISH"
    ABANDONED_PRE_INVOKE = "ABANDONED_PRE_INVOKE"
    ACK_UNKNOWN = "ACK_UNKNOWN"
    ERROR = "ERROR"


_TERMINAL_ATTEMPT_STATES = {
    AttemptState.ABANDONED_PRE_INVOKE.value,
    AttemptState.ACK_UNKNOWN.value,
    AttemptState.ERROR.value,
    AttemptState.VALIDATE_PUBLISH.value,
}


class SendIntentViolation(Exception):
    """A caller tried to do something D7/D16 forbids.

    Raised rather than returned because every call site is a place where
    continuing would risk a duplicate send. The message names the invariant.
    """


@dataclass
class SendIntentRecord:
    """The persisted record. Field names are the on-disk JSON keys."""

    run_id: str
    attempt_id: str
    prompt_sha: str
    state: str = IntentState.PENDING.value
    schema_version: int = _SCHEMA_VERSION

    # --- counters that survive a restart (D7/D16) -------------------------
    submits_dispatched: int = 0
    sends_observed: int = 0
    recoveries_used: int = 0
    mints: int = 0
    re_exports: int = 0

    # --- provenance -------------------------------------------------------
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    deadline_at: Optional[float] = None
    #: Free-form, redacted breadcrumbs. Never carries a token or a page dump.
    notes: List[str] = field(default_factory=list)

    # --- Amendment D one-use mint / relay custody -----------------------
    attempt_state: str = AttemptState.LOCKED.value
    profile_epoch: Optional[str] = None
    mint_id: Optional[str] = None
    mint_ordinal: int = 1
    relay_token_hash: Optional[str] = None
    relay_token_expires_at: Optional[float] = None
    bound_at: Optional[float] = None
    subscriber_id: Optional[str] = None
    skipped_at: Optional[float] = None
    pre_send_conversation_id: Optional[str] = None
    pre_send_current_node: Optional[str] = None
    attempt_nonce: Optional[str] = None
    body_sha256: Optional[str] = None
    header_names: List[str] = field(default_factory=list)
    page_generation: Optional[int] = None
    context_generation: Optional[int] = None
    cdp_session_generation: Optional[int] = None
    reserved_at: Optional[float] = None
    invoked_at: Optional[float] = None
    route_disposition: Optional[str] = None
    page_disposition: Optional[str] = None
    absence_observed: bool = False
    irreconcilable: bool = False
    supersedes_unresolved: Optional[str] = None
    relay_status: Optional[str] = None
    verified_node_id: Optional[str] = None
    conversation_digest: Optional[str] = None
    fulfilled_at: Optional[float] = None

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def _fsync_dir(directory: Path) -> None:
    """fsync a DIRECTORY so a rename into it is itself durable.

    Without this the temp file's contents are on stable storage but the
    directory entry that names it may not be, and a power loss can leave the
    attempt dir with no intent file at all — the exact ambiguity this module
    exists to remove. Best-effort: some platforms refuse O_RDONLY on a directory.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


class SendIntentLog:
    """Durable, restart-surviving custody of one attempt's send intent.

    Construct with the attempt directory. :meth:`open_attempt` creates the
    record; :meth:`load` re-attaches to it after a restart.
    """

    def __init__(self, attempt_dir: Path) -> None:
        self.attempt_dir = Path(attempt_dir)
        self.path = self.attempt_dir / SEND_INTENT_FILENAME
        self._record: Optional[SendIntentRecord] = None

    # ── persistence ──────────────────────────────────────────────────────

    def _write(self, record: SendIntentRecord) -> None:
        """Atomically replace the record and fsync file + parent directory."""
        record.updated_at = time.time()
        self.attempt_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".send_intent.", suffix=".tmp", dir=str(self.attempt_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record.to_json(), fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(self.path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _fsync_dir(self.attempt_dir)
        self._record = record

    @property
    def record(self) -> SendIntentRecord:
        if self._record is None:
            raise SendIntentViolation(
                "no send-intent record in memory; call open_attempt() or load() first"
            )
        return self._record

    def open_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
        prompt_sha: str,
        deadline_at: Optional[float] = None,
        profile_epoch: Optional[str] = None,
        mint_id: Optional[str] = None,
        mint_ordinal: int = 1,
        relay_token_hash: Optional[str] = None,
        relay_token_expires_at: Optional[float] = None,
        supersedes_unresolved: Optional[str] = None,
    ) -> SendIntentRecord:
        """Persist the PENDING intent. Call BEFORE the submit-triggering action.

        Returns only after the record is on stable storage, so a crash on the
        very next instruction still leaves a resolvable attempt on disk.
        """
        record = SendIntentRecord(
            run_id=run_id,
            attempt_id=attempt_id,
            prompt_sha=prompt_sha,
            deadline_at=deadline_at,
            profile_epoch=profile_epoch,
            mint_id=mint_id,
            mint_ordinal=mint_ordinal,
            relay_token_hash=relay_token_hash,
            relay_token_expires_at=relay_token_expires_at,
            supersedes_unresolved=supersedes_unresolved,
        )
        self._write(record)
        return record

    def create_locked_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
        prompt_sha: str,
        deadline_at: float,
        profile_epoch: str,
        mint_id: str,
        mint_ordinal: int,
        relay_token_hash: str,
        relay_token_expires_at: float,
        supersedes_unresolved: Optional[str] = None,
    ) -> SendIntentRecord:
        """Atomically persist ``LOCKED`` and the relay-token hash.

        The raw token is deliberately not accepted by this API.  Consequently
        there is no intermediate row or serializer that can leak it: callers
        hash first, then receive the value out-of-band exactly once.
        """
        if self.path.exists():
            raise SendIntentViolation(f"attempt {attempt_id} already exists")
        return self.open_attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            prompt_sha=prompt_sha,
            deadline_at=deadline_at,
            profile_epoch=profile_epoch,
            mint_id=mint_id,
            mint_ordinal=mint_ordinal,
            relay_token_hash=relay_token_hash,
            relay_token_expires_at=relay_token_expires_at,
            supersedes_unresolved=supersedes_unresolved,
        )

    def transition(self, state: AttemptState, **fields: Any) -> SendIntentRecord:
        """Persist one Amendment D state transition.

        Terminal rows are immutable, and a reserved mint can never move back
        before ``MINT_RESERVED``.  More specific helpers below enforce the
        transition's required evidence.
        """
        rec = self.record
        if rec.attempt_state in _TERMINAL_ATTEMPT_STATES:
            raise SendIntentViolation(
                f"attempt {rec.attempt_id} is terminal at {rec.attempt_state}; transition refused"
            )
        for key, value in fields.items():
            if key not in SendIntentRecord.__dataclass_fields__:
                raise SendIntentViolation(f"unknown durable field {key!r}")
            if key == "relay_token_hash" and rec.relay_token_hash not in (None, value):
                raise SendIntentViolation("relay token hash is immutable")
            setattr(rec, key, value)
        rec.attempt_state = state.value
        self._write(rec)
        return rec

    def record_send_intent(
        self,
        *,
        conversation_id: Optional[str],
        current_node: Optional[str],
        attempt_nonce: str,
    ) -> SendIntentRecord:
        """Persist the pre-composer uncertainty boundary (D6)."""
        if not attempt_nonce:
            raise SendIntentViolation("SEND_INTENT requires a non-empty attempt nonce")
        return self.transition(
            AttemptState.SEND_INTENT,
            pre_send_conversation_id=conversation_id,
            pre_send_current_node=current_node,
            attempt_nonce=attempt_nonce,
        )

    def record_relay_bound(self, *, subscriber_id: str, bound_at: float) -> SendIntentRecord:
        """Consume the one relay subscriber; rebinding is never allowed."""
        rec = self.record
        if rec.bound_at is not None or rec.subscriber_id is not None:
            raise SendIntentViolation("relay_already_bound")
        if rec.skipped_at is not None or rec.reserved_at is not None:
            raise SendIntentViolation("relay_closed")
        return self.transition(
            AttemptState.RELAY_BOUND_OR_SKIPPED,
            subscriber_id=subscriber_id,
            bound_at=bound_at,
        )

    def record_relay_skipped(self, *, skipped_at: float) -> SendIntentRecord:
        rec = self.record
        if rec.bound_at is not None:
            raise SendIntentViolation("bound relay cannot be marked skipped")
        return self.transition(AttemptState.RELAY_BOUND_OR_SKIPPED, skipped_at=skipped_at)

    def record_request_held(
        self,
        *,
        body_sha256: str,
        header_names: Sequence[str],
        page_generation: int,
        context_generation: int,
        cdp_session_generation: int,
    ) -> SendIntentRecord:
        """Persist only non-secret proof identifying the live holder."""
        return self.transition(
            AttemptState.REQUEST_HELD,
            body_sha256=body_sha256,
            header_names=sorted({str(name).lower() for name in header_names}),
            page_generation=page_generation,
            context_generation=context_generation,
            cdp_session_generation=cdp_session_generation,
        )

    def reserve_mint(self, *, reserved_at: Optional[float] = None) -> SendIntentRecord:
        """Write and fsync the one-way consumption point before network I/O."""
        rec = self.record
        if rec.reserved_at is not None or rec.attempt_state in {
            AttemptState.MINT_RESERVED.value,
            AttemptState.PYTHON_POST_INVOKED.value,
            AttemptState.RAW_SSE_RELAY.value,
            AttemptState.GET_VERIFY.value,
            AttemptState.BROWSER_FULFIL.value,
        }:
            raise SendIntentViolation("mint already reserved/spent; second invocation refused")
        # ``_write`` fsyncs the file and directory before returning.
        return self.transition(
            AttemptState.MINT_RESERVED,
            reserved_at=time.time() if reserved_at is None else reserved_at,
        )

    def record_python_post_invoked(self, *, invoked_at: Optional[float] = None) -> SendIntentRecord:
        rec = self.record
        if rec.attempt_state != AttemptState.MINT_RESERVED.value or rec.reserved_at is None:
            raise SendIntentViolation("Python POST requires a fsynced MINT_RESERVED row")
        if rec.invoked_at is not None:
            raise SendIntentViolation("mint already invoked; second POST refused")
        return self.transition(
            AttemptState.PYTHON_POST_INVOKED,
            invoked_at=time.time() if invoked_at is None else invoked_at,
        )

    def record_ack_unknown(
        self,
        *,
        route_disposition: Optional[str],
        page_disposition: str,
        absence_observed: bool = False,
        irreconcilable: bool = False,
    ) -> SendIntentRecord:
        if page_disposition not in {"quarantined", "closed", "live"}:
            raise SendIntentViolation("ACK_UNKNOWN requires a valid page disposition")
        return self.transition(
            AttemptState.ACK_UNKNOWN,
            route_disposition=route_disposition,
            page_disposition=page_disposition,
            absence_observed=absence_observed,
            irreconcilable=irreconcilable,
        )

    def record_abandoned_pre_invoke(
        self, *, route_disposition: str, page_disposition: str
    ) -> SendIntentRecord:
        if route_disposition != "aborted":
            raise SendIntentViolation(
                "ABANDONED_PRE_INVOKE is resend-safe only after holder-owned abort"
            )
        if page_disposition not in {"quarantined", "closed"}:
            raise SendIntentViolation("page must be closed or quarantined before profile release")
        return self.transition(
            AttemptState.ABANDONED_PRE_INVOKE,
            route_disposition=route_disposition,
            page_disposition=page_disposition,
        )

    def can_fresh_same_turn_mint(self) -> bool:
        rec = self.record
        return (
            rec.attempt_state == AttemptState.ABANDONED_PRE_INVOKE.value
            and rec.route_disposition == "aborted"
            and rec.page_disposition in {"quarantined", "closed"}
        )

    def restart_action(self) -> str:
        """Return the D6 recovery action for the current durable row."""
        state = self.record.attempt_state
        if state in {
            AttemptState.LOCKED.value,
            AttemptState.OWNED_BROWSER_READY.value,
            AttemptState.CONNECTOR_READY.value,
            AttemptState.INPUT_READY.value,
            AttemptState.INTERCEPT_ARMED.value,
        }:
            return "restart_from_locked"
        if state in {
            AttemptState.SEND_INTENT.value,
            AttemptState.RELAY_BOUND_OR_SKIPPED.value,
            AttemptState.COMPOSER_MINT_TRIGGERED.value,
            AttemptState.REQUEST_HELD.value,
        }:
            return "ack_unknown_reconcile_never_mint"
        if state in {
            AttemptState.MINT_RESERVED.value,
            AttemptState.PYTHON_POST_INVOKED.value,
            AttemptState.RAW_SSE_RELAY.value,
        }:
            return "get_reconcile_mint_spent"
        if state == AttemptState.GET_VERIFY.value:
            return "resume_get_verify"
        if state == AttemptState.BROWSER_FULFIL.value:
            return "reload_compare_never_fulfil"
        return "terminal"

    def load(self) -> Optional[SendIntentRecord]:
        """Re-attach to a record left by a previous process, or None if absent.

        A record this process cannot parse — corrupt JSON, or a schema version
        from the future — is reported as UNRESOLVABLE by raising, because the
        fail-open alternative is a duplicate send.
        """
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SendIntentViolation(
                f"send-intent record at {self.path} is unreadable ({exc.__class__.__name__}); "
                "the attempt is UNRESOLVABLE and must not be re-submitted (D7/D16)"
            ) from exc
        version = raw.get("schema_version")
        if version != _SCHEMA_VERSION:
            raise SendIntentViolation(
                f"send-intent record at {self.path} has schema_version={version!r}, "
                f"this runner understands {_SCHEMA_VERSION}; the attempt is UNRESOLVABLE "
                "and must not be re-submitted (D7/D16)"
            )
        known = {f for f in SendIntentRecord.__dataclass_fields__}
        self._record = SendIntentRecord(**{k: v for k, v in raw.items() if k in known})
        return self._record

    # ── the invariant ────────────────────────────────────────────────────

    def may_dispatch_submit(self) -> bool:
        """True iff dispatching a submit action now is permitted (D7/D16).

        False for DISPATCHED and OBSERVED — which is the whole rule: after the
        dispatch, a timeout, a 401, a 403, a disconnect, a browser crash and a
        missing observation all leave this False.
        """
        return self.record.state == IntentState.PENDING.value

    def record_submit_dispatch(self) -> SendIntentRecord:
        """Move PENDING → DISPATCHED and count the action. Persisted before returning.

        Call IMMEDIATELY BEFORE the Enter/click. Persisting first is what makes a
        crash between this line and the keypress resolvable: the record says
        DISPATCHED, so recovery reads and never resends.
        """
        rec = self.record
        if rec.state != IntentState.PENDING.value:
            raise SendIntentViolation(
                f"refusing a second submit action: attempt {rec.attempt_id} is already "
                f"'{rec.state}' (submits_dispatched={rec.submits_dispatched}). After the "
                "submit action is dispatched it is NEVER repeated — not after a timeout, "
                "401, 403, disconnect, crash or missing observation (D7/D16)."
            )
        rec.state = IntentState.DISPATCHED.value
        rec.submits_dispatched += 1
        # Amendment D's durable view records that the composer trigger happened;
        # the legacy ``state`` field remains for AC-20 compatibility.
        if rec.attempt_state in {
            AttemptState.LOCKED.value,
            AttemptState.INTERCEPT_ARMED.value,
            AttemptState.SEND_INTENT.value,
            AttemptState.RELAY_BOUND_OR_SKIPPED.value,
        }:
            rec.attempt_state = AttemptState.COMPOSER_MINT_TRIGGERED.value
        self._write(rec)
        return rec

    def demonstrate_non_dispatch(self, evidence: str) -> SendIntentRecord:
        """Positive evidence the app did NOT send; consumes the one recovery.

        The browser path's evidence is the composer still holding the prompt
        text. Returns the record in PENDING so exactly one further dispatch is
        permitted; a second call raises, which is the shared budget D7/D16
        specifies (and which AC-20's "a recovery resets its budget" mutant
        attacks).
        """
        rec = self.record
        if rec.state == IntentState.OBSERVED.value:
            raise SendIntentViolation(
                f"attempt {rec.attempt_id} has an OBSERVED send; non-dispatch cannot be "
                "demonstrated after acceptance (D7/D16)"
            )
        if rec.recoveries_used >= _MAX_RECOVERIES:
            raise SendIntentViolation(
                f"recovery budget exhausted for attempt {rec.attempt_id} "
                f"(recoveries_used={rec.recoveries_used}, max={_MAX_RECOVERIES}). "
                "At most ONE recovery is permitted within the original attempt "
                "deadline, and it is never reset (D7/D16)."
            )
        if rec.deadline_at is not None and time.time() > rec.deadline_at:
            raise SendIntentViolation(
                f"attempt {rec.attempt_id} is past its deadline; a recovery is permitted "
                "only WITHIN the original attempt deadline (D7/D16)"
            )
        rec.recoveries_used += 1
        rec.state = IntentState.PENDING.value
        rec.notes.append(f"non-dispatch demonstrated: {evidence}"[:200])
        self._write(rec)
        return rec

    def record_send_observed(self) -> SendIntentRecord:
        """A send was correlated: DISPATCHED → OBSERVED, ``sends_observed`` += 1."""
        rec = self.record
        if rec.state == IntentState.PENDING.value:
            raise SendIntentViolation(
                f"attempt {rec.attempt_id} observed a send while still PENDING — a send "
                "was correlated with no recorded dispatch, which means the counters no "
                "longer describe what happened (D7/D16, AC-20)"
            )
        rec.state = IntentState.OBSERVED.value
        rec.sends_observed += 1
        self._write(rec)
        return rec

    def record_mint(self) -> SendIntentRecord:
        """Count a mint cycle (survives restart)."""
        rec = self.record
        rec.mints += 1
        self._write(rec)
        return rec

    def record_re_export(self) -> SendIntentRecord:
        """Count a re-export (survives restart)."""
        rec = self.record
        rec.re_exports += 1
        self._write(rec)
        return rec

    # ── acceptance ───────────────────────────────────────────────────────

    def acceptance_failure(self) -> Optional[str]:
        """The AC-20 counter check. ``None`` when acceptance may proceed.

        NB-6 moved this obligation out of AC-18: acceptance fails when the
        submit-action and observed-send counts DISAGREE, or when either is
        ABSENT. A hidden duplicate submission shows up as
        ``submits_dispatched > sends_observed`` and fails here.
        """
        rec = self.record
        if rec.submits_dispatched == 0:
            return (
                f"attempt {rec.attempt_id}: no submit action was recorded "
                "(submits_dispatched=0); acceptance requires the count, not its absence "
                "(AC-20)"
            )
        if rec.sends_observed == 0:
            return (
                f"attempt {rec.attempt_id}: no send was observed (sends_observed=0) "
                f"against submits_dispatched={rec.submits_dispatched} (AC-20)"
            )
        if rec.submits_dispatched != rec.sends_observed:
            return (
                f"attempt {rec.attempt_id}: submit/send counts disagree "
                f"(submits_dispatched={rec.submits_dispatched}, "
                f"sends_observed={rec.sends_observed}); a hidden duplicate submission "
                "fails acceptance (AC-20)"
            )
        return None

    def counters(self) -> Dict[str, int]:
        """The counter block for the report/envelope."""
        rec = self.record
        return {
            "submits_dispatched": rec.submits_dispatched,
            "sends_observed": rec.sends_observed,
            "recoveries_used": rec.recoveries_used,
            "mints": rec.mints,
            "re_exports": rec.re_exports,
        }


def resolve_after_restart(attempt_dir: Path) -> "tuple[Optional[SendIntentRecord], bool]":
    """Read an attempt left by a dead process: ``(record, may_dispatch_submit)``.

    The crash mutant's entry point. ``(None, True)`` means no intent was ever
    persisted, so nothing was dispatched and a fresh attempt is safe.
    ``(record, False)`` means the previous process had already dispatched (or
    observed) — recovery reads and NEVER resends, which is D7/D16's "a crash
    after intent persistence is uncertain unless non-dispatch is demonstrated".
    """
    log = SendIntentLog(attempt_dir)
    record = log.load()
    if record is None:
        return None, True
    return record, log.may_dispatch_submit()
