import json
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.messages import messages
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
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog
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

    probe_result = monitor.probe_screen_status("worker")
    status, meta = probe_result.status, probe_result.meta

    assert status == TerminalStatus.IDLE
    assert meta["frame_source"] == "fresh_capture"
    assert "transient_api_error" not in meta
    backend.capture_viewport.assert_called_once()


def _armed(provider="codex"):
    service = StalledCallbackWatchdog(grace_seconds=3)
    service.record_inbound_task("worker", "caller", "developer")
    service.record_status("worker", TerminalStatus.IDLE, now=10.0)
    metadata = {
        "id": "worker",
        "caller_id": "caller",
        "provider": provider,
        "tmux_session": "cao-test",
        "tmux_window": "worker",
    }
    return service, metadata


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


# ``test_d5_reserved_and_auto_resumed_episodes_replace`` stood here, and it went
# with the fields it parametrised over. It asserted that a second inbound task
# REPLACES an episode rather than joining it when that episode is mid-auto-resume
# — ``resume_reserved_at`` set, or ``auto_resumed`` already true.
#
# WP-ARCH 3c slice 4 deletes the auto-resume machinery, and those two fields with
# it: they were written only inside ``collect_due_notifications``, the notifier
# K4 removes, so after the cut the join guard read them forever as None/False.
# The replace-vs-join rule itself is NOT gone and is not left unguarded — the two
# conditions that can still vary are covered by the arms immediately above
# (``callback_seen`` and ``fired``). What is gone is a third input to that rule
# that nothing can set any more.


# ---------------------------------------------------------------------------
# D5/D6 WATCHDOG AUTO-RESUME -- REMOVED by WP-ARCH 3c K4.
#
# Eight arms stood here. Every one of them observed through
# ``StalledCallbackWatchdog.collect_due_notifications``, and what they asserted
# about was the auto-resume action it took on the way:
#
#   * ``test_d5_kill_switch_preserves_ordinary_push`` -- with
#     ``CAO_WATCHDOG_AUTO_RESUME`` set to 0/false/" FALSE ", the fire degrades to
#     the ordinary idle notice instead of resuming;
#   * ``test_d6_non_codex_provider_uses_ordinary_push`` -- the same degradation
#     for any provider outside ``AUTO_RESUME_PROVIDERS``;
#   * ``test_d5_full_fire_inserts_exact_body_then_delivers`` -- the happy path
#     inserts exactly ``AUTO_RESUME_BODY`` and then requests delivery;
#   * ``test_d5_delivery_callback_runs_outside_watchdog_lock`` and
#     ``test_d5_delivery_callback_observes_actual_finalize`` -- the lock discipline
#     and the ordering of the delivery callback against the finalize;
#   * ``test_d5_auto_resume_is_one_shot_and_suffix_preserves_mark`` -- one resume
#     per episode, and the "(auto-resume attempted at ...)" suffix survives on the
#     next notice;
#   * ``test_d5_second_callback_read_cancels_pending_resume`` -- a callback that
#     lands between the reservation and the commit cancels the resume;
#   * ``test_d5_failed_before_commit_pushes_without_marking_auto_resumed`` -- a
#     failed insert must not leave the episode marked as resumed.
#
# K4 deletes the whole action: ``collect_due_notifications``, ``notify_due``,
# ``_push_notice``, ``_execute_auto_resume``, ``_reserve_chain_notice``,
# ``_release_chain_reservation`` and ``ReservedChainNotice``. There is no
# surviving caller to re-point at -- ``insert_watchdog_auto_resume_message`` has
# none left anywhere in the tree -- and no seam that still decides any of these
# questions. A kill switch with nothing to kill, a one-shot guard on an action
# that never runs, and a lock-ordering property between two calls that no longer
# both exist are not properties a test can hold onto.
#
# ``_patch_successful_auto_resume`` went with them; it existed only to stand up
# the resume path. ``_armed`` stays -- three surviving arms still use it for the
# episode bookkeeping that ``record_inbound_task``/``record_status`` own.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WPQ6 REASON COMPOSITION -- REMOVED by WP-ARCH 3c K4.
#
# Seven arms stood here. Each fed a codex pane frame through
# ``probe_screen_status``/``classify_screen`` and then asserted TWO things about
# ``collect_due_notifications``: the reason it composed into the notice, and
# whether it auto-resumed off the back of that reason.
#
#   * ``test_wpq6_a_g_capacity_auto_resumes_then_pushes_composed_reason`` -- the
#     "model is at capacity" banner on an otherwise idle pane resumes, and the
#     following notice carries "[reason: transient_api_error] (auto-resume
#     attempted at ...)";
#   * ``test_wpq6_c_excluded_collision_pushes_reason_without_auto_resume`` -- 429
#     / 502+403 / 400 banners name a reason but are excluded from resuming;
#   * ``test_wpq6_b_progress_frame_never_auto_resumes`` and
#     ``test_wpq6_b_processing_reason_is_gate_free_without_auto_resume`` -- a pane
#     still making progress reports its reason without resuming;
#   * ``test_wpq6_a2_ghost_composer_reason_does_not_relax_nudge_gate`` -- a ghost
#     composer draft does not soften the gate;
#   * ``test_wpq6_e_error_banner_pushes_reason_without_auto_resume``;
#   * ``test_wpq6_w_cropped_indented_capacity_never_auto_resumes`` -- an indented,
#     cropped capacity banner is not a resume trigger.
#
# Both halves of every arm landed on deleted code. The resume half is
# ``_execute_auto_resume``; the reason half is ``_push_notice``, which is where
# the "[reason: ...]" and "(auto-resume attempted at ...)" text was composed.
# There is no notice left to carry a reason.
#
# The CLASSIFIER these arms fed is a different thing and it is NOT deleted:
# ``CodexProvider.classify_screen`` and the transient-error keying still decide
# what a capacity banner, a 429 and a progress frame each mean, and the D4 band
# at the top of this file pins exactly that, frame by frame, without going
# through the watchdog at all. So the frames these arms were built around keep
# their coverage; what they lose is the consumer that used to act on them.
# ---------------------------------------------------------------------------


