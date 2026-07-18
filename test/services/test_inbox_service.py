"""Tests for the event-driven InboxService."""

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.constants import INBOX_RECONCILE_GRACE_SECONDS
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError


def _make_message(
    id=1,
    receiver_id="term-1",
    message="hello",
    status=MessageStatus.PENDING,
    sender_id="sender-1",
    orchestration_type=OrchestrationType.SEND_MESSAGE,
):
    return InboxMessage(
        id=id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=message,
        orchestration_type=orchestration_type,
        status=status,
        created_at=datetime.now(),
    )


def _terminal_service_mock():
    mock = MagicMock()
    mock.prepare_input.side_effect = lambda _terminal_id, text, *_args, **_kwargs: text
    class _PreparedSendMock(MagicMock):
        def assert_called_once_with(self, *args, **kwargs):
            self.assert_called_once()
            assert self.call_args.args[:len(args)] == args
            for key, value in kwargs.items():
                assert self.call_args.kwargs[key] == value
    mock.send_prepared_input = _PreparedSendMock()
    return mock


@pytest.fixture(autouse=True)
def _msgtrace_pipeline_defaults():
    """Run legacy delivery cases through the honest trace pipeline by default."""
    attempt = {
        "attempt_uuid": "attempt-1", "started_at": "2026-07-11T00:00:00+00:00",
        "evidence": {},
    }
    with (
        patch("cli_agent_orchestrator.services.inbox_service.count_ambiguous_attempts", return_value=0),
        patch("cli_agent_orchestrator.services.inbox_service.list_message_attempts", return_value=[]),
        patch("cli_agent_orchestrator.services.inbox_service.confirm_batch_from_prior_attempt",
              return_value=True),
        patch("cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt", return_value="attempt-1"),
        patch("cli_agent_orchestrator.services.inbox_service.get_message_trace", return_value={"attempts": [attempt]}),
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery", return_value=("unverified", {"kind": "send_returned_unverified"})),
        patch("cli_agent_orchestrator.services.inbox_service.settle_delivery_attempt"),
        patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata", return_value={"provider": "event"}),
    ):
        yield


