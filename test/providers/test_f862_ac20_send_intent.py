"""F862 (#718) AC-20 r6 — durable send intent, the recovery budget, and the
crash / disconnect / invoked-error mutants.

Amendment C, "Code owed before certification", owes three rows that all land
here:

* a durable ``SEND_INTENT`` record written before the composer dispatch, with
  counters that survive a restart (D7/D16, AC-20);
* the AC-20 crash, disconnect and invoked-error mutants — "no row for them
  exists in the r3 AC table, so 'no repeated submit after ambiguity' is
  untested";
* per-attempt recording of submit actions dispatched and sends observed — the
  obligation NB-6 moves out of AC-18.

The invariant under test (D7/D16, quoted):

    Persist SEND_INTENT before dispatching Enter/click or any equivalent action
    that can cause the app to send. After that dispatch, never repeat the submit
    action or issue a Python POST, including after a timeout, 401, 403, browser
    crash or missing observation. A crash after intent persistence is uncertain
    unless non-dispatch is demonstrated. A correction before demonstrated
    non-dispatch may use at most one recovery within the original attempt
    deadline; counters survive transitions and restart.

Every test here is offline: a directory and the pure state machine, no browser,
no network. The three ambiguity mutants are modelled the way they actually
occur — the process DIES (crash), the page goes away mid-window (disconnect), or
the send POST is answered 403 AFTER being invoked — and each asserts the same
thing: the next actor may not press Enter again.
"""

from __future__ import annotations

import json

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
    SEND_INTENT_FILENAME,
    IntentState,
    SendIntentLog,
    SendIntentViolation,
    resolve_after_restart,
)

pytestmark = pytest.mark.unit


def _open(tmp_path, **kw):
    log = SendIntentLog(tmp_path / "attempt")
    log.open_attempt(
        run_id=kw.pop("run_id", "run-1"),
        attempt_id=kw.pop("attempt_id", "att-1"),
        prompt_sha=kw.pop("prompt_sha", "a" * 64),
        **kw,
    )
    return log


# ==========================================================================
# Durability — the record exists, on stable storage, BEFORE the dispatch
# ==========================================================================


def test_intent_is_on_disk_before_any_dispatch(tmp_path):
    """Owed-code row 1: the record is written by ``open_attempt``, i.e. before the
    composer dispatch, not as a side effect of it."""
    log = _open(tmp_path)
    path = tmp_path / "attempt" / SEND_INTENT_FILENAME
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["state"] == IntentState.PENDING.value
    assert on_disk["submits_dispatched"] == 0
    assert on_disk["sends_observed"] == 0


def test_dispatch_is_persisted_before_the_call_returns(tmp_path):
    """The state flip is durable at return, so a crash on the NEXT instruction
    still finds DISPATCHED on disk. This ordering is the whole point: an intent
    written after the keypress would leave the same ambiguity as no intent."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    on_disk = json.loads((tmp_path / "attempt" / SEND_INTENT_FILENAME).read_text())
    assert on_disk["state"] == IntentState.DISPATCHED.value
    assert on_disk["submits_dispatched"] == 1


def test_counters_survive_a_restart(tmp_path):
    """D7/D16: "counters survive transitions and restart". A brand-new log object
    over the same directory — the restarted process — sees them all."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.record_mint()
    log.record_mint()
    log.record_re_export()
    log.record_send_observed()

    reopened = SendIntentLog(tmp_path / "attempt")
    reopened.load()
    assert reopened.counters() == {
        "submits_dispatched": 1,
        "sends_observed": 1,
        "recoveries_used": 0,
        "mints": 2,
        "re_exports": 1,
    }


# ==========================================================================
# The three ambiguity mutants — no repeated submit after ambiguity
# ==========================================================================


def test_mutant_crash_after_intent_never_resubmits(tmp_path):
    """CRASH mutant. The process dies after dispatching. A restarted runner
    re-reads the attempt and is told it may NOT dispatch.

    D7/D16: "A crash after intent persistence is uncertain unless non-dispatch is
    demonstrated" — uncertain means read, never resend.
    """
    log = _open(tmp_path)
    log.record_submit_dispatch()
    del log  # the process is gone; only the file survives

    record, may_dispatch = resolve_after_restart(tmp_path / "attempt")
    assert record is not None
    assert record.state == IntentState.DISPATCHED.value
    assert may_dispatch is False

    revived = SendIntentLog(tmp_path / "attempt")
    revived.load()
    with pytest.raises(SendIntentViolation, match="NEVER repeated"):
        revived.record_submit_dispatch()


def test_crash_BEFORE_intent_is_a_safe_fresh_attempt(tmp_path):
    """The other half of the crash mutant, without which the test above would
    pass against a guard that simply refuses everything: a crash with no intent
    on disk means nothing was dispatched, so a fresh attempt IS permitted."""
    record, may_dispatch = resolve_after_restart(tmp_path / "never-opened")
    assert record is None
    assert may_dispatch is True


