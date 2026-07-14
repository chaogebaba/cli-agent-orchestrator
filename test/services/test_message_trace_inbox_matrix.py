"""Focused MSGTRACE inbox state/race/recovery regression matrix."""

import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service as module
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.message_trace_service import transcript_lookup, wire_hash
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.cli.commands.messages import messages


def _message(status=MessageStatus.PENDING):
    return InboxMessage(id=1, sender_id="sender", receiver_id="receiver", message="raw",
                        orchestration_type=OrchestrationType.SEND_MESSAGE,
                        status=status, created_at=datetime.now())


@pytest.fixture
def delivery(monkeypatch):
    message = _message()
    monkeypatch.setattr(module, "get_pending_messages", lambda *_a, **_k: [message])
    monkeypatch.setattr(module, "get_terminal_metadata", lambda _id: {
        "id": "receiver", "provider": "claude_code", "caller_id": None,
        "tmux_session": "s", "tmux_window": "w",
    })
    monkeypatch.setattr(module.status_monitor, "get_status", lambda _id: TerminalStatus.IDLE)
    monkeypatch.setattr(module.status_monitor, "get_input_gen", lambda _id: 1)
    monkeypatch.setattr(module.status_monitor, "get_status_gen", lambda _id: 2)
    monkeypatch.setattr(module, "count_ambiguous_attempts", lambda _ids: 0)
    monkeypatch.setattr(module, "list_message_attempts", lambda _ids: [])
    monkeypatch.setattr(module, "resolve_session_transcript", lambda _meta: Path("/trace"))
    monkeypatch.setattr(module, "transcript_ref", lambda _path: {"path": "/trace", "size": 0})
    monkeypatch.setattr(module, "begin_delivery_attempt", lambda *_a, **_k: "attempt-1")
    monkeypatch.setattr(module, "get_message_trace", lambda _id: {"attempts": [{
        "attempt_uuid": "attempt-1", "started_at": datetime.now().isoformat(),
        "evidence": {"path": "/trace", "size": 0},
    }]})
    monkeypatch.setattr(module.terminal_service, "prepare_input", lambda *_a, **_k: "wire")
    send = MagicMock()
    monkeypatch.setattr(module.terminal_service, "send_prepared_input", send)
    settle = MagicMock()
    monkeypatch.setattr(module, "settle_delivery_attempt", settle)
    monkeypatch.setattr(InboxService, "_commit_watchdog_ops", MagicMock())
    return message, send, settle


def test_prior_attempt_hit_deduplicates_without_paste(monkeypatch, delivery):
    _message_row, send, _settle = delivery
    monkeypatch.setattr(module, "list_message_attempts", lambda _ids: [{
        "attempt_uuid": "old", "outcome": "ambiguous", "payload_hash": "hash",
        "started_at": datetime.now(), "evidence": "{}",
    }])
    monkeypatch.setattr(module, "_wpm2_lookup", lambda *_a, **_k: ("hit", {}))
    confirm = MagicMock(return_value=True)
    monkeypatch.setattr(module, "confirm_batch_from_prior_attempt", confirm)
    InboxService().deliver_pending("receiver")
    send.assert_not_called()
    assert confirm.call_args.args[:2] == ([1], "old")


def test_delivery_persists_resolution_kind_at_begin_and_settle(monkeypatch, delivery):
    _message_row, _send, settle = delivery
    begin = MagicMock(return_value="attempt-1")
    monkeypatch.setattr(module, "begin_delivery_attempt", begin)
    monkeypatch.setattr(
        module, "transcript_ref",
        lambda _resolution: {"path": "/trace", "inode": 7, "size": 0,
                             "resolution_kind": "binding"},
    )
    monkeypatch.setattr(
        module, "confirm_delivery",
        lambda *_a, **_k: (
            "hit",
            {"kind": "transcript_user_turn", "path": "/trace", "inode": 7,
             "resolution_kind": "binding"},
        ),
    )
    InboxService().deliver_pending("receiver")
    begin_evidence = json.loads(begin.call_args.kwargs["evidence"])
    settle_evidence = json.loads(settle.call_args.kwargs["evidence"])
    assert begin_evidence["resolution_kind"] == "binding"
    assert settle_evidence["resolution_kind"] == "binding"
    assert settle_evidence["kind"] == "transcript_user_turn"


