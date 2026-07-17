import json
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.messages import messages
from cli_agent_orchestrator.clients.database import WatchdogInsertResult
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services.inbox_service import (
    FirstLookupResult,
    InboxService,
    SuccessorCorroborationResult,
    SuccessorLookupPlan,
    corroborate_claude_successor,
)
from cli_agent_orchestrator.services.message_trace_service import transcript_lookup, wire_hash
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    AUTO_RESUME_BODY,
    StalledCallbackWatchdog,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def _plan(*, evidence=None, first_ref=("/tmp/transcript", 7, 10), attempt="a"):
    if evidence is None:
        evidence = {"last_observed_ref": {"path": "/tmp/transcript", "inode": 7, "size": 0}}
    return SuccessorLookupPlan(
        attempt_uuid=attempt,
        payload_hash="hash",
        started_at=datetime(2026, 7, 17),
        evidence_at_first_lookup=evidence,
        first_result=FirstLookupResult("absent", {}, {"id": "worker"}),
        first_ref=first_ref,
    )


def test_d1_older_attempt_hit_wins_with_returned_identity(monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.inbox_service.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
        MagicMock(
            side_effect=[
                ("absent", {"path": "/tmp/transcript", "inode": 7, "size": 10}),
                ("hit", {"kind": "transcript_queued_command", "offset": 4}),
            ]
        ),
    )
    result = corroborate_claude_successor((_plan(attempt="new"), _plan(attempt="old")))
    assert result.kind == "confirmed"
    assert result.hit_attempt_uuid == "old"
    assert result.hit_evidence == {"kind": "transcript_queued_command", "offset": 4}


@pytest.mark.parametrize(
    ("outcome", "observed", "first_ref"),
    [
        ("unresolved", {"kind": "transcript_unreadable"}, ("/tmp/transcript", 7, 10)),
        ("absent", {"path": "/tmp/transcript", "inode": 7, "size": 11}, ("/tmp/transcript", 7, 10)),
        ("absent", {"path": "/tmp/transcript", "inode": 8, "size": 10}, ("/tmp/transcript", 7, 10)),
        ("absent", {"path": "/tmp/transcript", "inode": 7, "size": 10}, None),
    ],
)
def test_d1_unresolved_or_changed_reference_defers(monkeypatch, outcome, observed, first_ref):
    monkeypatch.setattr("cli_agent_orchestrator.services.inbox_service.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
        lambda *_args: (outcome, observed),
    )
    assert corroborate_claude_successor((_plan(first_ref=first_ref),)).kind == "defer"


def test_d1_all_absent_identical_authorizes(monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.services.inbox_service.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
        lambda *_args: ("absent", {"path": "/tmp/transcript", "inode": 7, "size": 10}),
    )
    assert corroborate_claude_successor((_plan(),)).kind == "authorize"


def test_d1_plan_evidence_is_deep_copied_for_second_lookup(monkeypatch):
    source = {"last_observed_ref": {"path": "/tmp/transcript", "inode": 7, "size": 0}}
    plan = _plan(evidence=source)
    source["last_observed_ref"]["size"] = 999
    seen = []
    monkeypatch.setattr("cli_agent_orchestrator.services.inbox_service.time.sleep", lambda _: None)

    def lookup(_metadata, _payload_hash, _started_at, evidence):
        seen.append(evidence["last_observed_ref"]["size"])
        return "absent", {"path": "/tmp/transcript", "inode": 7, "size": 10}

    monkeypatch.setattr("cli_agent_orchestrator.services.inbox_service._wpm2_lookup", lookup)
    assert corroborate_claude_successor((plan,)).kind == "authorize"
    assert seen == [0]


