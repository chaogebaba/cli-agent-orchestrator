"""F721 #577: the ready-backlog watchdog is a real retry owner, and the silent
delivery/reconciliation paths name themselves in the journal.

Incident (2026-09-02, 07:00-07:27Z): four idle cline terminals held queued inbox
rows with status=completed and "no open delivery attempt". The ready-backlog
watchdog only notified ("Reconciliation remains the retry owner"), the
reconciliation daemon swallowed every fault at DEBUG, and ``deliver_pending``
returned silently from three gates. Only a server restart cleared it.

These tests pinned three halves of the hot fix. TWO of them survive:
  2. the reconciliation daemon logs a swallowed fault at WARNING with the
     receiver id and the message ids it was owed;
  3. each silent early return in ``deliver_pending`` emits one structured line
     naming its reason.

Half 1 -- "the watchdog kicks ``deliver_pending`` once per stalled terminal and
still notifies" -- is gone with WP-ARCH 3c K4, which deletes
``tick_ready_backlog`` along with the other four muted ticks and the notice
machinery they fed. See the note where its two arms stood.

Note that the incident's own reading survives the deletion: the ready-backlog
watchdog was never the retry OWNER (its own alert said so), and 3c answers the
stranded-row class at the source instead, by having the delivery tick adopt
orphaned PENDING rows on a schedule. The kick was a second retry path bolted to
a notifier; halves 2 and 3, which are about faults NAMING themselves rather than
dying silently, are what the incident actually turned on and they are untouched.
"""

import ast
import inspect
import logging
import threading
from unittest.mock import patch

from cli_agent_orchestrator.services.inbox_service import InboxService

# ---------------------------------------------------------------------------
# Half 1 -- REMOVED by WP-ARCH 3c K4.
#
# ``test_f721_watchdog_kicks_delivery_once_and_still_notifies`` asserted that the
# ready-backlog fire path called ``inbox_service.deliver_pending`` exactly once
# for the stalled receiver and still composed its alert (sender
# ``watchdog:<receiver>``, the "reconciliation remains the retry owner" line, and
# a ``cao messages trace <id>`` pointer). Its sibling,
# ``test_f721_watchdog_kick_failure_never_starves_the_notification``, asserted
# that a raising ``deliver_pending`` was reported INSIDE that alert
# ("watchdog delivery re-attempt failed (RuntimeError)") rather than escaping
# into the tick.
#
# Both drove ``StalledCallbackWatchdog.tick_ready_backlog``, which K4 deletes with
# the other four muted ticks, together with ``collect_due_notifications`` and the
# ``_push_notice`` machinery the alert half went through. There is no seam left to
# re-point at: the kick had one caller and the alert had one composer, and the
# slice removes both. Re-pointing the deliver-once half at ``deliver_pending``
# directly would assert that a mock called once was called once -- the kick was
# the subject, not the callee.
#
# The concern the kick existed to serve -- a PENDING row with no open attempt
# sitting behind an idle terminal -- is now owned by the delivery tick's adoption
# pass (DIAG-LEGACY-ROW-ADOPTED), whose arms live in
# ``test/app/delivery/test_adoption.py``. The three helpers that stood here
# (``_backlog_observation``, ``_boundary_observation``, ``_fire_ready_backlog``)
# existed only to drive the deleted tick and go with it; the surviving arms below
# build their own fixtures.
# ---------------------------------------------------------------------------


def test_f721_reconcile_daemon_logs_swallowed_fault_at_warning(caplog):
    """An injected deliver_pending fault surfaces at WARNING with receiver + rows."""
    service = InboxService()

    class _Row:
        def __init__(self, mid):
            self.id = mid

    with (
        patch.object(service, "reconcile_pending_orphans"),
        patch.object(service, "surface_stalled_direct_deliveries"),
        patch.object(service, "recover_stale_deliveries"),
        # WP-ARCH 3c K2 deleted ``reconcile_pull_mode_notifications`` (the legacy
        # pull-mode reconciler), so it is no longer among the sweeps that have to
        # be silenced to isolate the fault path. The arm's subject — a swallowed
        # ``deliver_pending`` fault surfacing at WARNING with receiver and row ids
        # — is unchanged.
        patch.object(service, "deliver_pending", side_effect=RuntimeError("wedged")),
        patch(
            "cli_agent_orchestrator.services.inbox_service." "list_pending_receiver_ids_older_than",
            return_value=["cline_general-68443474"],
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
            return_value=[_Row(3959), _Row(3960)],
        ),
        patch(
            "cli_agent_orchestrator.clients.database.list_expired_pending_rows",
            return_value=[],
        ),
        caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.services.inbox_service"),
    ):
        try:
            service.reconcile_orphaned_messages()
        except Exception:
            # Later sweeps in this method are out of scope; the loop already ran.
            pass

    records = [r for r in caplog.records if r.getMessage().startswith("inbox_reconcile_failed")]
    assert records, "reconciliation fault was not logged"
    assert records[0].levelno == logging.WARNING
    assert "receiver=cline_general-68443474" in records[0].getMessage()
    assert "message_ids=3959,3960" in records[0].getMessage()