class TestDeliverPending:
    """Tests for InboxService.deliver_pending()."""

    @patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.inbox_service.create_inbox_message")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_draft_guard_deferral_counts_attempted_only_and_notifies_caller_once(
        self, mock_get, _mock_update, mock_monitor, mock_term_svc, mock_create, mock_metadata
    ):
        attempted = _make_message(id=1)
        suffix = _make_message(id=2, sender_id="sender-2")
        mock_get.return_value = [attempted, suffix]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_prepared_input.side_effect = DeliveryDeferredError("unstable")
        mock_metadata.return_value = {"caller_id": "caller-1"}
        svc = InboxService()
        for _ in range(6):
            svc.deliver_pending("term-1", num_messages=0)
        assert svc._defer_attempts == {1: 6}
        assert svc._defer_notified == {1}
        mock_create.assert_called_once()
        assert mock_create.call_args.args[1] == "caller-1"

    @patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.inbox_service.create_inbox_message")
    def test_draft_guard_fifth_deferral_without_caller_warns_only(
        self, mock_create, mock_metadata, caplog
    ):
        mock_metadata.return_value = {"caller_id": None}
        svc = InboxService()
        message = _make_message()
        for _ in range(5):
            svc._record_delivery_deferred("term-1", [message])
        mock_create.assert_not_called()
        assert "no caller_id" in caplog.text

    def test_defer_state_is_evicted_on_terminal_outcomes(self):
        svc = InboxService()
        message = _make_message()
        svc._defer_attempts[1] = 4
        svc._defer_notified.add(1)
        svc._evict_defer_state([message])
        assert svc._defer_attempts == {}
        assert svc._defer_notified == set()

    @pytest.mark.parametrize("registry", [None, MagicMock()])
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_default_mode_identity_with_and_without_registry(
        self, mock_get, mock_monitor, mock_term_svc, _mock_update, registry
    ):
        mock_get.return_value = [_make_message(message="verbatim")]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        InboxService().deliver_pending("term-1", registry=registry)

        kwargs = mock_term_svc.send_prepared_input.call_args.kwargs
        assert mock_term_svc.send_prepared_input.call_args.args == ("term-1", "verbatim")
        if registry is None:
            assert kwargs["registry"] is None
            assert kwargs["sender_id"] == "sender-1"
        else:
            assert kwargs["registry"] is registry
            assert kwargs["sender_id"] == "sender-1"
            assert kwargs["orchestration_type"] is OrchestrationType.SEND_MESSAGE

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    @patch("cli_agent_orchestrator.services.terminal_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.preserve_draft_before_send")
    @patch("cli_agent_orchestrator.services.terminal_service.inject_memory_context")
    @patch("cli_agent_orchestrator.services.terminal_service._append_message_contract")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.get_backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_queued_assign_matches_direct_assign_event_and_backend_bytes(
        self,
        mock_metadata,
        mock_backend_factory,
        mock_provider_manager,
        _mock_terminal_status,
        mock_contract,
        mock_memory,
        mock_preserve,
        _mock_update_active,
        mock_dispatch,
        mock_get_pending,
        mock_inbox_status,
        _mock_update_message,
    ):
        raw = "MCP-shaped assigned task"
        registry = MagicMock()
        metadata = {
            "tmux_session": "cao-session",
            "tmux_window": "worker-window",
            "agent_profile": "developer",
        }
        mock_metadata.return_value = metadata
        provider = mock_provider_manager.get_provider.return_value
        provider.blocks_orchestrated_input_while_waiting_user_answer = False
        provider.paste_enter_count = 1
        provider.paste_submit_delay = 0.3
        mock_preserve.return_value = None
        mock_contract.side_effect = lambda message, _metadata, _mode: f"{message}|contract"
        mock_memory.side_effect = lambda message, _terminal_id, **_kwargs: f"memory|{message}"
        mock_inbox_status.get_status.return_value = TerminalStatus.IDLE
        mock_get_pending.return_value = [
            _make_message(
                receiver_id="queued",
                message=raw,
                sender_id="caller01",
                orchestration_type=OrchestrationType.ASSIGN,
            )
        ]

        terminal_service.send_input(
            "direct",
            raw,
            registry=registry,
            sender_id="caller01",
            orchestration_type=OrchestrationType.ASSIGN,
        )
        InboxService().deliver_pending("queued", registry=registry)

        pasted = [entry.args[2] for entry in mock_backend_factory.return_value.send_keys.call_args_list]
        assert pasted == ["memory|MCP-shaped assigned task|contract"] * 2
        events = [entry.args[2] for entry in mock_dispatch.call_args_list]
        assert [event.message for event in events] == [raw, raw]
        assert [event.orchestration_type for event in events] == [
            OrchestrationType.ASSIGN,
            OrchestrationType.ASSIGN,
        ]
        assert mock_contract.call_count == 2
        assert mock_memory.call_count == 2

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_mixed_modes_same_sender_preserve_fifo_as_separate_sends(
        self, mock_get, mock_monitor, mock_term_svc, _mock_update
    ):
        mock_get.return_value = [
            _make_message(id=1, message="assigned", orchestration_type=OrchestrationType.ASSIGN),
            _make_message(id=2, message="ordinary"),
        ]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        InboxService().deliver_pending("term-1", num_messages=0)

        assert [entry.args[1] for entry in mock_term_svc.send_prepared_input.call_args_list] == [
            "assigned",
            "ordinary",
        ]
        assert [
            entry.kwargs.get("orchestration_type")
            for entry in mock_term_svc.send_prepared_input.call_args_list
        ] == [OrchestrationType.ASSIGN, None]

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_time_input_block_returns_remaining_batch_to_pending(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        messages = [
            _make_message(id=1, orchestration_type=OrchestrationType.ASSIGN),
            _make_message(id=2, orchestration_type=OrchestrationType.ASSIGN),
        ]
        mock_get.return_value = messages
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_prepared_input.side_effect = TerminalInputBlockedError("dialog")

        InboxService().deliver_pending("term-1", num_messages=0)

        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_idle(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_called_once_with(
            "term-1",
            "hello",
            defer_on_dialog=True,
        )
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_message_when_completed(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_called_once_with(
            "term-1",
            "hello",
            defer_on_dialog=True,
        )
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_no_pending_messages(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        mock_get.return_value = []

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_processing(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_skips_when_unknown(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.UNKNOWN

        svc = InboxService()
        svc.deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_multiple_messages_concatenated(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        msgs = [_make_message(id=1, message="hello"), _make_message(id=2, message="world")]
        mock_get.return_value = msgs
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1", num_messages=2)

        mock_get.assert_called_once_with("term-1", limit=2)
        mock_term_svc.send_prepared_input.assert_called_once_with(
            "term-1",
            "hello\nworld",
            defer_on_dialog=True,
        )
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivers_all_when_num_messages_zero(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        msgs = [_make_message(id=i, message=f"msg{i}") for i in range(3)]
        mock_get.return_value = msgs
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        svc = InboxService()
        svc.deliver_pending("term-1", num_messages=0)

        mock_get.assert_called_once_with("term-1", limit=100)
        mock_term_svc.send_prepared_input.assert_called_once_with(
            "term-1",
            "msg0\nmsg1\nmsg2",
            defer_on_dialog=True,
        )
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_marks_failed_on_send_error(self, mock_get, mock_monitor, mock_term_svc, mock_update):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_prepared_input.side_effect = RuntimeError("tmux error")

        svc = InboxService()
        svc.deliver_pending("term-1")

        # Trace settlement owns DELIVERING -> FAILED atomically.
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_marks_delivered_before_send_input(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        """Regression for the double-delivery race (#164).

        send_input()'s output flows back through the FIFO/StatusMonitor pipeline
        and can re-emit a status event that re-enters deliver_pending. The
        message must already be DELIVERED by then, so the status update has to
        happen before send_input is called.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        order = []
        mock_update.side_effect = lambda *args, **kwargs: order.append(("update", args))
        mock_term_svc.send_prepared_input.side_effect = lambda *args, **kwargs: order.append(("send", args))

        svc = InboxService()
        svc.deliver_pending("term-1")

        assert order == [("send", ("term-1", "hello"))]

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_resolution_failure_leaves_message_pending(
        self, mock_get, mock_monitor, mock_term_svc, mock_update
    ):
        """A TerminalNotFoundError during send leaves the message PENDING, not FAILED.

        Pane resolution can transiently fail (e.g. herdr pane not yet resolvable).
        Status is optimistically set DELIVERED before send (to close the
        re-entrancy race), so on a resolution failure it must be reset to PENDING
        for a later retry — never left DELIVERED or marked FAILED.
        """
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_prepared_input.side_effect = TerminalNotFoundError("s:w")

        svc = InboxService()
        svc.deliver_pending("term-1")

        # Trace settlement owns DELIVERING -> PENDING, never legacy direct updates.
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_concurrent_delivery_sends_stateful_pending_message_once(
        self, mock_get, mock_update, mock_monitor, mock_term_svc
    ):
        message = _make_message()
        state_lock = threading.Lock()
        statuses = {message.id: MessageStatus.PENDING}

        def get_pending(*args, **kwargs):
            with state_lock:
                snapshot = (
                    [message]
                    if statuses[message.id] == MessageStatus.PENDING
                    else []
                )
                if snapshot:
                    statuses[message.id] = MessageStatus.DELIVERING
            time.sleep(0.2)
            return snapshot

        def update(message_id, status):
            with state_lock:
                statuses[message_id] = status

        mock_get.side_effect = get_pending
        mock_update.side_effect = update
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        start = threading.Barrier(3)
        service = InboxService()

        def deliver():
            start.wait()
            service.deliver_pending("term-1")

        threads = [threading.Thread(target=deliver) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)

        assert all(not thread.is_alive() for thread in threads)
        mock_term_svc.send_prepared_input.assert_called_once_with(
            "term-1",
            "hello",
            defer_on_dialog=True,
        )
        assert statuses[message.id] == MessageStatus.DELIVERING

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_waiting_dialog_opened_before_send_resets_pending(
        self, mock_get, mock_update, mock_monitor, mock_term_svc, mock_pm
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.side_effect = [
            TerminalStatus.IDLE,
            TerminalStatus.IDLE,
            TerminalStatus.WAITING_USER_ANSWER,
        ]
        mock_pm.get_provider.return_value.blocks_orchestrated_input_while_waiting_user_answer = (
            True
        )

        InboxService().deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_not_called()
        assert mock_update.call_args_list == [call(1, MessageStatus.PENDING)]
        assert mock_monitor.get_status.call_count == 3

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_waiting_before_first_sender_resets_all_marked_messages(
        self, mock_get, mock_update, mock_monitor, mock_term_svc, mock_pm
    ):
        messages = [
            _make_message(id=1, sender_id="sender-1"),
            _make_message(id=2, sender_id="sender-2"),
        ]
        mock_get.return_value = messages
        mock_monitor.get_status.side_effect = [
            TerminalStatus.IDLE,
            TerminalStatus.IDLE,
            TerminalStatus.WAITING_USER_ANSWER,
        ]
        mock_pm.get_provider.return_value.blocks_orchestrated_input_while_waiting_user_answer = (
            True
        )

        InboxService().deliver_pending("term-1", num_messages=0)

        mock_term_svc.send_prepared_input.assert_not_called()
        assert mock_update.call_args_list == [
            call(1, MessageStatus.PENDING), call(2, MessageStatus.PENDING)
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_dialog_snapshot_defer_resets_pending_not_failed(
        self, mock_get, mock_update, mock_monitor, mock_term_svc
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        mock_term_svc.send_prepared_input.side_effect = DeliveryDeferredError("dialog")

        InboxService().deliver_pending("term-1")

        mock_update.assert_not_called()
        assert call(1, MessageStatus.FAILED) not in mock_update.call_args_list

    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_processing_at_pre_send_recheck_still_delivers(
        self, mock_get, mock_update, mock_monitor, mock_term_svc
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.side_effect = [
            TerminalStatus.IDLE,
            TerminalStatus.IDLE,
            TerminalStatus.PROCESSING,
        ]

        InboxService().deliver_pending("term-1")

        mock_term_svc.send_prepared_input.assert_called_once_with(
            "term-1",
            "hello",
            defer_on_dialog=True,
        )
        mock_update.assert_not_called()


class TestEagerInboxDelivery:
    """Tests for eager inbox delivery (CAO_EAGER_INBOX_DELIVERY).

    Covers the relaxed status gate in deliver_pending() that allows PROCESSING
    and WAITING_USER_ANSWER delivery when the env var is enabled and the
    provider declares accepts_input_while_processing=True.
    """

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_idle_status_always_works(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """IDLE delivers regardless of env var or provider capability."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.IDLE
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_completed_status_always_works(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """COMPLETED delivers regardless of env var or provider capability."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch(
        "cli_agent_orchestrator.services.inbox_service.terminal_service",
        new_callable=_terminal_service_mock,
    )
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_unanchored_footer_no_longer_self_latches_delivery(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        frame = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "issue405"
            / "03-idle-false-positive-2.1.209.plain.txt"
        ).read_text(encoding="utf-8")
        provider = ClaudeCodeProvider("t1", "session", "window")
        provider._resolve_native_status = lambda *_: None  # type: ignore[method-assign]
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
            return_value=frame.splitlines(),
        ):
            classified = provider.get_status(frame)
        assert classified == TerminalStatus.COMPLETED

        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = classified
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            InboxService().deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """PROCESSING + eager ON + capable provider -> delivers."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_non_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """PROCESSING + eager ON + non-capable provider -> skips."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_disabled(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """PROCESSING + eager OFF -> skips even for capable provider."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_waiting_user_answer_with_eager_enabled_and_capable_provider(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """WAITING_USER_ANSWER never delivers, even on the eager path."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_waiting_provider_defers_even_with_eager_enabled(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            InboxService().deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service", new_callable=_terminal_service_mock)
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_error_status_never_delivers(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        """ERROR -> never delivers regardless of flags."""
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.ERROR
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            svc = InboxService()
            svc.deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_not_called()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch(
        "cli_agent_orchestrator.services.inbox_service.terminal_service",
        new_callable=_terminal_service_mock,
    )
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.status_monitor")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_render_uncertain_never_delivers_even_in_eager_mode(
        self, mock_get, mock_monitor, mock_pm, mock_term_svc, mock_update
    ):
        mock_get.return_value = [_make_message()]
        mock_monitor.get_status.return_value = TerminalStatus.RENDER_UNCERTAIN
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        provider.blocks_orchestrated_input_while_waiting_user_answer = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            InboxService().deliver_pending("t1")

        mock_term_svc.send_prepared_input.assert_not_called()
        mock_update.assert_not_called()


class TestPollOpenCodePendingMessages:
    """Tests for the OpenCode inbox poller."""

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider")
    def test_polls_pending_opencode_receivers(self, mock_list_receivers):
        """Test poller attempts delivery for each pending OpenCode receiver."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock()
        svc.poll_opencode_pending_messages()

        mock_list_receivers.assert_called_once_with("opencode_cli")
        assert svc.deliver_pending.call_args_list == [
            call("receiver-1", registry=None),
            call("receiver-2", registry=None),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider")
    def test_survives_per_receiver_failure(self, mock_list_receivers):
        """Test one failed receiver does not stop the poll loop."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=[Exception("tmux busy"), None])
        svc.poll_opencode_pending_messages()

        assert svc.deliver_pending.call_count == 2


class TestReconcileOrphanedMessages:
    """Tests for the provider-agnostic inbox reconciliation sweep (issue #131)."""

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_reconciles_stale_receivers(self, mock_list_receivers):
        """Sweep attempts delivery for each receiver with an orphaned message."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock()
        svc.reconcile_orphaned_messages()

        mock_list_receivers.assert_called_once_with(INBOX_RECONCILE_GRACE_SECONDS)
        assert svc.deliver_pending.call_args_list == [
            call("receiver-1", registry=None),
            call("receiver-2", registry=None),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_older_than")
    def test_survives_per_receiver_failure(self, mock_list_receivers):
        """One failed receiver does not stop the sweep."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        svc = InboxService()
        svc.deliver_pending = MagicMock(side_effect=[Exception("tmux busy"), None])
        svc.reconcile_orphaned_messages()

        assert svc.deliver_pending.call_count == 2


class TestRun:
    """Tests for InboxService.run() event loop."""

    @pytest.mark.asyncio
    async def test_processes_idle_status_event(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            # Run one iteration then cancel
            async def run_one():
                task = asyncio.create_task(svc.run())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            await run_one()

        svc.deliver_pending.assert_called_once_with("abc123", registry=None)

    @pytest.mark.asyncio
    async def test_processes_completed_status_event(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.xyz789.status",
                "data": {"status": TerminalStatus.COMPLETED.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.deliver_pending.assert_called_once_with("xyz789", registry=None)

    @pytest.mark.asyncio
    async def test_ignores_processing_status_event(self):
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.PROCESSING.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.deliver_pending.assert_not_called()

    @pytest.mark.asyncio
    async def test_threads_registry_to_delivery(self):
        """run(registry) threads the plugin registry to deliver_pending so
        status-driven deliveries fire PostSendMessageEvent hooks with the same
        attribution as the immediate and OpenCode-poller paths (PR #273 review).
        """
        svc = InboxService()
        svc.deliver_pending = MagicMock()
        registry = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value},
            }
        )

        with patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus:
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run(registry))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.deliver_pending.assert_called_once_with("abc123", registry=registry)

    @pytest.mark.asyncio
    async def test_offloads_delivery_to_thread(self):
        """Delivery is offloaded via asyncio.to_thread so the consumer loop keeps
        yielding to the event loop and never blocks StatusMonitor/LogWriter on
        deliver_pending's synchronous DB + tmux I/O (PR #273 review; see the
        threading discipline note in docs/event-driven-architecture.md).
        """
        svc = InboxService()
        svc.deliver_pending = MagicMock()

        queue = asyncio.Queue()
        await queue.put(
            {
                "topic": "terminal.abc123.status",
                "data": {"status": TerminalStatus.IDLE.value},
            }
        )

        with (
            patch("cli_agent_orchestrator.services.inbox_service.bus") as mock_bus,
            patch(
                "cli_agent_orchestrator.services.inbox_service.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread,
        ):
            mock_bus.subscribe.return_value = queue

            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_to_thread.assert_awaited_once_with(svc.deliver_pending, "abc123", registry=None)
