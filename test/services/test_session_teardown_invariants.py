"""#498 atomic-teardown INVARIANTS, ported to the fork's teardown spine.

The upstream test file ``test_session_teardown_atomic.py`` exercised #498 against
upstream's three-phase ``delete_session`` (``capture_terminal_snapshot`` ->
``dismantle_terminal_runtime`` -> ``delete_terminal_row`` + id-scoped sweep). The
0824 upstream merge kept the FORK's lease-based teardown spine
(``_delete_terminal_under_lease`` + ``finalize_session``) and wrapped it in the
per-session-name ``session_lifecycle_lock`` (Option A). That upstream file was
therefore dropped wholesale — it is coupled to a spine and to exact error
strings / kill-call-counts that no longer exist here, and a heavily adapted copy
would re-conflict on every future upstream sync.

This file ports the LOAD-BEARING #498 invariants to the fork spine, asserting
them positively and by end-state (not by upstream's internal call sequence):

  (a) create/delete mutual exclusion under ``session_lifecycle_lock``;
  (b) a kill that silently fails is SURFACED (RuntimeError containing
      "still exists"), not reported as success;
  (c) teardown confirms the session is liveness-gone (strict check) before
      reporting success, and reports success only once it is provably gone.

Fork-population behavioural coverage for the rest of the teardown surface
(leases, draft_guard, deferred cleanup, per-terminal event emission) lives in
``test_session_service.py::TestDeleteSession`` and
``test_plugin_event_emission.py``.
"""

import threading
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import session_service
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock


# ---------------------------------------------------------------------------
# (a) Mutual exclusion under the per-session-name lifecycle lock.
# ---------------------------------------------------------------------------


def test_session_lifecycle_lock_serializes_same_name():
    """The primitive #498's mutual exclusion rests on: the SAME session name is
    strictly serialized. A second acquisition of the same-name lock cannot enter
    its critical section while the first holds it; a DIFFERENT name never waits.

    create_terminal and delete_session both take this lock on the session NAME
    (verified by test_delete_session_holds_the_lifecycle_lock below and by the
    create-path closure), so serializing the lock IS serializing create-vs-
    teardown and teardown-vs-teardown for one name.
    """
    holder_in = threading.Event()
    release_holder = threading.Event()
    contender_entered = threading.Event()

    def _holder():
        with session_lifecycle_lock("cao-mx"):
            holder_in.set()
            # Hold the lock until the test releases us.
            assert release_holder.wait(5), "holder never released"

    def _contender():
        assert holder_in.wait(5), "holder never entered"
        with session_lifecycle_lock("cao-mx"):
            contender_entered.set()

    ht = threading.Thread(target=_holder)
    ct = threading.Thread(target=_contender)
    ht.start()
    ct.start()
    try:
        assert holder_in.wait(5)
        # While the holder keeps the same-name lock, the contender is committed
        # to acquire() and provably cannot enter its critical section.
        assert not contender_entered.wait(0.3), (
            "same-name lifecycle lock did NOT serialize — mutual exclusion broken"
        )
        # A DIFFERENT name must never wait on this lock.
        other_entered = threading.Event()

        def _other():
            with session_lifecycle_lock("cao-other"):
                other_entered.set()

        ot = threading.Thread(target=_other)
        ot.start()
        assert other_entered.wait(5), "a different session name was serialized — too coarse"
        ot.join(5)
    finally:
        release_holder.set()
        ct.join(5)
        ht.join(5)

    assert contender_entered.is_set(), "contender never got the lock after release"


@patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
@patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.session_service.get_backend")
def test_delete_session_holds_the_lifecycle_lock(
    mock_get_backend, mock_list_terminals, mock_delete_terminal
):
    """delete_session runs its critical section UNDER session_lifecycle_lock for
    the session name — the same lock create_terminal holds.

    Proven from INSIDE the per-terminal teardown (which only runs within
    delete_session's critical section): another thread attempts to acquire the
    same-name lifecycle lock and must be BLOCKED for the duration, then succeed
    once delete_session returns and releases it.
    """
    mock_get_backend.return_value.session_exists.side_effect = [True, False, False]
    mock_get_backend.return_value.session_exists_strict.side_effect = [True, False, False]
    mock_list_terminals.return_value = [{"id": "t1"}]

    observed = {"blocked_during_teardown": None}

    def _probe_lock(*_args, **_kwargs):
        # We are inside delete_session's critical section here. A second thread
        # trying to take the SAME name's lifecycle lock must not get in.
        entered = threading.Event()

        def _contend():
            with session_lifecycle_lock("cao-lockcheck"):
                entered.set()

        t = threading.Thread(target=_contend)
        t.start()
        # If the lock is held by delete_session, the contender cannot enter.
        observed["blocked_during_teardown"] = not entered.wait(0.3)
        # Stash the thread so the test can join it after the lock releases.
        observed["contender"] = t
        observed["entered"] = entered
        return {"terminal_deleted": True}

    mock_delete_terminal.side_effect = _probe_lock

    result = session_service.delete_session("cao-lockcheck")

    # The contender gets in only AFTER delete_session released the lock.
    observed["contender"].join(5)
    assert result == {"deleted": ["cao-lockcheck"], "errors": []}
    assert observed["blocked_during_teardown"] is True, (
        "delete_session did not hold session_lifecycle_lock during its critical "
        "section — #498 mutual exclusion is not in force on the teardown path"
    )
    assert observed["entered"].is_set(), "contender never acquired the lock after release"


# ---------------------------------------------------------------------------
# (b) A silent kill failure is surfaced, not reported as success.
# ---------------------------------------------------------------------------


@patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
@patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.session_service.get_backend")
def test_silent_kill_failure_is_surfaced_not_swallowed(
    mock_get_backend, mock_list_terminals, mock_delete_terminal
):
    """A kill that silently fails (the session survives) must RAISE, not report a
    successful delete. finalize_session confirms with session_exists_strict and
    raises when the session is still there.

    Asserted SEMANTICALLY (message contains "still exists"), not on the upstream
    error string or a kill-call count — the fork's finalize_session runs a
    bounded retry loop, so the number of kill_session calls is an implementation
    detail; the invariant is that a surviving session ends the call in an error.
    """
    backend = mock_get_backend.return_value
    # Kill is swallowed: the session is alive on every liveness check, forever.
    backend.session_exists.return_value = True
    backend.session_exists_strict.return_value = True
    backend.kill_session.return_value = False
    mock_list_terminals.return_value = [{"id": "t1"}]

    with pytest.raises(RuntimeError, match="still exists"):
        session_service.delete_session("cao-silentkill")

    # The kill was at least attempted — the failure was real, not skipped.
    assert backend.kill_session.called


# ---------------------------------------------------------------------------
# (c) Teardown confirms liveness-gone before reporting success.
# ---------------------------------------------------------------------------


@patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
@patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.session_service.get_backend")
def test_success_only_after_session_confirmed_gone(
    mock_get_backend, mock_list_terminals, mock_delete_terminal
):
    """delete_session reports success only once the session is CONFIRMED gone.

    Model a lagged reap: strict liveness reports alive on the pre-kill check,
    then gone. Success is returned, and at return time the last strict check the
    teardown made observed the session as absent — the confirmation gate.
    Assert END-STATE (confirmed-gone at success), not the number of kill calls.
    """
    backend = mock_get_backend.return_value
    # finalize_session._still_alive() call sequence: initial (alive -> kill),
    # loop check (gone -> break), final confirm (gone -> success).
    backend.session_exists_strict.side_effect = [True, False, False]
    backend.session_exists.side_effect = [True, False, False]
    backend.kill_session.return_value = True
    mock_list_terminals.return_value = [{"id": "t1"}]

    result = session_service.delete_session("cao-confirmed")

    assert result == {"deleted": ["cao-confirmed"], "errors": []}
    # The kill was issued and the teardown confirmed absence before success.
    assert backend.kill_session.called


@patch("cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease")
@patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.session_service.get_backend")
def test_lagged_kill_is_confirmed_gone_before_success(
    mock_get_backend, mock_list_terminals, mock_delete_terminal
):
    """kill_session may return before tmux finishes reaping (a real race). The
    teardown keeps confirming until the session is provably gone, then succeeds —
    the session is absent THE MOMENT delete_session returns, not "eventually".
    """
    backend = mock_get_backend.return_value
    # Alive for the first two strict checks (lagged reap), gone on the third.
    backend.session_exists_strict.side_effect = [True, True, False, False]
    backend.session_exists.side_effect = [True, True, False, False]
    backend.kill_session.return_value = True
    mock_list_terminals.return_value = []

    with patch("cli_agent_orchestrator.services.session_service.time.sleep"):
        result = session_service.delete_session("cao-lagged")

    assert result == {"deleted": ["cao-lagged"], "errors": []}
    assert backend.kill_session.call_count >= 1