def test_d1_real_caller_runs_one_corroboration_and_defer_never_opens(monkeypatch):
    service = InboxService()
    plan = _plan()
    message = SimpleNamespace(
        id=1,
        sender_id="sender",
        receiver_id="worker",
        message="payload",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        logical_receiver_id=None,
    )
    evidence = {
        "_wpm1_prior_attempt_uuid": "prior",
        "_successor_lookup_plans": (plan,),
        "boundary_authorized": "2026-07-17T00:00:00Z",
        "last_observed_ref": {"path": "/tmp/transcript", "inode": 7, "size": 10},
    }
    opener = MagicMock(return_value="new-attempt")
    corroborate = MagicMock(return_value=SuccessorCorroborationResult("defer"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
        lambda _terminal: {"id": "worker", "provider": "claude_code"},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
        lambda *_args, **_kwargs: [message],
    )
    monkeypatch.setattr(
        service, "_handle_wpm1_gate", lambda *_args, **_kwargs: ("inject", evidence)
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.count_ambiguous_attempts",
        lambda _ids: 0,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
        lambda _metadata: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.list_message_attempts", lambda _ids: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt", opener
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.corroborate_claude_successor",
        corroborate,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
        lambda *_args: "payload",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.status_monitor.get_status",
        lambda _terminal: TerminalStatus.IDLE,
    )

    service.deliver_pending("worker")

    corroborate.assert_called_once_with((plan,))
    opener.assert_not_called()


def test_d1_wpq2_stale_hit_precedes_final_corroboration(monkeypatch):
    service = InboxService()
    attempt = {
        "attempt_uuid": "prior",
        "payload_hash": "hash",
        "started_at": datetime(2026, 7, 17),
        "outcome": "ambiguous",
        "reason": "confirmation_timeout",
        "evidence": json.dumps(
            {
                "resolution_kind": "binding",
                "last_observed_ref": {
                    "path": "/trace",
                    "inode": 1,
                    "size": 10,
                    "resolution_kind": "binding",
                },
            }
        ),
    }
    corroborate = MagicMock(return_value=SuccessorCorroborationResult("authorize"))
    settle = MagicMock(return_value="settled")
    monkeypatch.setattr(service, "_exact_batch_attempts", lambda _ids: [attempt])
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
        lambda _metadata: SimpleNamespace(resolution_kind="binding"),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
        lambda *_args: (
            "absent",
            {
                "path": "/trace",
                "inode": 1,
                "size": 10,
                "last_observed_ref": {
                    "path": "/trace",
                    "inode": 1,
                    "size": 10,
                    "resolution_kind": "binding",
                },
            },
        ),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.advance_wpm2_continuity_cursor",
        lambda *_args: "already_advanced",
    )
    monkeypatch.setattr(
        service,
        "_resolve_stale_binding_prior_hits",
        lambda *_args: ("hit", attempt, {"kind": "transcript_user_turn"}, None),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch", settle
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.corroborate_claude_successor",
        corroborate,
    )

    state, detail = service._handle_wpm1_gate(
        "worker",
        [SimpleNamespace(id=1)],
        {"provider": "claude_code"},
        MagicMock(),
        "sender",
        OrchestrationType.SEND_MESSAGE,
    )

    assert (state, detail) == ("stop", None)
    corroborate.assert_not_called()
    settle.assert_called_once()


def test_d3_real_queued_command_multiline_hits_with_queue_corroboration(tmp_path):
    payload = "first line\nsecond line"
    path = tmp_path / "claude.jsonl"
    rows = [
        {"type": "queue-operation", "operation": "enqueue", "content": payload},
        {"attachment": {"type": "queued_command", "prompt": payload}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    outcome, evidence = transcript_lookup(path, wire_hash(payload), scan_from_start=True)
    assert outcome == "hit"
    assert evidence["kind"] == "transcript_queued_command"
    assert evidence["queue_corroboration"]["op"] == "enqueue"


def test_d3_queue_operations_alone_are_not_a_hit(tmp_path):
    payload = "queued body"
    path = tmp_path / "claude.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"type": "queue-operation", "operation": op, "content": payload})
            for op in ("enqueue", "remove")
        )
        + "\n",
        encoding="utf-8",
    )
    outcome, evidence = transcript_lookup(path, wire_hash(payload), scan_from_start=True)
    assert outcome == "absent"
    assert evidence["queue_corroboration"]["op"] == "remove"


