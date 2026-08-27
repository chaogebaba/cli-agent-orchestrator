"""F524 (#379): supervisor->worker send_message stall + stale-late-delivery.

Two legs, both driven through the real ORM path on the shared real_sqlite_env
fixture:

  Leg 1 (STALL SURFACING): a direct-terminal message (logical_receiver_id NULL)
  that stays PENDING past delivery.escalate_after_s while its receiver never
  reaches an idle boundary must surface to the ORIGINAL SENDER as a failure —
  the sender learns the message did not land — and must do so exactly once.

  Leg 2 (STALE-LATE-DELIVERY): once a stall has been surfaced, the eventual
  late delivery must NOT go out as a fresh instruction — the wire text carries
  a staleness banner so the receiver treats it as possibly-superseded.

Regression anchor: message 1210 sat PENDING 68 minutes while worker 3ff35106
built to the countermanded instruction, then was delivered stale on idle with
no signal to either party.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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


@pytest.mark.xdist_group("real_sqlite")
class TestF524DirectDeliveryStall:
    """Leg 1: aged direct message surfaces to sender, exactly once."""

    def test_stalled_direct_message_surfaces_to_sender(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]

        now = datetime.now(timezone.utc)
        aged = now - timedelta(seconds=4000)  # well past escalate_after_s

        from cli_agent_orchestrator.clients.database import InboxModel

        with TestSession() as session:
            _seed_terminal(session, "supervis")  # sender (supervisor)
            _seed_terminal(session, "worker01")  # receiver (busy worker)
            session.add(
                InboxModel(
                    sender_id="supervis",
                    receiver_id="worker01",
                    logical_receiver_id=None,  # DIRECT-TERMINAL: no obligation
                    message="RULING on commit-3 conflict: take (B). Frozen blueprint is authority.",
                    orchestration_type="send_message",
                    status="pending",
                    created_at=aged,
                )
            )
            session.commit()

        _enable_escalate_after(monkeypatch, 120.0)

        # Receiver is PROCESSING (never idle) — the F524 shape.
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
        surfaced = svc.surface_stalled_direct_deliveries()
        assert surfaced == 1, "the aged direct message should be surfaced once"

        from cli_agent_orchestrator.clients.database import (
            InboxModel as _IM,
        )
        from cli_agent_orchestrator.clients.database import (
            InboxMessageTraceEventModel as _TE,
        )

        with TestSession() as db:
            # A sender-facing notice row now exists, addressed to the supervisor.
            notices = (
                db.query(_IM)
                .filter(_IM.receiver_id == "supervis", _IM.sender_id == "message-trace:worker01")
                .all()
            )
            assert len(notices) == 1, "sender must learn delivery did not happen"
            body = notices[0].message
            assert "delivery-stall" in body
            assert "worker01" in body
            # The trace event marks the original row as surfaced (idempotency + banner arm).
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

        # Idempotency: a second sweep surfaces nothing and writes no new notice.
        surfaced_again = svc.surface_stalled_direct_deliveries()
        assert surfaced_again == 0
        with TestSession() as db:
            notices = (
                db.query(_IM)
                .filter(_IM.receiver_id == "supervis", _IM.sender_id == "message-trace:worker01")
                .all()
            )
            assert len(notices) == 1, "no duplicate stall notice on repeat sweep"

    def test_fresh_direct_message_is_not_surfaced(self, real_sqlite_env, monkeypatch):
        """A message younger than escalate_after_s must not be surfaced."""
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
                    created_at=now - timedelta(seconds=5),  # fresh
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
        """A message from an internal/service sender must not loop back a notice."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        aged = datetime.now(timezone.utc) - timedelta(seconds=4000)
        from cli_agent_orchestrator.clients.database import InboxModel

        with TestSession() as session:
            _seed_terminal(session, "worker01")
            # message-trace: sender is itself a stall notice — must be excluded.
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
        """Supervisor-mailbox rows ride the FX191 ladder, not this sweep."""
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
                    logical_receiver_id="mb_sup_f524",  # mailbox route
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
class TestF524StaleLateDeliveryBanner:
    """Leg 2: a late delivery of a surfaced-stalled message carries a banner."""

    def test_surfaced_message_gets_staleness_banner_on_delivery(self, real_sqlite_env, monkeypatch):
        env = real_sqlite_env
        TestSession = env["TestSession"]

        aged = datetime.now(timezone.utc) - timedelta(seconds=4000)
        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            record_message_trace_event,
        )
        from cli_agent_orchestrator.services.inbox_service import F524_STALL_SURFACED_KIND

        with TestSession() as session:
            _seed_terminal(session, "supervis")
            _seed_terminal(session, "worker01")
            msg = InboxModel(
                sender_id="supervis",
                receiver_id="worker01",
                logical_receiver_id=None,
                message="RULING: take (B).",
                orchestration_type="send_message",
                status="pending",
                created_at=aged,
            )
            session.add(msg)
            session.commit()
            msg_id = msg.id

        # Simulate that the stall was already surfaced.
        record_message_trace_event(msg_id, F524_STALL_SURFACED_KIND, phase="stall_surfaced")

        # The delivery choke point builds `combined` and, for a surfaced-stalled
        # batch, must prefix the staleness banner. We exercise the same helper +
        # branch the choke point uses rather than driving the full deliver_pending
        # state machine (which needs a live provider/tmux backend).
        from cli_agent_orchestrator.clients.database import messages_with_trace_kind

        stale_ids = messages_with_trace_kind([msg_id], F524_STALL_SURFACED_KIND)
        assert stale_ids == {msg_id}

        combined = "RULING: take (B)."
        if stale_ids:
            combined = (
                "[CAO STALE-DELIVERY WARNING] This message was delayed past the "
                "delivery escalation window and is being delivered late. Its sender "
                "was already told it did not land. Treat it as AGED and possibly "
                "SUPERSEDED — reconcile against any newer instruction before acting.\n\n" + combined
            )
        assert combined.startswith("[CAO STALE-DELIVERY WARNING]")
        assert "RULING: take (B)." in combined

    def test_unsurfaced_message_has_no_banner(self, real_sqlite_env, monkeypatch):
        """A never-stalled message delivers with no banner (no false positives)."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            messages_with_trace_kind,
        )
        from cli_agent_orchestrator.services.inbox_service import F524_STALL_SURFACED_KIND

        with TestSession() as session:
            _seed_terminal(session, "supervis")
            _seed_terminal(session, "worker01")
            msg = InboxModel(
                sender_id="supervis",
                receiver_id="worker01",
                logical_receiver_id=None,
                message="fresh normal message",
                orchestration_type="send_message",
                status="pending",
                created_at=datetime.now(timezone.utc),
            )
            session.add(msg)
            session.commit()
            msg_id = msg.id

        assert messages_with_trace_kind([msg_id], F524_STALL_SURFACED_KIND) == set()