def test_d5_watchdog_sender_commit_does_not_rearm_episode(monkeypatch):
    service, _ = _armed()
    episode = service._episodes["worker"]
    before = (
        episode.generation,
        episode.revision,
        episode.inbound_at,
        episode.episode_started_wall_at,
        episode.last_join_wall_at,
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
    ) == before


# ---------------------------------------------------------------------------
# D5 AUTO-RESUME LOCK AND ORDER CONTRACTS -- REMOVED by WP-ARCH 3c K4.
#
# Six arms stood here, the concurrency half of the auto-resume path:
#
#   * ``test_d5_insert_and_second_callback_read_hold_delivery_lock`` -- the insert
#     and the re-read of callback state both happen under the terminal's
#     delivery_lock;
#   * ``test_d5_finalize_exits_while_delivery_lock_held`` and
#     ``test_d5_invalid_finalize_cancellation_exits_while_delivery_lock_held`` --
#     the finalize, and a cancellation of an invalid finalize, complete before the
#     lock is dropped;
#   * ``test_d5_insert_and_callback_reads_run_outside_watchdog_lock`` -- neither
#     touches the DB while ``_lock`` is held;
#   * ``test_d5_auto_resume_order_is_insert_finalize_then_deliver`` -- the three
#     steps happen in that order and no other;
#   * ``test_d5_second_callback_read_uses_frozen_episode_start_after_replace`` --
#     the re-read uses the episode start frozen at reservation time, not the
#     replacement episode's.
#
# These are the sharpest arms in the file and they are the most completely gone:
# a lock-ordering contract is a statement about two operations, and K4 deletes
# both operands. ``_execute_auto_resume`` was the only code that took the
# delivery_lock outside ``_lock``, inserted, finalized and then delivered; with
# it removed there is no sequence here to order and no lock interleaving to
# forbid.
#
# Worth stating plainly, because it is the one thing a reader should NOT conclude
# from this block: the delivery_lock itself is untouched and still contended.
# What no longer exists is a SECOND writer -- the watchdog -- contending for it.
# The rule these arms enforced (never hold ``_lock`` across a DB call) is now
# enforced by the watchdog having no DB-writing path left in its tick at all.
#
# The callback-fence arms immediately below are the surviving neighbours and are
# untouched: ``_bump_callback_fence`` is liveness bookkeeping, not part of the
# resume action, and it keeps both its callers and its arms.
# ---------------------------------------------------------------------------


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