@pytest.mark.parametrize(
    "error_row",
    [
        '{"error":{"type":"invalid_request_error"}}',
        "<html>400 Bad Request nginx/1.25</html>",
        "429 Too Many Requests",
        "stream disconnected",
    ],
)
def test_d4_incident_shapes_signal_only_on_strict_idle(error_row):
    provider = CodexProvider("worker", "session", "window")
    rows = [error_row, "› "]
    classification = provider.classify_screen(rows)
    assert classification.status == TerminalStatus.IDLE
    assert provider.transient_error_detected(rows, classification)


@pytest.mark.parametrize(
    "row",
    [
        "invalid_api_key and 400 Bad Request",
        "model_not_found from nginx",
        "429 Too Many Requests: quota exhausted",
        "stream error: content policy",
    ],
)
def test_d4_exclusions_veto_positive_rows(row):
    provider = CodexProvider("worker", "session", "window")
    rows = [row, "› "]
    assert not provider.transient_error_detected(rows, provider.classify_screen(rows))


@pytest.mark.parametrize("draft", ["› investigate nginx timeout", "› explain 429 rate limits"])
def test_d4_nonempty_draft_never_signals(draft):
    provider = CodexProvider("worker", "session", "window")
    rows = ["502 Bad Gateway", draft]
    assert not provider.transient_error_detected(rows, provider.classify_screen(rows))


@pytest.mark.parametrize(
    "quote",
    [
        "• nginx returned 502 Bad Gateway",
        "• stream disconnected",
        "• 429 Too Many Requests",
        "• invalid_request_error",
    ],
)
def test_d4_completed_quotes_never_signal(quote):
    provider = CodexProvider("worker", "session", "window")
    rows = [quote, "› "]
    classification = provider.classify_screen(rows)
    assert classification.status == TerminalStatus.COMPLETED
    assert not provider.transient_error_detected(rows, classification)


def test_d4_generic_json_with_idle_chrome_is_not_transient_error():
    provider = CodexProvider("worker", "session", "window")
    rows = ['{"event":"response.completed"}', "› "]
    classification = provider.classify_screen(rows)
    assert classification.status == TerminalStatus.IDLE
    assert not provider.transient_error_detected(rows, classification)


def test_d4_transport_uses_only_fresh_final_frame_for_transient_key(monkeypatch):
    monitor = StatusMonitor()
    monitor._screens["worker"] = (
        SimpleNamespace(display=["502 Bad Gateway", "› "], columns=80, lines=24),
        object(),
    )
    backend = MagicMock(supports_identity_readback=False)
    backend.capture_viewport.return_value = "all good\n› "
    backend.get_pane_size.return_value = (80, 24)
    provider = CodexProvider("worker", "session", "window")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _terminal: provider,
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _terminal: {"tmux_session": "session", "tmux_window": "window"},
    )

    status, meta = monitor.probe_screen_status("worker")

    assert status == TerminalStatus.IDLE
    assert meta["frame_source"] == "fresh_capture"
    assert "transient_api_error" not in meta
    backend.capture_viewport.assert_called_once()


def _armed(provider="codex"):
    service = StalledCallbackWatchdog(grace_seconds=3)
    service.record_inbound_task("worker", "caller", "developer")
    service.record_status("worker", TerminalStatus.IDLE, now=10.0)
    service._episodes["worker"].last_screen_fp = "stable"
    metadata = {"id": "worker", "caller_id": "caller", "provider": provider}
    return service, metadata


def _patch_successful_auto_resume(monkeypatch, service, metadata, deliver):
    delivery_lock = threading.Lock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: delivery_lock,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        lambda *_args: WatchdogInsertResult("inserted", 46),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending", deliver
    )
    return delivery_lock


def test_d5_join_bumps_revision_and_fired_replaces_generation():
    service, _ = _armed()
    first = service._episodes["worker"]
    service.record_inbound_task("worker", "caller", "developer")
    assert service._episodes["worker"] is first
    assert first.revision == 1
    first.fired = True
    service.record_inbound_task("worker", "caller", "developer")
    replacement = service._episodes["worker"]
    assert replacement is not first
    assert replacement.generation == first.generation + 1
    assert replacement.revision == 0