def test_prior_attempt_miss_performs_retry_paste(monkeypatch, delivery):
    _message_row, send, _settle = delivery
    monkeypatch.setattr(module, "list_message_attempts", lambda _ids: [{
        "attempt_uuid": "old", "outcome": "ambiguous", "payload_hash": "hash",
        "started_at": datetime.now(), "evidence": "{}",
    }])
    monkeypatch.setattr(module, "_wpm2_lookup", lambda *_a, **_k: ("absent", {}))
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: ("unverified", {}))
    InboxService().deliver_pending("receiver")
    send.assert_called_once()


def test_retry_reshapes_and_persists_each_wire_hash_not_raw(monkeypatch, delivery):
    _message_row, _send, _settle = delivery
    prepared = iter(["memory|contract|raw", "contract|raw"])
    monkeypatch.setattr(module.terminal_service, "prepare_input",
                        lambda *_a, **_k: next(prepared))
    captured = []
    monkeypatch.setattr(module, "begin_delivery_attempt",
                        lambda _batch, _receiver, _provider, digest, length, *_a, **_k:
                        captured.append((digest, length)) or f"attempt-{len(captured)}")
    monkeypatch.setattr(module, "get_message_trace", lambda _id: {"attempts": [{
        "attempt_uuid": f"attempt-{len(captured)}", "started_at": datetime.now().isoformat(),
        "evidence": {},
    }]})
    outcomes = iter([("ambiguous", {}), ("unverified", {})])
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: next(outcomes))
    svc = InboxService()
    svc.deliver_pending("receiver")
    svc.deliver_pending("receiver")
    assert [item[0] for item in captured] == [
        module.wire_hash("memory|contract|raw"), module.wire_hash("contract|raw")]
    assert all(item[0] != module.wire_hash("raw") for item in captured)


def test_real_contract_and_first_memory_shape_bind_transcript_hash(monkeypatch, tmp_path):
    terminal_service._memory_injected_terminals.discard("receiver")
    metadata = {
        "id": "receiver", "tmux_session": "s", "tmux_window": "w",
        "agent_profile": "worker",
    }
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _id: metadata)
    profile = MagicMock(messageContract="return through CAO")
    monkeypatch.setattr(terminal_service, "load_agent_profile", lambda _name: profile)
    monkeypatch.setattr(
        terminal_service.MemoryService, "get_curated_memory_context",
        lambda *_a, **_k: "<cao-memory>remember this</cao-memory>")

    first = terminal_service.prepare_input(
        "receiver", "raw", OrchestrationType.SEND_MESSAGE)
    assert first == (
        "<cao-memory>remember this</cao-memory>\n\n"
        "raw\n\n[Contract: return through CAO]")
    with terminal_service._memory_injected_lock:
        terminal_service._memory_injected_terminals.add("receiver")
    second = terminal_service.prepare_input(
        "receiver", "raw", OrchestrationType.SEND_MESSAGE)
    assert second == "raw\n\n[Contract: return through CAO]"

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(json.dumps({"type": "user", "message": value})
                    for value in (first, second)) + "\n",
        encoding="utf-8",
    )
    assert transcript_lookup(transcript, wire_hash(first))[0] == "hit"
    assert transcript_lookup(transcript, wire_hash(second))[0] == "hit"
    assert transcript_lookup(transcript, wire_hash("raw"))[0] == "absent"
    terminal_service._memory_injected_terminals.discard("receiver")


def test_ambiguity_cap_is_checked_before_fourth_paste(monkeypatch, delivery):
    _message_row, send, _settle = delivery
    monkeypatch.setattr(module, "count_ambiguous_attempts", lambda _ids: 3)
    monkeypatch.setattr(module, "transition_pending_to_delivery_failed", lambda _ids: True)
    notify = MagicMock()
    monkeypatch.setattr(InboxService, "_notify_delivery_failed", notify)
    InboxService().deliver_pending("receiver")
    send.assert_not_called()
    notify.assert_called_once_with("receiver", [1])


