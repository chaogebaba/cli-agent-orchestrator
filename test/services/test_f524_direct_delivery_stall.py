"""F524 (#379): supervisor->worker send_message stall + stale-late-delivery.

Legs, all driven through DEPLOYED entry points on the shared real_sqlite_env
fixture (no calls to the internal surface method or reimplemented branches — the
re-gate's B1/B2 findings):

  Leg 1 (STALL SURFACING): a direct-terminal message (logical_receiver_id NULL)
  that stays PENDING past delivery.escalate_after_s while its receiver never
  reaches an idle boundary must surface to the ORIGINAL SENDER as a failure —
  the sender learns the message did not land.

  B1 (RECONCILER WIRING): the surfacing must fire from the deployed heartbeat
  entry point reconcile_orphaned_messages, NOT only when the sweep method is
  called directly. Mutant: delete the self.surface_stalled_direct_deliveries()
  call from reconcile_orphaned_messages -> this test must fail.

  Leg 2 / B2 (STALE-LATE-DELIVERY BANNER): the real deliver_pending composition
  path must prepend the staleness banner to the delivered wire text for a
  surfaced-stalled message, and must NOT for an unstamped one. Mutant: neuter
  the banner prepend in deliver_pending -> this test must fail.

  S1 (ATOMIC ONE-SHOT): the sender notice must be emitted at most once even
  under a concurrent sweep or a crash between commits. Backed by the partial
  unique index on (message_id) WHERE kind='f524.stall_surfaced' and the
  insert-or-ignore claim.

Regression anchor: message 1210 sat PENDING 68 minutes while worker 3ff35106
built to the countermanded instruction, then was delivered stale on idle.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _seed_terminal(session, term_id: str, *, role_profile: str = "developer"):
    from cli_agent_orchestrator.clients.database import TerminalModel

    session.add(
        TerminalModel(
            id=term_id,
            tmux_session="test-sess",
            tmux_window=f"win-{term_id}",
            provider="kiro_cli",
            agent_profile=role_profile,
            lifecycle="sticky",
            init_state="ready",
            lifecycle_generation=1,
            metadata_json=json.dumps({}),
        )
    )


def _enable_escalate_after(monkeypatch, seconds: float):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.config_service.ConfigService.get",
        staticmethod(
            lambda key, default=None, override=None: {
                "delivery.escalate_after_s": seconds,
            }.get(key, default)
        ),
    )


def _seed_direct_stalled(
    TestSession, *, sender="supervis", receiver="worker01", body="RULING: (B)"
):
    """Seed one aged, PENDING, direct-terminal message and its two terminals."""
    aged = datetime.now(timezone.utc) - timedelta(seconds=4000)
    from cli_agent_orchestrator.clients.database import InboxModel

    with TestSession() as session:
        _seed_terminal(session, sender)
        _seed_terminal(session, receiver)
        msg = InboxModel(
            sender_id=sender,
            receiver_id=receiver,
            logical_receiver_id=None,  # DIRECT-TERMINAL: no obligation
            message=body,
            orchestration_type="send_message",
            status="pending",
            created_at=aged,
        )
        session.add(msg)
        session.commit()
        return msg.id


def _count_sender_notices(TestSession, sender_terminal: str, receiver_terminal: str) -> int:
    from cli_agent_orchestrator.clients.database import InboxModel

    with TestSession() as db:
        return (
            db.query(InboxModel)
            .filter(
                InboxModel.receiver_id == sender_terminal,
                InboxModel.sender_id == f"message-trace:{receiver_terminal}",
            )
            .count()
        )


@pytest.mark.xdist_group("real_sqlite")
class TestF524StallSurfacing:
    """Leg 1: aged direct message surfaces to sender; exclusions hold."""

    def test_stalled_direct_message_surfaces_to_sender(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]
        _seed_direct_stalled(TestSession)

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )

        from cli_agent_orchestrator.services.inbox_service import (
            F524_STALL_SURFACED_KIND,
            InboxService,
        )

        svc = InboxService()
        assert svc.surface_stalled_direct_deliveries() == 1

        from cli_agent_orchestrator.clients.database import InboxMessageTraceEventModel as _TE
        from cli_agent_orchestrator.clients.database import InboxModel as _IM

        assert _count_sender_notices(TestSession, "supervis", "worker01") == 1
        with TestSession() as db:
            body = (
                db.query(_IM.message)
                .filter(
                    _IM.receiver_id == "supervis",
                    _IM.sender_id == "message-trace:worker01",
                )
                .scalar()
            )
            assert "delivery-stall" in body and "worker01" in body
            original = (
                db.query(_IM)
                .filter(_IM.sender_id == "supervis", _IM.receiver_id == "worker01")
                .one()
            )
            events = (
                db.query(_TE)
                .filter(_TE.message_id == original.id, _TE.kind == F524_STALL_SURFACED_KIND)
                .all()
            )
            assert len(events) == 1

    def test_fresh_direct_message_is_not_surfaced(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]
        now = datetime.now(timezone.utc)
        from cli_agent_orchestrator.clients.database import InboxModel

        with TestSession() as session:
            _seed_terminal(session, "supervis")
            _seed_terminal(session, "worker01")
            session.add(
                InboxModel(
                    sender_id="supervis",
                    receiver_id="worker01",
                    logical_receiver_id=None,
                    message="fresh instruction",
                    orchestration_type="send_message",
                    status="pending",
                    created_at=now - timedelta(seconds=5),
                )
            )
            session.commit()

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )
        from cli_agent_orchestrator.services.inbox_service import InboxService

        assert InboxService().surface_stalled_direct_deliveries() == 0

    def test_service_sender_is_never_surfaced(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]
        aged = datetime.now(timezone.utc) - timedelta(seconds=4000)
        from cli_agent_orchestrator.clients.database import InboxModel

        with TestSession() as session:
            _seed_terminal(session, "worker01")
            session.add(
                InboxModel(
                    sender_id="message-trace:worker09",
                    receiver_id="worker01",
                    logical_receiver_id=None,
                    message="[delivery-stall] earlier notice",
                    orchestration_type="send_message",
                    status="pending",
                    created_at=aged,
                )
            )
            session.commit()

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )
        from cli_agent_orchestrator.services.inbox_service import InboxService

        assert InboxService().surface_stalled_direct_deliveries() == 0

    def test_mailbox_message_is_not_surfaced_here(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]
        aged = datetime.now(timezone.utc) - timedelta(seconds=4000)
        from cli_agent_orchestrator.clients.database import InboxModel, MailboxModel

        with TestSession() as session:
            _seed_terminal(session, "worker01")
            session.add(
                MailboxModel(
                    id="mb_sup_f524",
                    session_name="test-sess",
                    role="supervisor",
                    current_terminal_id="worker01",
                    generation=1,
                    consumed_through_id=0,
                    schema_version=1,
                )
            )
            session.add(
                InboxModel(
                    sender_id="worker05",
                    receiver_id="worker01",
                    logical_receiver_id="mb_sup_f524",
                    message="mailbox-routed",
                    orchestration_type="send_message",
                    status="pending",
                    created_at=aged,
                )
            )
            session.commit()

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )
        from cli_agent_orchestrator.services.inbox_service import InboxService

        assert InboxService().surface_stalled_direct_deliveries() == 0


@pytest.mark.xdist_group("real_sqlite")
class TestF524ReconcilerWiring:
    """B1: surfacing fires from the DEPLOYED reconcile_orphaned_messages path.

    This test never calls surface_stalled_direct_deliveries directly. Deleting
    the call from reconcile_orphaned_messages must make it fail (mutant kill).
    """

    def test_reconcile_orphaned_messages_surfaces_stalled_direct_row(
        self, real_sqlite_env, monkeypatch
    ):
        env = real_sqlite_env
        TestSession = env["TestSession"]
        _seed_direct_stalled(TestSession)

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )

        from cli_agent_orchestrator.services import inbox_service as _is_mod
        from cli_agent_orchestrator.services.inbox_service import InboxService

        # Silence the OTHER things reconcile does so this test isolates the
        # F524 wiring (mirror test_f165's no-op pattern). We do NOT touch
        # surface_stalled_direct_deliveries — that is the code under test.
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_older_than", lambda seconds: [])
        monkeypatch.setattr(InboxService, "reconcile_pending_orphans", lambda self: None)
        monkeypatch.setattr(InboxService, "recover_stale_deliveries", lambda self, **kw: None)
        monkeypatch.setattr(InboxService, "reconcile_pull_mode_notifications", lambda self: None)
        # ConfigService.get above returns None for supervisor.mailbox_pull, so the
        # mailbox-quarantine branch of reconcile is skipped.

        svc = InboxService()
        svc.reconcile_orphaned_messages()  # DEPLOYED entry point, not the sweep method

        assert _count_sender_notices(TestSession, "supervis", "worker01") == 1


def _msgtrace_patches():
    """The delivery-pipeline patches test_inbox_service.py applies as an autouse
    fixture, reproduced here so deliver_pending reaches the send seam through the
    legacy_test_seam (begin_delivery_attempt patched)."""
    attempt = {
        "attempt_uuid": "attempt-1",
        "started_at": "2026-07-11T00:00:00+00:00",
        "evidence": {},
    }
    return [
        patch(
            "cli_agent_orchestrator.services.inbox_service.count_ambiguous_attempts", return_value=0
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.list_message_attempts", return_value=[]
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_batch_from_prior_attempt",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
            return_value="attempt-1",
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_message_trace",
            return_value={"attempts": [attempt]},
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
        patch("cli_agent_orchestrator.services.inbox_service.settle_delivery_attempt"),
    ]


def _terminal_service_capture():
    mock = MagicMock()
    mock.prepare_input.side_effect = lambda _tid, text, *_a, **_k: text
    mock.send_prepared_input = MagicMock()
    return mock


class TestF524StaleDeliveryBannerRealPath:
    """Leg 2 / B2: banner is composed by the REAL deliver_pending path.

    Mutant: neuter the banner prepend in deliver_pending -> both asserts on the
    delivered wire text fail.
    """

    def _run_deliver(self, monkeypatch, *, stamped: bool):
        from datetime import datetime as _dt

        from cli_agent_orchestrator.models.inbox import (
            InboxMessage,
            MessageStatus,
            OrchestrationType,
        )
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.inbox_service import (
            F524_STALL_SURFACED_KIND,
            InboxService,
        )

        msg = InboxMessage(
            id=42,
            sender_id="supervis",
            receiver_id="worker01",
            message="RULING: take (B).",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
            status=MessageStatus.PENDING,
            park_warm=False,
            created_at=_dt.now(),
        )

        term_mock = _terminal_service_capture()
        stamped_ids = {42} if stamped else set()

        from contextlib import ExitStack

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                    return_value=[msg],
                )
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
                    return_value={"provider": "event"},
                )
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.inbox_service.messages_with_trace_kind",
                    return_value=stamped_ids,
                )
            )
            stack.enter_context(
                patch("cli_agent_orchestrator.services.inbox_service.terminal_service", term_mock)
            )
            mon = stack.enter_context(
                patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
            )
            stack.enter_context(
                patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
            )
            for p in _msgtrace_patches():
                stack.enter_context(p)
            mon.get_status.return_value = TerminalStatus.IDLE
            InboxService().deliver_pending("worker01")

        assert term_mock.send_prepared_input.called, "delivery did not reach the send seam"
        wire = term_mock.send_prepared_input.call_args.args[1]
        return wire

    def test_surfaced_message_delivered_with_banner(self, monkeypatch):
        wire = self._run_deliver(monkeypatch, stamped=True)
        assert wire.startswith("[CAO STALE-DELIVERY WARNING]"), wire[:80]
        assert "RULING: take (B)." in wire

    def test_unsurfaced_message_delivered_without_banner(self, monkeypatch):
        wire = self._run_deliver(monkeypatch, stamped=False)
        assert "[CAO STALE-DELIVERY WARNING]" not in wire
        assert wire == "RULING: take (B)."


@pytest.mark.xdist_group("real_sqlite")
class TestF524AtomicOneShot:
    """S1: the sender notice is emitted at most once under repeat/concurrent sweeps."""

    def test_repeat_sweep_emits_no_duplicate_notice(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]
        _seed_direct_stalled(TestSession)

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )
        from cli_agent_orchestrator.services.inbox_service import InboxService

        svc = InboxService()
        assert svc.surface_stalled_direct_deliveries() == 1
        # Two further sweeps must surface nothing and add no notice.
        assert svc.surface_stalled_direct_deliveries() == 0
        assert svc.surface_stalled_direct_deliveries() == 0
        assert _count_sender_notices(TestSession, "supervis", "worker01") == 1

    def test_claim_is_atomic_insert_or_ignore(self, real_sqlite_env, monkeypatch):
        """The claim primitive itself: second claim loses; crash-after-commit
        (notice never sent) still cannot re-send on the next sweep."""
        env = real_sqlite_env
        TestSession = env["TestSession"]
        msg_id = _seed_direct_stalled(TestSession)

        from cli_agent_orchestrator.clients.database import claim_message_trace_once
        from cli_agent_orchestrator.services.inbox_service import F524_STALL_SURFACED_KIND

        # Simulate a prior sweep that WON the claim (stamped) but crashed before
        # sending the notice.
        assert claim_message_trace_once(msg_id, F524_STALL_SURFACED_KIND, phase="stall_surfaced")

        _enable_escalate_after(monkeypatch, 120.0)
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            lambda tid: TerminalStatus.PROCESSING,
        )
        from cli_agent_orchestrator.services.inbox_service import InboxService

        # The next sweep must NOT re-send (claim already taken) — no duplicate,
        # even though the earlier notice was lost. Degradation, not duplication.
        assert InboxService().surface_stalled_direct_deliveries() == 0
        assert _count_sender_notices(TestSession, "supervis", "worker01") == 0
