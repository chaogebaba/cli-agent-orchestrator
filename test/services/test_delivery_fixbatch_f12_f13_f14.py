"""Acceptance coverage for the F12/F13/F14 delivery-engine fixbatch."""

from __future__ import annotations

import json
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxModel,
    begin_delivery_attempt,
    begin_delivery_attempt_if_no_other_delivering,
    create_inbox_message,
    get_message_trace,
    list_ready_backlog_observations,
    make_admission_proof,
    settle_delivery_attempt,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services import draft_guard, terminal_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.inbox_service import InboxService, begin_delivery_attempt
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
    transcript_ref,
    wire_hash,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def delivery_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'delivery-fixbatch.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.create_terminal("caller", "s", "caller", "codex")
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "grok_cli", caller_id="caller")
    yield sessions
    engine.dispose()


def _grok_provider() -> GrokCliProvider:
    return object.__new__(GrokCliProvider)


def _grok_screen(draft: str) -> str:
    return "\n".join(
        [
            "   • Working (8s • esc to interrupt)",
            "   prior output",
            f"   ❯ {draft}",
            "   Composer 2.5 · always-approve · ctrl+o transcript",
        ]
    )


def _terminal_send(
    monkeypatch,
    backend,
    provider,
    history,
):
    metadata = {"tmux_session": "cao-test", "tmux_window": "grok"}
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 2, None, 2)
    backend.get_history.side_effect = history
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _tid: metadata)
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda _tid: provider)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(draft_guard, "get_backend", lambda: backend)
    monkeypatch.setattr(draft_guard.status_monitor, "get_rendered_screen", lambda _tid: None)
    monkeypatch.setattr(terminal_service.status_monitor, "notify_input_sent", lambda _tid: None)
    monkeypatch.setattr(
        terminal_service.status_monitor, "clear_rolling_buffer", lambda _tid, _p=None: None
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "mark_injection_completed",
        lambda _tid: observation,
    )
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _tid: None)
    monkeypatch.setattr(draft_guard.time, "sleep", lambda _seconds: None)
    return terminal_service.send_prepared_input("receiver", "CAO_MESSAGE")


def test_f12_provider_capability_is_codex_only():
    assert CodexProvider.clear_immune_ghosts is True
    assert GrokCliProvider.clear_immune_ghosts is False


@pytest.mark.parametrize(
    "history",
    [
        ["busy output without a composer footer"],
        [_grok_screen("HUMAN_DRAFT"), _grok_screen("HUMAN_DRAFT"), _grok_screen("HUMAN_DRAFT")],
        [
            _grok_screen("HUMAN_DRAFT"),
            _grok_screen("HUMAN_DRAFT"),
            _grok_screen("PARTIAL"),
            _grok_screen("PARTIAL"),
        ],
    ],
    ids=["parser-none", "unchanged-clear", "failed-clear"],
)
def test_f12_grok_uncertainty_never_reaches_message_paste(monkeypatch, history):
    backend = MagicMock()
    monkeypatch.setattr(draft_guard, "DRAFT_CLEAR_MAX_ITERATIONS", 1)

    with pytest.raises(DeliveryDeferredError):
        _terminal_send(monkeypatch, backend, _grok_provider(), history)

    backend.send_keys.assert_not_called()


def test_f12_stable_grok_draft_sends_clean_turn_then_restores_without_enter(monkeypatch):
    backend = MagicMock()
    draft = _grok_screen("HUMAN_DRAFT")
    empty = _grok_screen("")

    _terminal_send(monkeypatch, backend, _grok_provider(), [draft, draft, empty, empty])

    assert backend.send_keys.call_count == 2
    message_call, restore_call = backend.send_keys.call_args_list
    assert message_call.args[2] == "CAO_MESSAGE"
    assert message_call.kwargs["enter_count"] == 1
    assert restore_call.args[2] == "HUMAN_DRAFT"
    assert restore_call.kwargs["enter_count"] == 0