def test_mutant_disconnect_mid_window_never_resubmits(tmp_path):
    """DISCONNECT mutant. The page/socket goes away after Enter and before any
    correlated evidence. The observation is MISSING, not negative, so the state
    stays DISPATCHED and a second submit is refused."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    # A disconnect yields no send observation at all — nothing is recorded.
    assert log.may_dispatch_submit() is False
    with pytest.raises(SendIntentViolation, match="NEVER repeated"):
        log.record_submit_dispatch()
    assert log.record.sends_observed == 0


def test_mutant_invoked_error_403_never_resubmits(tmp_path):
    """INVOKED-ERROR mutant. The send POST was INVOKED and answered 403 (or 401).

    An error response is not evidence of non-dispatch: the app may have sent
    before the failure surfaced. D7/D16 names 401 and 403 explicitly among the
    outcomes that must NOT be followed by a repeated submit.
    """
    log = _open(tmp_path)
    log.record_submit_dispatch()
    with pytest.raises(SendIntentViolation, match="401, 403"):
        log.record_submit_dispatch()


@pytest.mark.parametrize("ambiguity", ["crash", "disconnect", "invoked_error_403", "timeout"])
def test_no_ambiguity_permits_a_second_submit(tmp_path, ambiguity):
    """One assertion across every ambiguity class D7/D16 names, so a future edit
    that carves out an exception for one of them fails here."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    assert log.may_dispatch_submit() is False, f"{ambiguity} must not re-open dispatch"


# ==========================================================================
# The recovery budget — one, within the deadline, never reset
# ==========================================================================


def test_demonstrated_non_dispatch_permits_exactly_one_recovery(tmp_path):
    """D7/D16: "a correction before demonstrated non-dispatch may use at most one
    recovery". The browser evidence is the composer still holding the prompt."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.demonstrate_non_dispatch("composer still holds 812 chars after Enter")
    assert log.may_dispatch_submit() is True
    log.record_submit_dispatch()
    assert log.record.submits_dispatched == 2
    assert log.record.recoveries_used == 1


def test_mutant_a_recovery_never_resets_the_budget(tmp_path):
    """The AC-20 "a recovery resets its budget" mutant. The SECOND recovery is
    refused, whatever the evidence."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.demonstrate_non_dispatch("composer still holds the prompt")
    log.record_submit_dispatch()
    with pytest.raises(SendIntentViolation, match="recovery budget exhausted"):
        log.demonstrate_non_dispatch("composer still holds the prompt")


def test_recovery_survives_a_restart_and_is_still_spent(tmp_path):
    """The budget is a persisted counter, so a restart cannot launder it — the
    obvious way an implementation would accidentally "reset" it."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.demonstrate_non_dispatch("composer still holds the prompt")
    log.record_submit_dispatch()

    restarted = SendIntentLog(tmp_path / "attempt")
    restarted.load()
    assert restarted.record.recoveries_used == 1
    with pytest.raises(SendIntentViolation, match="recovery budget exhausted"):
        restarted.demonstrate_non_dispatch("composer still holds the prompt")


def test_recovery_is_refused_past_the_original_attempt_deadline(tmp_path):
    """ "within the original attempt deadline" — a recovery attempted after it is
    refused even though the budget is untouched."""
    log = SendIntentLog(tmp_path / "attempt")
    log.open_attempt(
        run_id="run-1",
        attempt_id="att-1",
        prompt_sha="b" * 64,
        deadline_at=0.0,  # already past
    )
    log.record_submit_dispatch()
    with pytest.raises(SendIntentViolation, match="past its deadline"):
        log.demonstrate_non_dispatch("composer still holds the prompt")


def test_non_dispatch_cannot_be_demonstrated_after_acceptance(tmp_path):
    """Once a send is OBSERVED, no evidence re-opens dispatch."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.record_send_observed()
    with pytest.raises(SendIntentViolation, match="cannot be demonstrated after acceptance"):
        log.demonstrate_non_dispatch("composer looks empty")


# ==========================================================================
# AC-20 counters — the NB-6 obligation moved out of AC-18
# ==========================================================================


def test_acceptance_passes_when_submits_and_sends_agree(tmp_path):
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.record_send_observed()
    assert log.acceptance_failure() is None


def test_acceptance_fails_on_a_hidden_duplicate_submission(tmp_path):
    """The case NB-6 names: submit actions and observed sends DISAGREE."""
    log = _open(tmp_path)
    log.record_submit_dispatch()
    log.demonstrate_non_dispatch("composer still holds the prompt")
    log.record_submit_dispatch()  # two submit actions
    log.record_send_observed()  # one observed send
    failure = log.acceptance_failure()
    assert failure is not None
    assert "counts disagree" in failure
    assert "submits_dispatched=2" in failure and "sends_observed=1" in failure


def test_acceptance_fails_when_either_count_is_absent(tmp_path):
    """ "or when either is absent" — the absence is a failure, not a pass."""
    log = _open(tmp_path)
    assert "no submit action was recorded" in (log.acceptance_failure() or "")
    log.record_submit_dispatch()
    assert "no send was observed" in (log.acceptance_failure() or "")


def test_a_send_observed_with_no_dispatch_is_refused(tmp_path):
    """The counters must describe what happened: an observation with no recorded
    dispatch means the record has lost custody, so it raises rather than
    silently recording a send the log never authorised."""
    log = _open(tmp_path)
    with pytest.raises(SendIntentViolation, match="no recorded dispatch"):
        log.record_send_observed()


# ==========================================================================
# Fail-closed reads
# ==========================================================================


def test_a_corrupt_record_is_unresolvable_not_a_fresh_attempt(tmp_path):
    """Fail closed: unreadable custody must never degrade to "no intent, so send"."""
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    (attempt / SEND_INTENT_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(SendIntentViolation, match="UNRESOLVABLE"):
        resolve_after_restart(attempt)


def test_a_future_schema_version_is_unresolvable(tmp_path):
    """A record written by a newer runner is not interpreted with today's rules."""
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    (attempt / SEND_INTENT_FILENAME).write_text(
        json.dumps({"schema_version": 99, "run_id": "r", "attempt_id": "a", "prompt_sha": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(SendIntentViolation, match="UNRESOLVABLE"):
        resolve_after_restart(attempt)