def test_two_ambiguous_attempts_do_not_cap_or_notify(monkeypatch, delivery):
    _message_row, send, _settle = delivery
    monkeypatch.setattr(module, "count_ambiguous_attempts", lambda _ids: 2)
    transition = MagicMock()
    monkeypatch.setattr(module, "transition_pending_to_delivery_failed", transition)
    notify = MagicMock()
    monkeypatch.setattr(InboxService, "_notify_delivery_failed", notify)
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: ("unverified", {}))
    InboxService().deliver_pending("receiver")
    send.assert_called_once()
    transition.assert_not_called()
    notify.assert_not_called()


def test_delivery_failed_without_caller_is_durable_warning(monkeypatch, caplog):
    monkeypatch.setattr(module, "get_terminal_metadata", lambda _id: {"caller_id": None})
    InboxService()._notify_delivery_failed("receiver", [1])
    assert "no caller_id" in caplog.text


def test_waiter_queued_during_ambiguous_settlement_skips_same_wake(monkeypatch, delivery):
    _message_row, send, _settle = delivery
    entered = threading.Event()
    release = threading.Event()

    def blocked_send(*_a, **_k):
        entered.set()
        assert release.wait(2)

    send.side_effect = blocked_send
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: ("ambiguous", {}))
    svc = InboxService()
    first = threading.Thread(target=svc.deliver_pending, args=("receiver",))
    second = threading.Thread(target=svc.deliver_pending, args=("receiver",))
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert send.call_count == 1


def test_later_idle_stuck_entry_retries_after_ambiguous(monkeypatch, delivery):
    _message_row, send, _settle = delivery
    outcomes = iter([("ambiguous", {}), ("unverified", {"kind": "send_returned_unverified"})])
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: next(outcomes))
    svc = InboxService()
    svc.deliver_pending("receiver")
    svc.deliver_pending("receiver")
    assert send.call_count == 2


def test_watchdog_operations_are_owned_by_confirmed_transaction(monkeypatch, delivery):
    _message_row, _send, settle = delivery
    commit = MagicMock()
    monkeypatch.setattr(InboxService, "_commit_watchdog_ops", commit)
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: ("unverified", {}))
    InboxService().deliver_pending("receiver")
    callback = settle.call_args.kwargs["on_confirmed"]
    commit.assert_not_called()
    callback()
    commit.assert_called_once()


def test_ambiguous_attempt_never_arms_watchdog(monkeypatch, delivery):
    _message_row, _send, settle = delivery
    commit = MagicMock()
    monkeypatch.setattr(InboxService, "_commit_watchdog_ops", commit)
    monkeypatch.setattr(module, "confirm_delivery", lambda *_a, **_k: ("ambiguous", {}))
    InboxService().deliver_pending("receiver")
    assert "on_confirmed" not in settle.call_args.kwargs
    commit.assert_not_called()


def test_generic_send_exception_settles_failed_durably(monkeypatch, delivery):
    _message_row, send, settle = delivery
    send.side_effect = RuntimeError("backend failed")
    InboxService().deliver_pending("receiver")
    assert settle.call_args.args[1:3] == (MessageStatus.FAILED, "failed")
    assert settle.call_args.kwargs["error"] == "backend failed"


def test_ambiguous_outcome_settles_pending_with_evidence(monkeypatch, delivery):
    _message_row, _send, settle = delivery
    monkeypatch.setattr(
        module, "confirm_delivery",
        lambda *_a, **_k: ("ambiguous", {"kind": "transcript_absent"}),
    )
    InboxService().deliver_pending("receiver")
    assert settle.call_args.args[1:3] == (MessageStatus.PENDING, "ambiguous")
    assert json.loads(settle.call_args.kwargs["evidence"])["kind"] == "transcript_absent"