def _resolution(tmp_path: Path) -> TranscriptResolution:
    path = tmp_path / "grok-transcript.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    stat = path.stat()
    return TranscriptResolution(
        path,
        "exact_id",
        stat.st_ino,
        live_reference=TranscriptLiveReference(path, stat.st_ino, stat.st_size),
    )


def _ambiguous(message, payload: str, *, evidence: dict | None = None) -> str:
    attempt = begin_delivery_attempt(
        [message],
        "receiver",
        "grok_cli",
        wire_hash(payload),
        len(payload),
        evidence=json.dumps(evidence or {}),
    )
    settle_delivery_attempt(
        attempt,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
        evidence=json.dumps(evidence or {}),
    )
    return attempt


def _deliver_with_fakes(
    service: InboxService,
    *,
    resolution: TranscriptResolution | None,
    lookup_results,
    confirm_result=("absent", {"kind": "transcript_absent"}),
):
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 3, 1, 4, 2, 4)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = 1
    monitor.get_status_gen.return_value = 3
    monitor.probe_screen_status.return_value = (
        TerminalStatus.IDLE,
        {"result_status": "idle", "law_signal": {"class": "chrome"}},
    )
    wires = []

    def send(_terminal_id, wire, **kwargs):
        wires.append(wire)
        if "on_submitted" in kwargs:
            kwargs["on_submitted"](observation)
        return observation

    stack = ExitStack()
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=resolution,
        )
    )
    lookup = stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
            side_effect=lookup_results,
        )
    )
    stack.enter_context(
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor)
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            return_value="payload",
        )
    )
    paste = stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        )
    )
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=confirm_result,
        )
    )
    # Trigger the legacy_test_seam path (patching begin_delivery_attempt makes
    # `begin_delivery_attempt is not _PRODUCTION_BEGIN_DELIVERY_ATTEMPT` True)
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
            side_effect=begin_delivery_attempt,
        )
    )
    return stack, lookup, paste, wires


def test_f13_f14_resolution_object_normalized_third_tagged_attempt_and_cap_unblocks(
    delivery_db, tmp_path
):
    message = create_inbox_message("sender", "receiver", "payload")
    first = _ambiguous(message, "payload")
    second = _ambiguous(message, "payload")
    resolution = _resolution(tmp_path)
    service = InboxService()

    stack, lookup, paste, wires = _deliver_with_fakes(
        service,
        resolution=resolution,
        lookup_results=[("unresolved", {}), ("unresolved", {})],
    )
    with stack:
        service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    assert paste.call_count == 1 and lookup.call_count == 2
    assert len(trace["attempts"]) == 3
    third = trace["attempts"][-1]
    assert third["prior_attempt_uuid"] == second
    assert wires == [
        f"[redelivery of attempt {second[:8]} - prior delivery unconfirmed; "
        "ignore if already received]\npayload"
    ]
    assert third["evidence"]["path"] == str(resolution.path)
    assert third["evidence"]["resolution_kind"] == "exact_id"
    assert third["evidence"]["redelivery_tag"]["prior_attempt_uuid"] == second
    assert first != second

    stack, lookup, paste, _ = _deliver_with_fakes(
        service,
        resolution=resolution,
        lookup_results=[("unresolved", {})] * 3,
    )
    with stack:
        service.deliver_pending("receiver")
    assert lookup.call_count == 3
    paste.assert_not_called()
    assert get_message_trace(message.id)["message"]["status"] == "delivery_failed"

    # F401: Create next_message AFTER the first message is fully settled
    # (delivery_failed). Earlier creation caused a batch membership mismatch
    # because deliver_pending batches all pending messages together.
    next_message = create_inbox_message("sender", "receiver", "next")

    stack, _, paste, wires = _deliver_with_fakes(
        service,
        resolution=resolution,
        lookup_results=[],
        confirm_result=("unverified", {}),
    )
    with stack:
        service.deliver_pending("receiver")
    assert paste.call_count == 1 and wires == ["payload"]
    assert get_message_trace(next_message.id)["message"]["status"] == "delivered"