@pytest.mark.parametrize("field", ["resume_reserved_at", "auto_resumed"])
def test_d5_reserved_and_auto_resumed_episodes_replace(field):
    service, _ = _armed()
    first = service._episodes["worker"]
    setattr(first, field, 1.0 if field == "resume_reserved_at" else True)
    service.record_inbound_task("worker", "caller", "developer")
    assert service._episodes["worker"].generation == first.generation + 1


@pytest.mark.parametrize("disabled", ["0", "false", " FALSE "])
def test_d5_kill_switch_preserves_ordinary_push(monkeypatch, disabled):
    service, metadata = _armed()
    monkeypatch.setenv("CAO_WATCHDOG_AUTO_RESUME", disabled)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        lambda *_args: None,
    )
    due = service.collect_due_notifications(now=13.0)
    assert due == [
        ("worker", "caller", "[watchdog] worker worker (developer) idle 3s without callback")
    ]


def test_d6_non_codex_provider_uses_ordinary_push(monkeypatch):
    service, metadata = _armed(provider="grok_cli")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        lambda *_args: None,
    )
    assert len(service.collect_due_notifications(now=13.0)) == 1
    assert service._episodes["worker"].fired


def test_d5_full_fire_inserts_exact_body_then_delivers(monkeypatch):
    service, metadata = _armed()
    delivery_lock = threading.Lock()
    deliver = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        MagicMock(side_effect=[None, None]),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: delivery_lock,
    )
    insert = MagicMock(return_value=WatchdogInsertResult("inserted", 41))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        insert,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending", deliver
    )
    assert service.collect_due_notifications(now=13.0) == []
    insert.assert_called_once_with("worker", AUTO_RESUME_BODY)
    deliver.assert_called_once_with("worker")
    episode = service._episodes["worker"]
    assert episode.auto_resumed
    assert episode.resume_reserved_at is None


def test_d5_delivery_callback_runs_outside_watchdog_lock(monkeypatch):
    service, metadata = _armed()
    delivered = []

    def deliver(terminal_id):
        assert not service._lock._is_owned()
        assert not delivery_lock.locked()
        delivered.append(terminal_id)

    delivery_lock = _patch_successful_auto_resume(monkeypatch, service, metadata, deliver)

    assert service.collect_due_notifications(now=13.0) == []
    assert delivered == ["worker"]


def test_d5_delivery_callback_observes_actual_finalize(monkeypatch):
    service, metadata = _armed()
    finalized = []

    def deliver(terminal_id):
        episode = service._episodes[terminal_id]
        finalized.append(
            (
                episode.auto_resumed,
                episode.resume_reserved_at,
                episode.auto_resume_attempted_at,
                episode.idle_since,
            )
        )

    _patch_successful_auto_resume(monkeypatch, service, metadata, deliver)

    assert service.collect_due_notifications(now=13.0) == []
    assert len(finalized) == 1
    auto_resumed, reserved_at, attempted_at, idle_since = finalized[0]
    assert auto_resumed is True
    assert reserved_at is None
    assert attempted_at is not None
    assert idle_since == 13.0


def test_d5_auto_resume_is_one_shot_and_suffix_preserves_mark(monkeypatch):
    service, metadata = _armed()
    insert = MagicMock(return_value=WatchdogInsertResult("inserted", 45))
    deliver = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        insert,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending", deliver
    )

    assert service.collect_due_notifications(now=13.0) == []
    due = service.collect_due_notifications(now=16.0)

    assert len(due) == 1
    assert "auto-resume attempted at" in due[0][2]
    assert service._episodes["worker"].auto_resumed is True
    assert service._episodes["worker"].fired is True
    insert.assert_called_once_with("worker", AUTO_RESUME_BODY)
    deliver.assert_called_once_with("worker")


def test_d5_second_callback_read_cancels_pending_resume(monkeypatch):
    service, metadata = _armed()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        MagicMock(side_effect=[None, MessageStatus.PENDING]),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        lambda *_args: WatchdogInsertResult("inserted", 42),
    )
    cancel = MagicMock(return_value=True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.cancel_pending_watchdog_message",
        cancel,
    )
    assert service.collect_due_notifications(now=13.0) == []
    cancel.assert_called_once_with(42, "worker")
    assert not service._episodes["worker"].auto_resumed