@pytest.mark.parametrize(
    ("path", "lookup", "status", "outcome"),
    [
        (Path("/trace"), "hit", MessageStatus.DELIVERED, "confirmed"),
        (Path("/trace"), "absent", MessageStatus.PENDING, "interrupted"),
        (Path("/trace"), "unresolved", MessageStatus.DELIVERY_FAILED, "unresolved"),
        (None, None, MessageStatus.PENDING, "interrupted"),
    ],
)
def test_startup_sweep_oracle_matrix(monkeypatch, path, lookup, status, outcome):
    message = _message(MessageStatus.DELIVERING)
    monkeypatch.setattr(module, "list_stale_delivering_messages", lambda: [message])
    monkeypatch.setattr(module, "get_message_trace", lambda _id: {"attempts": [{
        "attempt_uuid": "attempt", "payload_hash": "hash", "started_at": datetime.now(),
        "evidence": {}, "sender_id": "sender", "orchestration_type": "send_message",
    }]})
    monkeypatch.setattr(module, "list_attempt_member_ids", lambda _id: [1])
    monkeypatch.setattr(module, "get_terminal_metadata", lambda _id: {
        "id": "receiver", "tmux_session": "s", "tmux_window": "w", "caller_id": None,
    })
    backend = MagicMock()
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(module, "resolve_session_transcript", lambda _meta: path)
    if lookup is not None:
        monkeypatch.setattr(module, "transcript_lookup", lambda *_a, **_k: (lookup, {}))
    settle = MagicMock()
    monkeypatch.setattr(module, "settle_delivery_attempt", settle)
    monkeypatch.setattr(InboxService, "_notify_delivery_failed", MagicMock())
    monkeypatch.setattr(InboxService, "_commit_watchdog_ops", MagicMock())
    svc = InboxService()
    svc.recover_stale_deliveries()
    assert settle.call_args.args[1:3] == (status, outcome)


def test_startup_sweep_metadata_gone_is_failed(monkeypatch):
    message = _message(MessageStatus.DELIVERING)
    monkeypatch.setattr(module, "list_stale_delivering_messages", lambda: [message])
    monkeypatch.setattr(module, "get_message_trace", lambda _id: {"attempts": [{
        "attempt_uuid": "attempt", "payload_hash": "hash", "started_at": datetime.now(),
        "evidence": {}, "sender_id": "sender", "orchestration_type": "send_message",
    }]})
    monkeypatch.setattr(module, "list_attempt_member_ids", lambda _id: [1])
    monkeypatch.setattr(module, "get_terminal_metadata", lambda _id: None)
    settle = MagicMock()
    monkeypatch.setattr(module, "settle_delivery_attempt", settle)
    InboxService().recover_stale_deliveries()
    assert settle.call_args.args[1:3] == (MessageStatus.FAILED, "failed")


def test_startup_sweep_pane_gap_requeues_without_transcript_reader(monkeypatch):
    message = _message(MessageStatus.DELIVERING)
    monkeypatch.setattr(module, "list_stale_delivering_messages", lambda: [message])
    monkeypatch.setattr(module, "get_message_trace", lambda _id: {"attempts": [{
        "attempt_uuid": "attempt", "payload_hash": "hash", "started_at": datetime.now(),
        "evidence": {}, "sender_id": "sender", "orchestration_type": "send_message",
    }]})
    monkeypatch.setattr(module, "list_attempt_member_ids", lambda _id: [1])
    monkeypatch.setattr(module, "get_terminal_metadata", lambda _id: {
        "id": "receiver", "tmux_session": "s", "tmux_window": "w"})
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("pane gap")
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    reader = MagicMock()
    monkeypatch.setattr(module, "resolve_session_transcript", reader)
    settle = MagicMock()
    monkeypatch.setattr(module, "settle_delivery_attempt", settle)
    InboxService().recover_stale_deliveries()
    assert settle.call_args.args[1:3] == (MessageStatus.PENDING, "interrupted")
    reader.assert_not_called()