def test_f13_all_prior_attempts_scanned_and_later_hit_suppresses(delivery_db, tmp_path):
    message = create_inbox_message("sender", "receiver", "payload")
    _ambiguous(message, "payload")
    second = _ambiguous(message, "payload")
    service = InboxService()
    stack, lookup, paste, _ = _deliver_with_fakes(
        service,
        resolution=_resolution(tmp_path),
        lookup_results=[("unresolved", {}), ("hit", {"kind": "exact_id"})],
    )
    with stack:
        service.deliver_pending("receiver")

    assert lookup.call_count == 2
    paste.assert_not_called()
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == "delivered"
    assert trace["attempts"][-1]["attempt_uuid"] == second


def test_f14_no_transcript_oracle_still_opens_visible_tag(delivery_db):
    message = create_inbox_message("sender", "receiver", "payload")
    prior = _ambiguous(message, "payload")
    stack, _, paste, wires = _deliver_with_fakes(
        InboxService(),
        resolution=None,
        lookup_results=[("unresolved", {})],
    )
    with stack:
        InboxService().deliver_pending("receiver")

    assert paste.call_count == 1
    assert wires[0].startswith(f"[redelivery of attempt {prior[:8]} ")
    trace = get_message_trace(message.id)
    assert trace["attempts"][-1]["prior_attempt_uuid"] == prior