def test_d5_failed_before_commit_pushes_without_marking_auto_resumed(monkeypatch):
    service, metadata = _armed()
    deliver = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        lambda *_args: WatchdogInsertResult("failed_before_commit"),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending", deliver
    )

    assert service.collect_due_notifications(now=13.0) == [
        ("worker", "caller", "[watchdog] worker worker (developer) idle 3s without callback")
    ]
    episode = service._episodes["worker"]
    assert episode.fired
    assert episode.auto_resumed is False
    assert episode.resume_reserved_at is None
    deliver.assert_not_called()


def test_d5_watchdog_sender_commit_does_not_rearm_episode(monkeypatch):
    service, _ = _armed()
    episode = service._episodes["worker"]
    before = (
        episode.generation,
        episode.revision,
        episode.inbound_at,
        episode.episode_started_wall_at,
        episode.last_join_wall_at,
        episode.idle_since,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog",
        service,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: None,
    )

    InboxService()._commit_watchdog_ops(
        "worker",
        "watchdog:worker",
        OrchestrationType.SEND_MESSAGE,
        {"caller_id": "caller", "agent_profile": "developer"},
    )

    current = service._episodes["worker"]
    assert current is episode
    assert (
        current.generation,
        current.revision,
        current.inbound_at,
        current.episode_started_wall_at,
        current.last_join_wall_at,
        current.idle_since,
    ) == before


def test_d5_insert_and_second_callback_read_hold_delivery_lock(monkeypatch):
    service, metadata = _armed()
    delivery_lock = threading.Lock()
    callback_reads = 0

    def callback_status(*_args):
        nonlocal callback_reads
        callback_reads += 1
        if callback_reads == 2:
            assert delivery_lock.locked()
        return None

    def insert(*_args):
        assert delivery_lock.locked()
        return WatchdogInsertResult("failed_before_commit")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        callback_status,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: delivery_lock,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        insert,
    )

    assert len(service.collect_due_notifications(now=13.0)) == 1
    assert callback_reads == 2
    assert not delivery_lock.locked()


def test_d5_finalize_exits_while_delivery_lock_held(monkeypatch):
    service, metadata = _armed()
    delivery_lock = threading.Lock()
    callback_reads = 0
    finalize_armed = False
    finalize_exits = []

    class FinalizeExitProbe:
        def __init__(self, lock):
            self._lock = lock

        def acquire(self, *args, **kwargs):
            return self._lock.acquire(*args, **kwargs)

        def release(self):
            return self._lock.release()

        def _is_owned(self):
            return self._lock._is_owned()

        def __enter__(self):
            self._lock.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            nonlocal finalize_armed
            try:
                if finalize_armed:
                    assert self._lock._is_owned()
                    assert delivery_lock.locked()
                    finalize_exits.append(True)
                    finalize_armed = False
            finally:
                self._lock.__exit__(exc_type, exc, traceback)
            return False

    def callback_status(*_args):
        nonlocal callback_reads, finalize_armed
        callback_reads += 1
        if callback_reads == 2:
            finalize_armed = True
        return None

    monkeypatch.setattr(service, "_lock", FinalizeExitProbe(service._lock))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        callback_status,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: delivery_lock,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        lambda *_args: WatchdogInsertResult("inserted", 47),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        lambda _terminal: None,
    )

    assert service.collect_due_notifications(now=13.0) == []
    assert callback_reads == 2
    assert finalize_exits == [True]
    assert not delivery_lock.locked()


def test_d5_insert_and_callback_reads_run_outside_watchdog_lock(monkeypatch):
    service, metadata = _armed()
    callback_reads = 0

    def callback_status(*_args):
        nonlocal callback_reads
        callback_reads += 1
        assert not service._lock._is_owned()
        return None

    def insert(*_args):
        assert not service._lock._is_owned()
        return WatchdogInsertResult("failed_before_commit")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        callback_status,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        insert,
    )

    assert len(service.collect_due_notifications(now=13.0)) == 1
    assert callback_reads == 2


