"""F721 #577: the ready-backlog watchdog is a real retry owner, and the silent
delivery/reconciliation paths name themselves in the journal.

Incident (2026-09-02, 07:00-07:27Z): four idle cline terminals held queued inbox
rows with status=completed and "no open delivery attempt". The ready-backlog
watchdog only notified ("Reconciliation remains the retry owner"), the
reconciliation daemon swallowed every fault at DEBUG, and ``deliver_pending``
returned silently from three gates. Only a server restart cleared it.

These tests pin the three halves of the hot fix:
  1. the watchdog kicks ``deliver_pending`` once per stalled terminal and still
     notifies;
  2. the reconciliation daemon logs a swallowed fault at WARNING with the
     receiver id and the message ids it was owed;
  3. each silent early return in ``deliver_pending`` emits one structured line
     naming its reason.
"""

import ast
import inspect
import logging
import threading
from datetime import datetime
from unittest.mock import patch

from cli_agent_orchestrator.clients.database import ReadyBacklogObservation
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


def _backlog_observation(fingerprint=(0, None, None, datetime(2030, 1, 1))):
    return ReadyBacklogObservation(
        receiver_id="receiver",
        oldest_message_id=17,
        oldest_pending_age_seconds=100,
        has_open_delivering_attempt=False,
        attempt_fingerprint=fingerprint,
    )


def _boundary_observation(status):
    return BoundaryObservation(
        observation_epoch="epoch",
        status=status,
        status_gen=0,
        input_gen=0,
        seq=0,
        last_non_ready_seq=None,
        last_ready_seq=None,
    )


def _fire_ready_backlog(service, deliver_side_effect=None):
    """Drive tick_ready_backlog past its grace so the fire path runs once."""
    observation = _backlog_observation()
    metadata = {"caller_id": "caller", "agent_profile": "cline_general"}
    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "list_ready_backlog_observations",
            return_value=[observation],
        ),
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            return_value=TerminalStatus.COMPLETED,
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor."
            "get_boundary_observation",
            return_value=_boundary_observation(TerminalStatus.COMPLETED),
        ),
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "CAO_WAITING_INBOX_GRACE_SECONDS",
            10,
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"
        ) as create,
        patch(
            "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
            side_effect=deliver_side_effect,
        ) as deliver,
    ):
        service.tick_ready_backlog(now=100.0)
        service.tick_ready_backlog(now=109.0)
        service.tick_ready_backlog(now=110.0)
        service.tick_ready_backlog(now=120.0)
    return create, deliver


def test_f721_watchdog_kicks_delivery_once_and_still_notifies():
    """The fire path re-attempts delivery exactly once, then alerts as before."""
    service = StalledCallbackWatchdog()
    create, deliver = _fire_ready_backlog(service)

    deliver.assert_called_once()
    assert deliver.call_args.args[0] == "receiver"

    create.assert_called_once()
    sender, receiver, message = create.call_args.args
    assert (sender, receiver) == ("watchdog:receiver", "caller")
    assert "watchdog re-attempted delivery" in message
    assert "reconciliation remains the retry owner" in message
    assert "cao messages trace 17" in message


def test_f721_watchdog_kick_failure_never_starves_the_notification():
    """A delivery fault is reported in the alert, not raised into the tick."""
    service = StalledCallbackWatchdog()
    create, deliver = _fire_ready_backlog(service, deliver_side_effect=RuntimeError("boom"))

    deliver.assert_called_once()
    create.assert_called_once()
    _, _, message = create.call_args.args
    assert "watchdog delivery re-attempt failed (RuntimeError)" in message


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
        patch.object(service, "reconcile_pull_mode_notifications"),
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