def test_f721_lock_miss_early_return_names_itself(caplog):
    """The non-blocking delivery-lock miss emits its structured reason line."""
    service = InboxService()
    held = threading.Lock()
    held.acquire()

    with (
        patch.object(service, "_f339_is_abandoned", return_value=False),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
            return_value=held,
        ),
        caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.services.inbox_service"),
    ):
        service.deliver_pending("cline_general-68443474")

    assert any(
        r.getMessage() == "deliver_pending_skip terminal=cline_general-68443474 reason=lock_miss"
        for r in caplog.records
    )


def test_f721_log_delivery_skip_line_is_structured(caplog):
    """One line, one reason, greppable from the journal."""
    service = InboxService()
    with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.services.inbox_service"):
        service._log_delivery_skip("t1", "probe_status=processing")

    lines = [r for r in caplog.records if r.getMessage().startswith("deliver_pending_skip")]
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    assert (
        lines[0].getMessage() == "deliver_pending_skip terminal=t1 reason=probe_status=processing"
    )


def test_f721_recovery_state_gate_names_itself(caplog):
    """The eligibility skip a non-None recovery_state causes is no longer silent.

    This is the shape the incident actually had: the delivery loop was alive and
    calling deliver_pending, and the row was skipped before any attempt opened.
    """
    service = InboxService()

    with (
        patch.object(service, "_f339_is_abandoned", return_value=False),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
            return_value=threading.Lock(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
            return_value={"provider": "cline_cli", "recovery_state": "rebinding"},
        ),
        caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.services.inbox_service"),
    ):
        service.deliver_pending("cline_general-68443474")

    assert any(
        r.getMessage()
        == "deliver_pending_skip terminal=cline_general-68443474 reason=recovery_state=rebinding"
        for r in caplog.records
    )


def test_f721_abandoned_ghost_gate_names_itself(caplog):
    """The very first gate in the method names itself too."""
    service = InboxService()

    with (
        patch.object(service, "_f339_is_abandoned", return_value=True),
        caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.services.inbox_service"),
    ):
        service.deliver_pending("cline_general-68443474")

    assert any(
        r.getMessage()
        == "deliver_pending_skip terminal=cline_general-68443474 reason=f339_abandoned"
        for r in caplog.records
    )


def _skip_reasons_in_deliver_pending():
    """Collect the reason argument of every _log_delivery_skip call in the method.

    The deeper admission and probe gates sit behind live terminal state a unit
    test cannot reach without standing up a terminal, so their instrumentation is
    pinned structurally: deleting any reason line fails this test.
    """
    source = inspect.getsource(InboxService.deliver_pending)
    tree = ast.parse(ast.unparse(ast.parse(source.lstrip())))
    reasons = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "_log_delivery_skip"):
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant):
            reasons.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            head = arg.values[0]
            reasons.append(head.value if isinstance(head, ast.Constant) else "")
    return reasons


#: Every gate between deliver_pending's entry and the point where a delivery
#: attempt is opened. The incident's rows were skipped somewhere in this region
#: -- the journal proves the loop was alive (supervisor-bound rows reached 126
#: and 130 attempts at 07:13:40Z and 07:18:18Z) while the four cline rows opened
#: zero attempts. Any one of these could have been the wedge, and none of them
#: said so.
EXPECTED_SKIP_REASONS = {
    "f339_abandoned",
    "abandoned_no_terminal",
    "no_terminal_metadata",
    "lock_miss",
    "recovery_state=",
    "db_locked",
    "native_probe_none_preadmission",
    "probe_evidence_none_preadmission",
    "wake_superseded",
    "gate_stop",
    "admission_status_unready",
    "boundary_observation_error",
    "snapshot_none",
    "attempt_already_delivering",
    "native_probe_none_preopen",
    "probe_evidence_none_preopen",
    "probe_status=",
}


def test_f721_every_pre_attempt_gate_names_a_reason():
    reasons = set(_skip_reasons_in_deliver_pending())
    missing = EXPECTED_SKIP_REASONS - reasons
    assert not missing, f"pre-attempt gates lost their reason line: {sorted(missing)}"