def test_f14_concurrent_tagged_admission_opens_exactly_one_successor(delivery_db):
    message = create_inbox_message("sender", "receiver", "payload")
    prior = _ambiguous(message, "payload")
    proof = make_admission_proof("tagged_replay", [message.id], prior)
    barrier = threading.Barrier(2)
    results = []

    def open_candidate():
        barrier.wait()
        results.append(
            begin_delivery_attempt_if_no_other_delivering(
                [message],
                "receiver",
                "grok_cli",
                "tagged",
                6,
                prior_attempt_uuid=prior,
                admission_proof=proof,
            )
        )

    threads = [threading.Thread(target=open_candidate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    assert sum(result.kind == "opened" for result in results) == 1
    assert len(get_message_trace(message.id)["attempts"]) == 2


def test_f14_pre_paste_successor_is_retryable_but_post_paste_has_no_sibling(delivery_db):
    message = create_inbox_message("sender", "receiver", "payload")
    prior = _ambiguous(message, "payload")
    first_proof = make_admission_proof("tagged_replay", [message.id], prior)
    first = begin_delivery_attempt_if_no_other_delivering(
        [message],
        "receiver",
        "grok_cli",
        "tagged-1",
        8,
        prior_attempt_uuid=prior,
        admission_proof=first_proof,
    )
    assert first.kind == "opened"
    settle_delivery_attempt(
        first.attempt_uuid,
        MessageStatus.PENDING,
        "deferred",
        reason="delivery_deferred",
    )

    retry = begin_delivery_attempt_if_no_other_delivering(
        [message],
        "receiver",
        "grok_cli",
        "tagged-2",
        8,
        prior_attempt_uuid=prior,
        admission_proof=make_admission_proof("tagged_replay", [message.id], prior),
    )
    assert retry.kind == "opened"
    settle_delivery_attempt(
        retry.attempt_uuid,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
    )

    sibling = begin_delivery_attempt_if_no_other_delivering(
        [message],
        "receiver",
        "grok_cli",
        "sibling",
        7,
        prior_attempt_uuid=prior,
        admission_proof=make_admission_proof("tagged_replay", [message.id], prior),
    )
    assert sibling.kind == "stale_admission"


def test_f14_three_ambiguities_atomically_block_fourth_tagged_open(delivery_db):
    message = create_inbox_message("sender", "receiver", "payload")
    first = _ambiguous(message, "payload")
    second = begin_delivery_attempt_if_no_other_delivering(
        [message],
        "receiver",
        "grok_cli",
        "tagged-2",
        8,
        prior_attempt_uuid=first,
        admission_proof=make_admission_proof("tagged_replay", [message.id], first),
    )
    assert second.kind == "opened"
    settle_delivery_attempt(
        second.attempt_uuid,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
    )
    third = begin_delivery_attempt_if_no_other_delivering(
        [message],
        "receiver",
        "grok_cli",
        "tagged-3",
        8,
        prior_attempt_uuid=second.attempt_uuid,
        admission_proof=make_admission_proof("tagged_replay", [message.id], second.attempt_uuid),
    )
    assert third.kind == "opened"
    settle_delivery_attempt(
        third.attempt_uuid,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
    )

    fourth = begin_delivery_attempt_if_no_other_delivering(
        [message],
        "receiver",
        "grok_cli",
        "tagged-4",
        8,
        prior_attempt_uuid=third.attempt_uuid,
        admission_proof=make_admission_proof("tagged_replay", [message.id], third.attempt_uuid),
    )
    assert fourth.kind == "stale_admission"
    assert len(get_message_trace(message.id)["attempts"]) == 3


# ---------------------------------------------------------------------------
# F13/F353 ready-backlog ALERT arms -- REMOVED by WP-ARCH 3c K4.
#
# Four arms stood here, all driving ``StalledCallbackWatchdog.tick_ready_backlog``:
#
#   * ``test_f13_ready_backlog_alert_once_and_never_retries_receiver`` -- the
#     alert composes once per receiver past its grace and never re-fires for the
#     same receiver;
#   * ``test_f13_ready_backlog_suppressed_while_open_and_progress_resets_clock``
#     -- an open delivering attempt suppresses the alert, and a fingerprint change
#     (progress) restarts the grace clock;
#   * ``test_f353_ready_backlog_no_alert_when_delivery_busy_gate_says_busy`` and
#     ``test_f353_ready_backlog_alert_fires_when_deliverable_and_unattempted`` --
#     the F353 busy gate, read through ``get_boundary_observation``, decides
#     between silence and an alert.
#
# K4 deletes ``tick_ready_backlog`` with the other four muted ticks and takes
# ``collect_due_notifications``/``_push_notice`` -- the composer every one of
# these arms observed through -- with it. Nothing here survives the cut: the
# grace clock, the once-per-receiver dedup key and the busy gate were all fields
# and branches INSIDE the deleted tick, not shared helpers it called.
#
# What does survive is the OBSERVATION the tick consumed, and the arm below keeps
# pinning it: ``list_ready_backlog_observations`` still reports the receiver, the
# oldest pending row, its age, whether an attempt is open, and the coalescing
# fingerprint. That is the half with a live reader today -- the delivery tick's
# adoption pass (``test/app/delivery/test_adoption.py``) is what acts on a
# stranded PENDING row now, and it acts by adopting the row rather than by
# alerting a caller about it.
#
# ``_backlog_observation`` and ``_boundary_observation`` went with the arms. The
# surviving arm builds real rows through ``begin_delivery_attempt`` rather than
# stubbing an observation, so it never needed either.
# ---------------------------------------------------------------------------


def test_f13_backlog_fingerprint_observes_coalesced_deferred_last_at(delivery_db):
    message = create_inbox_message("sender", "receiver", "payload")
    first = begin_delivery_attempt([message], "receiver", "grok_cli", "same", 4)
    settle_delivery_attempt(first, MessageStatus.PENDING, "deferred", reason="delivery_deferred")
    before = list_ready_backlog_observations()[0].attempt_fingerprint
    time.sleep(0.001)
    second = begin_delivery_attempt([message], "receiver", "grok_cli", "same", 4)
    settle_delivery_attempt(second, MessageStatus.PENDING, "deferred", reason="delivery_deferred")
    after = list_ready_backlog_observations()[0].attempt_fingerprint

    assert before[:3] == after[:3]
    assert before[3] != after[3]
    with delivery_db() as db:
        assert db.query(InboxDeliveryAttemptModel).count() == 1
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value