def test_startup_sweep_unresolved_notifies_once_and_is_idempotent(monkeypatch):
    message = _message(MessageStatus.DELIVERING)
    monkeypatch.setattr(module, "list_stale_delivering_messages", MagicMock(
        side_effect=[[message], []]))
    monkeypatch.setattr(module, "get_message_trace", lambda _id: {"attempts": [{
        "attempt_uuid": "attempt", "payload_hash": "hash", "started_at": datetime.now(),
        "evidence": {}, "sender_id": "sender", "orchestration_type": "send_message",
    }]})
    monkeypatch.setattr(module, "list_attempt_member_ids", lambda _id: [1])
    monkeypatch.setattr(module, "get_terminal_metadata", lambda _id: {
        "id": "receiver", "tmux_session": "s", "tmux_window": "w", "caller_id": "caller",
    })
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", MagicMock())
    static_reader = MagicMock(return_value=Path("/trace"))
    monkeypatch.setattr(module, "resolve_session_transcript", static_reader)
    live_provider_lookup = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        live_provider_lookup,
    )
    monkeypatch.setattr(module, "transcript_lookup", lambda *_a, **_k: (
        "unresolved", {"kind": "transcript_unreadable"}))
    settle = MagicMock(return_value=True)
    monkeypatch.setattr(module, "settle_delivery_attempt", settle)
    notify = MagicMock()
    monkeypatch.setattr(InboxService, "_notify_delivery_failed", notify)
    svc = InboxService()
    svc.recover_stale_deliveries()
    svc.recover_stale_deliveries()
    static_reader.assert_called_once()
    live_provider_lookup.assert_not_called()
    notify.assert_called_once_with("receiver", [1])
    assert settle.call_args.args[1:3] == (MessageStatus.DELIVERY_FAILED, "unresolved")


def test_delivery_failed_notification_transition_is_exactly_once_across_events(
    monkeypatch, delivery
):
    _message_row, send, _settle = delivery
    monkeypatch.setattr(module, "count_ambiguous_attempts", lambda _ids: 3)
    transition = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(module, "transition_pending_to_delivery_failed", transition)
    notify = MagicMock()
    monkeypatch.setattr(InboxService, "_notify_delivery_failed", notify)
    svc = InboxService()
    svc.deliver_pending("receiver")
    svc.deliver_pending("receiver")
    send.assert_not_called()
    notify.assert_called_once_with("receiver", [1])


def test_messages_trace_cli_table_and_json(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "message": {"status": "delivered"},
        "attempts": [{"started_at": "now", "outcome": "confirmed",
                      "attempt_uuid": "attempt", "reason": None}],
    }
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.messages.requests.get",
                        lambda *_a, **_k: response)
    runner = CliRunner()
    table = runner.invoke(messages, ["trace", "7"])
    assert table.exit_code == 0
    assert "message 7  status=delivered" in table.output
    assert "confirmed" in table.output
    json_result = runner.invoke(messages, ["trace", "7", "--json"])
    assert json_result.exit_code == 0
    assert '"status": "delivered"' in json_result.output


def test_terminal_delete_state_cleans_lock_and_wake_seq_together():
    module._get_delivery_lock("cleanup-target")
    with module._delivery_seq_guard:
        module._delivery_wake_seq["cleanup-target"] = 4
    module.clear_terminal_delivery_state("cleanup-target")
    assert "cleanup-target" not in module._delivery_locks
    assert "cleanup-target" not in module._delivery_wake_seq


def test_prepared_send_consumes_memory_before_backend_io_exception(monkeypatch):
    terminal_service._memory_injected_terminals.discard("receiver")
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _id: {
        "tmux_session": "s", "tmux_window": "w",
    })
    provider = MagicMock()
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.3
    provider.composer_stash_keys = None
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda _id: provider)
    monkeypatch.setattr(terminal_service, "preserve_draft_before_send", lambda *_a: None)
    monkeypatch.setattr(terminal_service.status_monitor, "notify_input_sent", lambda _id: None)
    monkeypatch.setattr(terminal_service.status_monitor, "clear_rolling_buffer", lambda _id: None)
    backend = MagicMock()
    backend.send_keys.side_effect = RuntimeError("partial paste")
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    with pytest.raises(RuntimeError, match="partial paste"):
        terminal_service.send_prepared_input("receiver", "wire")
    assert "receiver" in terminal_service._memory_injected_terminals
    terminal_service._memory_injected_terminals.discard("receiver")