def test_d5_auto_resume_order_is_insert_finalize_then_deliver(monkeypatch):
    service, metadata = _armed()
    events = []
    callback_reads = 0

    def callback_status(*_args):
        nonlocal callback_reads
        callback_reads += 1
        if callback_reads == 2:
            events.append("finalize")
        return None

    def insert(*_args):
        events.append("insert")
        return WatchdogInsertResult("inserted", 44)

    def deliver(*_args):
        events.append("deliver")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        callback_status,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        insert,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending", deliver
    )

    assert service.collect_due_notifications(now=13.0) == []
    assert events == ["insert", "finalize", "deliver"]


def test_d5_second_callback_read_uses_frozen_episode_start_after_replace(monkeypatch):
    service, metadata = _armed()
    old_started = service._episodes["worker"].episode_started_wall_at
    callback_starts = []
    cancel = MagicMock(return_value=True)

    def callback_status(_terminal, _caller, since):
        callback_starts.append(since)
        return None

    def insert(*_args):
        service.record_inbound_task("worker", "caller", "developer")
        service._episodes["worker"].episode_started_wall_at = datetime(2030, 1, 1)
        return WatchdogInsertResult("inserted", 43)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _terminal: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
        callback_status,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
        lambda _terminal: (TerminalStatus.IDLE, {"transient_api_error": True}),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
        lambda _terminal: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
        lambda _terminal: threading.Lock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.insert_watchdog_auto_resume_message",
        insert,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.cancel_pending_watchdog_message",
        cancel,
    )

    assert service.collect_due_notifications(now=13.0) == []
    assert callback_starts == [old_started, old_started]
    assert service._episodes["worker"].episode_started_wall_at != old_started
    cancel.assert_called_once_with(43, "worker")


def test_d5_callback_fence_holds_lock_until_commit_resolution(monkeypatch):
    service = StalledCallbackWatchdog()
    guard_entered = threading.Event()
    resolve_commit = threading.Event()
    lock_attempted = threading.Event()
    competing_acquired = threading.Event()

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.terminal_exists",
        lambda _sender: True,
    )

    def guarded_insert():
        with service.callback_insert_guard("worker"):
            guard_entered.set()
            assert resolve_commit.wait(1)

    def competing_watchdog_step():
        assert guard_entered.wait(1)
        lock_attempted.set()
        with service._lock:
            competing_acquired.set()

    insert_thread = threading.Thread(target=guarded_insert)
    competitor_thread = threading.Thread(target=competing_watchdog_step)
    insert_thread.start()
    competitor_thread.start()
    try:
        assert lock_attempted.wait(1)
        assert not competing_acquired.wait(0.1)
    finally:
        resolve_commit.set()
        insert_thread.join(1)
        competitor_thread.join(1)

    assert not insert_thread.is_alive()
    assert not competitor_thread.is_alive()
    assert competing_acquired.is_set()


def test_d5_callback_fence_bumps_before_body_and_never_rolls_back(monkeypatch):
    service = StalledCallbackWatchdog()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.terminal_exists",
        lambda _sender: True,
    )
    with pytest.raises(RuntimeError):
        with service.callback_insert_guard("worker"):
            assert service._callback_fences["worker"] == 1
            raise RuntimeError("rollback")
    assert service._callback_fences["worker"] == 1


def test_d5_watchdog_sender_never_bumps_fence(monkeypatch):
    service = StalledCallbackWatchdog()
    exists = MagicMock(return_value=True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.terminal_exists", exists
    )
    with service.callback_insert_guard("watchdog:worker"):
        pass
    assert service._callback_fences == {}
    exists.assert_not_called()


def test_cancelled_cli_filter_is_forwarded():
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = []
    runner = CliRunner()
    with patch(
        "cli_agent_orchestrator.cli.commands.messages.requests.get", return_value=response
    ) as request:
        result = runner.invoke(messages, ["list", "--to", "worker", "--status", "cancelled"])
    assert result.exit_code == 0
    assert request.call_args.kwargs["params"]["status"] == "cancelled"
