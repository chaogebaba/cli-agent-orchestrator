"""F424/F426 mutation kills for inbox_service equality guards.

Targets surviving Eq_NotEq mutants from the f408 campaign (issues #279/#281):

P0
  1. _handle_wpm1_gate provider == "claude_code" (empty-metadata D1.1 + authoritative)
  2. deliver_pending FSM: state == "stop" and reason == "confirmation_timeout"
  3. _f136_run_callback_delivery batch.kind / result.kind / needs_wake
  4. reset_binding_episodes key[0] == terminal_id

P1
  8. _resolve_stale_binding_prior_hits refreshed == "hit"

WP-ARCH 3c K2 deleted ``teammate_push_service`` -- the legacy FILE carrier, its
``PushOutcome``, its ``NativeInboxWriteResult`` writer and the pull-mode
reconciler that drove it. Every kill above whose predicate lives in
``inbox_service`` survives and is asserted here unchanged; the kills that named
the writer's ``result.kind`` or the reconciler's mailbox filter lost their
predicate with it, and each removal is recorded in place below. Where an arm
merely referenced the deleted module to reach a surviving predicate (the
``_patch_delivery`` seat gate, the F136 claim/commit translation) it is
re-pointed, not removed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBatchResult,
    CallbackBatchRow,
    CallbackProgressResult,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    begin_delivery_attempt,
    clear_terminal_metadata_cache,
    create_inbox_message,
    create_terminal,
    get_pending_messages,
    settle_delivery_attempt,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import (
    InboxService,
    _IdentityAuthorityEpisode,
)
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation

_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _binding() -> TranscriptResolution:
    return TranscriptResolution(
        path=Path("/trace"),
        resolution_kind="binding",
        live_reference=TranscriptLiveReference(Path("/trace"), 1, 20),
    )


def _idle_observation() -> BoundaryObservation:
    return BoundaryObservation("f424-epoch", TerminalStatus.IDLE, 3, 1, 4, 2, 4)


@pytest.fixture
def f424_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f424.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions)
    clear_terminal_metadata_cache()
    from cli_agent_orchestrator.services import inbox_service as inbox_mod

    inbox_mod._failure_streaks.clear()
    yield sessions
    clear_terminal_metadata_cache()
    inbox_mod._failure_streaks.clear()
    engine.dispose()


def _seed_receiver(terminal_id: str, provider: str = "claude_code") -> None:
    create_terminal("caller-" + terminal_id[:8], "s", "caller", "codex")
    create_terminal("sender-" + terminal_id[:8], "s", "sender", "codex")
    create_terminal(terminal_id, "s", terminal_id, provider, caller_id="caller-" + terminal_id[:8])


def _ambiguous_on(
    sender: str,
    receiver: str,
    text: str = "wire",
    *,
    provider: str = "claude_code",
    extra_messages=None,
):
    create_inbox_message(sender, receiver, text)
    messages = get_pending_messages(receiver)
    if extra_messages:
        messages = list(messages) + list(extra_messages)
    message = messages[0] if extra_messages is None else messages
    batch = messages if extra_messages is not None else [messages[0]]
    attempt = begin_delivery_attempt(
        batch,
        receiver,
        provider,
        "digest",
        4,
        evidence=json.dumps(
            {"resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10}
        ),
    )
    settle_delivery_attempt(
        attempt,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
        evidence=json.dumps(
            {"resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10}
        ),
    )
    return (batch if extra_messages is not None else messages[0]), attempt


@contextmanager
def _patch_delivery(svc: InboxService, *, lookup=("unresolved", {}), merge=False):
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    provider.capabilities = MagicMock(accepts_input_while_processing=False)
    settle_calls: list[tuple[tuple, dict]] = []

    def _settle_spy(*args, **kwargs):
        settle_calls.append((args, kwargs))
        return "settled"

    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=_binding(),
        ),
        patch(
            "cli_agent_orchestrator.services.message_trace_service.continuity_aware_lookup",
            return_value=lookup,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
            return_value=merge,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
            side_effect=_settle_spy,
        ) as settle,
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            return_value="prepared",
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input"
        ) as send,
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
        patch.object(svc, "_commit_watchdog_ops"),
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager") as pm,
        # WP-ARCH 3c K8: deliver_pending's seat gate asks the fail-closed role
        # probe, not the deleted ``is_supervisor_mailbox_pull_terminal`` flag
        # helper. It is forced False so these arms exercise the WORKER path --
        # a True answer returns before the scan/inject seam and would make every
        # send-count assertion below trivially true.
        patch(
            "cli_agent_orchestrator.services.mailbox_service.probe_supervisor_role",
            return_value=False,
        ) as role_gate,
    ):
        pm.get_provider.return_value = provider
        observation = _idle_observation()
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_boundary_observation.return_value = observation
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE,
            {"result_status": "idle", "law_signal": {"class": "chrome"}},
        )
        yield SimpleNamespace(
            send=send,
            settle=settle,
            settle_calls=settle_calls,
            monitor=monitor,
            role_gate=role_gate,
        )


# ---------------------------------------------------------------------------
# P0 #1 — WPM1 provider equality
# ---------------------------------------------------------------------------


def _gate_message(msg_id: int = 1) -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id="sender",
        receiver_id="receiver",
        message="wire",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )


def _gate_attempt(*, provider: str = "claude_code") -> dict:
    return {
        "attempt_uuid": "a0",
        "provider": provider,
        "payload_hash": "digest",
        "outcome": "ambiguous",
        "reason": "confirmation_timeout",
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        "settled_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "prior_attempt_uuid": None,
        "evidence": json.dumps(
            {"resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10}
        ),
    }


@pytest.mark.parametrize(
    ("attempt_provider", "expect_receiver_gone"),
    [
        ("claude_code", True),
        ("codex", False),
        ("grok_cli", False),
    ],
)
def test_wpm1_empty_metadata_settles_receiver_gone_only_for_claude_attempts(
    attempt_provider, expect_receiver_gone
):
    """Kill [1753,53] provider == 'claude_code' on D1.1 empty-metadata arm.

    deliver_pending bails on empty metadata before the gate (F339), so this
    branch is only reachable through _handle_wpm1_gate — the production
    predicate itself.
    """
    svc = InboxService()
    attempt = _gate_attempt(provider=attempt_provider)
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[attempt]),
        patch(
            "cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
            return_value="settled",
        ) as settle,
        patch.object(svc, "_notify_delivery_failed") as notice,
    ):
        state, _ = svc._handle_wpm1_gate(
            "receiver",
            [_gate_message()],
            {},
            MagicMock(),
            "sender",
            OrchestrationType.SEND_MESSAGE,
        )
    if expect_receiver_gone:
        assert state == "stop"
        settle.assert_called_once_with(
            [1], MessageStatus.DELIVERY_FAILED, "receiver", reason="receiver_gone"
        )
        notice.assert_called_once_with("receiver", [1], reason="receiver_gone")
    else:
        assert state != "stop" or settle.call_count == 0
        settle.assert_not_called()
        notice.assert_not_called()


def test_wpm1_non_claude_metadata_is_not_authoritative(f424_db):
    """Kill [1769,37] metadata.get('provider') == 'claude_code'.

    Non-Claude metadata + non-Claude attempt must NOT take the authoritative
    WPM1 arm (no settle_wpm1). Mutant treats non-Claude as authoritative.
    Driven through deliver_pending on the real persistence path.
    """
    receiver = "f424-native"
    sender = "sender-" + receiver[:8]
    _seed_receiver(receiver, provider="codex")
    _ambiguous_on(sender, receiver, provider="codex")
    svc = InboxService()
    with _patch_delivery(svc, lookup=("hit", {"kind": "payload"})) as ctx:
        svc.deliver_pending(receiver)
    assert ctx.settle.call_count == 0
    wpm1_delivered = [
        args for args, kwargs in ctx.settle_calls if args[1] is MessageStatus.DELIVERED
    ]
    assert wpm1_delivered == []


def test_wpm1_claude_metadata_empty_attempts_mixed_provider_via_gate():
    """Mixed-provider attempts: settle receiver_gone iff a Claude row is present."""
    svc = InboxService()
    claude = _gate_attempt(provider="claude_code")
    native = _gate_attempt(provider="codex")
    native["attempt_uuid"] = "a1"
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[native, claude]),
        patch(
            "cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
            return_value="settled",
        ) as settle,
        patch.object(svc, "_notify_delivery_failed"),
    ):
        state, _ = svc._handle_wpm1_gate(
            "receiver",
            [_gate_message()],
            {},
            MagicMock(),
            "sender",
            OrchestrationType.SEND_MESSAGE,
        )
    assert state == "stop"
    settle.assert_called_once_with(
        [1], MessageStatus.DELIVERY_FAILED, "receiver", reason="receiver_gone"
    )


# ---------------------------------------------------------------------------
# P0 #2 — deliver_pending FSM stop + confirmation_timeout
# ---------------------------------------------------------------------------


def test_deliver_pending_stop_does_not_inject(f424_db):
    """Kill [2285,25] state == 'stop'. Hard abort must not fall through to inject."""
    receiver = "f424-stop"
    sender = "sender-" + receiver[:8]
    _seed_receiver(receiver, provider="claude_code")
    message, _attempt = _ambiguous_on(sender, receiver, provider="claude_code")
    svc = InboxService()
    gate_states: list[str] = []
    real_gate = svc._handle_wpm1_gate

    def _gate_spy(terminal_id, batch, *args, **kwargs):
        state, detail = real_gate(terminal_id, batch, *args, **kwargs)
        gate_states.append(state)
        return state, detail

    svc._handle_wpm1_gate = _gate_spy  # noqa: SLF001 — spy production scan/inject calls
    with _patch_delivery(svc, lookup=("unresolved", {}), merge=False) as ctx:
        svc.deliver_pending(receiver)
    assert gate_states == ["stop"], gate_states
    # Scan-loop abort must not fall through to the mailbox-channel / inject path.
    # (Inject has its own stop check at 2356, so send-count alone cannot kill 2285.)
    assert ctx.role_gate.call_count == 0
    assert ctx.send.call_count == 0
    with f424_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value


def test_deliver_pending_confirmation_timeout_groups_multi_sender_batch(f424_db):
    """Kill [2247,47] reason == 'confirmation_timeout'.

    One timeout attempt covers two different-sender rows. Protected grouping
    must feed both ids to the gate (exact-batch match → stop). Mutant groups
    by first sender only → no exact match → 'normal' → injects.
    """
    receiver = "f424-timeout"
    sender_a = "sender-a-" + receiver[:8]
    sender_b = "sender-b-" + receiver[:8]
    create_terminal("caller-" + receiver[:8], "s", "caller", "codex")
    create_terminal(sender_a, "s", sender_a, "codex")
    create_terminal(sender_b, "s", sender_b, "codex")
    create_terminal(receiver, "s", receiver, "claude_code", caller_id="caller-" + receiver[:8])
    create_inbox_message(sender_a, receiver, "wire-a")
    create_inbox_message(sender_b, receiver, "wire-b")
    messages = get_pending_messages(receiver)
    assert len(messages) == 2
    evidence = json.dumps({"resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10})
    attempt = begin_delivery_attempt(
        messages, receiver, "claude_code", "digest", 4, evidence=evidence
    )
    settle_delivery_attempt(
        attempt,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
        evidence=evidence,
    )
    svc = InboxService()
    gate_batches: list[list[int]] = []
    gate_states: list[str] = []
    real_gate = svc._handle_wpm1_gate

    def _gate_spy(terminal_id, batch, *args, **kwargs):
        gate_batches.append([m.id for m in batch])
        state, detail = real_gate(terminal_id, batch, *args, **kwargs)
        gate_states.append(state)
        return state, detail

    svc._handle_wpm1_gate = _gate_spy  # noqa: SLF001 — observe grouping
    with _patch_delivery(svc, lookup=("unresolved", {}), merge=False) as ctx:
        svc.deliver_pending(receiver)
    assert gate_batches, "gate was not invoked"
    assert sorted(gate_batches[0]) == sorted(m.id for m in messages)
    assert gate_states[0] == "stop"
    assert ctx.role_gate.call_count == 0
    assert ctx.send.call_count == 0


# ---------------------------------------------------------------------------
# P0 #3 — F136 callback-delivery kinds on the outcome dataclass
# ---------------------------------------------------------------------------


@dataclass
class _FakeInc:
    mailbox_id: str = "mb_f424"
    terminal_id: str = "t-f424"


@dataclass
class _FakeMailbox:
    id: str = "mb_f424"
    generation: int = 1
    session_name: str = "cao-f424"
    role: str = "supervisor"
    cc_inbox_path: str | None = "/tmp/f424-inbox.json"


def _f136_run_with_batch(batch, *, progress=None, heal=False):
    """Drive _f136_run_callback_delivery with a controlled batch/commit.

    Translates legacy CallbackBatchResult inputs into the F476
    claim_unnotified_wake / commit_wake API.

    WP-ARCH 3c K2 removed the third input this helper used to take. The runner's
    emit phase called ``teammate_push_service.write_supervisor_callback_notification``
    once per claimed row and counted the ``written`` kinds it returned; that
    module is deleted and the loop now counts the claimed rows themselves, so
    there is no writer left to seed with a ``write_results`` script.
    """
    from cli_agent_orchestrator.clients.database import WakeClaimResult, WakeCommitResult

    svc = InboxService()
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_db = MagicMock()
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_db)
    mock_session.__exit__ = MagicMock(return_value=False)

    # Decide what cc_inbox_path to provide based on batch.kind
    fake_cc_path: str | None = batch.inbox_path or "/tmp/f424-inbox.json"
    if batch.kind == "no_path":
        fake_cc_path = None

    fake_mailbox = _FakeMailbox()
    fake_mailbox.cc_inbox_path = fake_cc_path  # type: ignore[attr-defined]

    mock_db.query.return_value.filter_by.return_value.one_or_none.side_effect = [
        _FakeInc(terminal_id="t-f424"),
        fake_mailbox,
    ]

    # Translate batch into WakeClaimResult for the new F476 contract
    if batch.kind == "retryable_failure":
        claim_result = WakeClaimResult(
            kind="authority_lock_contention",
            rows=(),
            claimed_high_water=0,
            path_version=0,
            reason=batch.reason or "db_error: locked",
        )
    elif batch.kind == "no_path":
        # cc_inbox_path is None → triggers self-heal before claim is reached
        claim_result = WakeClaimResult(
            kind="claimed",
            rows=batch.rows,
            claimed_high_water=batch.cursor or 0,
            path_version=1,
            reason="ok",
        )
    else:
        claim_result = WakeClaimResult(
            kind="claimed",
            rows=batch.rows,
            claimed_high_water=batch.cursor or 0,
            path_version=batch.path_version if hasattr(batch, "path_version") else 1,
            reason="ok",
        )

    # commit_wake result — translate progress into the new form
    if progress and progress.kind == "path_changed":
        commit_result = WakeCommitResult(
            kind="path_changed",
            reason=progress.reason or "path_version_mismatch",
        )
    else:
        commit_result = WakeCommitResult(
            kind="committed",
            reason="ok",
        )

    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
            return_value=mock_lock,
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock",
            return_value=mock_lock,
        ),
        patch("cli_agent_orchestrator.clients.database.SessionLocal", return_value=mock_session),
        patch(
            "cli_agent_orchestrator.clients.database.claim_unnotified_wake",
            return_value=claim_result,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.commit_wake",
            return_value=commit_result,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=None,
        ),
        patch.object(svc, "_f150_self_heal_inbox_path", return_value=heal) as heal_spy,
    ):
        outcome = svc._f136_run_callback_delivery("t-f424")
    return outcome, heal_spy


def _ok_batch(*, rows, has_more=False, cursor=0, path="/tmp/f424-inbox.json"):
    return CallbackBatchResult(
        kind="ok",
        rows=tuple(rows),
        has_more=has_more,
        cursor=cursor,
        inbox_path=path,
        path_version=1,
        bootstrap_mode=None,
        reason="ok",
    )


def _row(row_id: int, tag: str = "forward") -> CallbackBatchRow:
    return CallbackBatchRow(
        inbox_row_id=row_id,
        sender_id="worker-1",
        message=f"msg-{row_id}",
        created_at=_NOW,
        tag=tag,
    )


def test_f136_retryable_failure_kind_returns_retryable_outcome():
    """Kill [870,26] batch.kind == 'retryable_failure'."""
    batch = CallbackBatchResult(
        kind="retryable_failure",
        rows=(),
        has_more=False,
        cursor=None,
        inbox_path=None,
        path_version=0,
        bootstrap_mode=None,
        reason="db_error: locked",
    )
    outcome, heal = _f136_run_with_batch(batch)
    assert outcome.retryable_failure_count == 1
    assert outcome.reason == "db_error: locked"
    assert outcome.written == 0
    assert outcome.reason != "empty"
    heal.assert_not_called()


def test_f136_no_path_kind_invokes_f150_self_heal():
    """Kill [876,26] batch.kind == 'no_path' (must attempt F150 self-heal)."""
    batch = CallbackBatchResult(
        kind="no_path",
        rows=(),
        has_more=False,
        cursor=5,
        inbox_path=None,
        path_version=0,
        bootstrap_mode=None,
        reason="no_inbox_path_configured",
    )
    outcome, heal = _f136_run_with_batch(batch, heal=False)
    # F150's self-heal is still ATTEMPTED, which is what this kill is about and
    # what the mutant it names would remove.
    heal.assert_called_once()
    # WP-ARCH 3b / A1.5: a missing content channel is no longer a REFUSAL.
    #
    # `cc_inbox_path` is K2's pull-mode file, written only when
    # supervisor.mailbox_pull is on — which A1.5 keeps at its shipped False. The
    # old `no_path` return left `written` at zero, held the doorbell's
    # `written > 0` gate shut, and left the seat neither pasted nor woken, which
    # is #604 arriving through the amendment written to end it. The run now
    # proceeds and the wake carries ids while the seat drains bodies by ack.
    assert outcome.reason != "no_path"
    assert outcome.retryable_failure_count == 0


def test_f136_committed_claim_counts_its_rows_and_reports_the_high_id():
    """Was: kill [985,31] ``result.kind == 'written'`` on the K2 writer.

    The writer is deleted, so that equality is gone -- but the two counters it
    fed are not, and they are what the rest of the system reads. ``written`` is
    the wake's own count of committed, wake-eligible rows (the doorbell gate
    used to read it; the delivery tick reads it now) and ``max_written_row_id``
    is the id the wake reports as covered. A mutant that drops either -- leaving
    ``written`` at zero, or the high id at zero -- still passes commit and still
    reports ``ok``, and a seat with rows would go unwoken, so the counters are
    asserted rather than the vanished equality.
    """
    batch = _ok_batch(rows=[_row(10, "forward")], cursor=0)
    outcome, _heal = _f136_run_with_batch(batch)
    assert outcome.written == 1
    assert outcome.reason == "ok"
    assert outcome.max_written_row_id == 10


def test_f136_path_changed_sets_needs_wake_and_reason():
    """Kill [1026,35] progress.kind == 'path_changed'."""
    batch = _ok_batch(rows=[_row(10, "forward")], cursor=0)
    outcome, _heal = _f136_run_with_batch(
        batch,
        progress=CallbackProgressResult(kind="path_changed", reason="path_version_mismatch"),
    )
    assert outcome.reason == "path_changed_during_run"
    assert outcome.needs_immediate_wake is True
    # F476: path_changed now detected at commit (before writes), so written=0
    assert outcome.written == 0


def test_f136_replay_tag_counts_only_replay_rows():
    """Kill [1042,65] r.tag == 'replay'.

    F476: replay rows are identified at claim/commit for replay-queue
    dequeue. The outcome written count includes replay rows (all rows
    get written to the native inbox).
    """
    batch = _ok_batch(
        rows=[_row(1, "forward"), _row(2, "forward"), _row(3, "replay")],
        cursor=5,
    )
    outcome, _heal = _f136_run_with_batch(batch)
    # All rows (including replay) are processed and counted
    assert outcome.written == 3
    assert outcome.selected == 3


# WP-ARCH 3c K2 removes ``test_f136_retryable_failures_do_not_immediate_wake``.
#
# Its whole premise was a PARTIAL run: one of the claimed rows fails to reach the
# carrier, so ``written < selected`` while the outcome still reports ``ok`` and
# declines an immediate re-wake. The only thing that could fail that way was
# ``write_supervisor_callback_notification`` returning ``retryable_failure`` --
# a lock timeout on K2's ``team-lead.json``. That file, its lockfile and its
# writer are deleted, and the emit loop that consumed the result is now a counter
# over rows the commit already accepted.
#
# So the divergence the arm existed to pin cannot arise: after a successful
# commit ``written`` equals ``selected`` by construction (the deadline break is
# the sole remaining gap, and it is a timing property, not an outcome kind).
# Keeping the arm would mean asserting ``written == selected`` and calling the
# tautology a mutation kill. What the arm genuinely guarded on the failure side
# -- that a retryable claim reports ``retryable_failure_count`` and does not
# advance -- is pinned by ``test_f136_retryable_failure_kind_returns_retryable_outcome``
# above, which survives because its failure comes from the claim, not the writer.


def test_f136_written_counter_on_the_real_persistence_path(f424_db, tmp_path):
    """Production-path written counter: real mailbox, real claim, real commit.

    This arm is the mocked helper's counterweight -- it drives
    ``claim_unnotified_wake``/``commit_wake`` against an actual SQLite mailbox
    instead of a translated ``CallbackBatchResult``, so it is the only place the
    counter is checked against the real cursor.

    It used to end on ``assert inbox_path.exists()``, because the run wrote K2's
    ``team-lead.json``. WP-ARCH 3c deleted that writer, so the file assertion is
    inverted rather than dropped: the mailbox still CARRIES a ``cc_inbox_path``
    (the column and its self-heal survive, and fx168 still compares it against
    terminal metadata), and the point worth pinning is that carrying the path no
    longer causes anything to be written to it. A rebuilt file carrier that
    quietly re-enabled itself would fail here.
    """
    inbox_path = tmp_path / "cc-inbox.json"
    terminal_id = "t-f424-write"
    mailbox_id = "mb_f424_write"
    with f424_db.begin() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="test",
                tmux_window=terminal_id,
                provider="claude_code",
                agent_profile="supervisor",
                lifecycle_generation=1,
            )
        )
        db.add(
            MailboxModel(
                id=mailbox_id,
                session_name="test-write",
                role="supervisor",
                current_terminal_id=terminal_id,
                generation=1,
                consumed_through_id=0,
                callback_notified_through_id=0,
                cc_inbox_path=str(inbox_path),
                cc_inbox_path_version=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id=mailbox_id,
                generation=1,
                terminal_id=terminal_id,
                published_at=_NOW,
            )
        )
        db.add(
            InboxModel(
                id=42,
                sender_id="worker-1",
                receiver_id=terminal_id,
                logical_receiver_id=mailbox_id,
                message="callback-done",
                orchestration_type="send_message",
                status="pending",
                enqueue_generation=1,
                created_at=_NOW,
            )
        )
    outcome = InboxService()._f136_run_callback_delivery(terminal_id)
    assert outcome.written == 1
    assert outcome.reason == "ok"
    assert outcome.max_written_row_id == 42
    # K2's carrier is gone: the path is configured and nothing writes to it.
    assert not inbox_path.exists()


# ---------------------------------------------------------------------------
# P0 #4 — reset_binding_episodes filter
# ---------------------------------------------------------------------------


def test_reset_binding_episodes_clears_only_named_terminal():
    """Kill [1425,73] key[0] == terminal_id. Mutant clears every OTHER terminal."""
    svc = InboxService()
    meta = {"tmux_session": "session"}
    svc._record_binding_authority_failure("term-a", 1, meta)
    svc._record_binding_authority_failure("term-b", 2, meta)
    svc._record_binding_authority_failure("term-b", 3, meta)
    assert ("term-a", "binding:1") in svc._binding_authority
    assert ("term-b", "binding:2") in svc._binding_authority
    assert ("term-b", "binding:3") in svc._binding_authority
    svc.reset_binding_episodes("term-a")
    assert ("term-a", "binding:1") not in svc._binding_authority
    assert ("term-b", "binding:2") in svc._binding_authority
    assert ("term-b", "binding:3") in svc._binding_authority
    # And the inverse: clearing B must leave A (re-seed A first).
    svc._record_binding_authority_failure("term-a", 1, meta)
    svc.reset_binding_episodes("term-b")
    assert ("term-a", "binding:1") in svc._binding_authority
    assert ("term-b", "binding:2") not in svc._binding_authority
    assert ("term-b", "binding:3") not in svc._binding_authority


def test_reset_binding_episodes_does_not_invent_identity_episodes():
    svc = InboxService()
    svc._binding_authority[("keep", "binding:9")] = _IdentityAuthorityEpisode(count=2)
    svc.reset_binding_episodes("other")
    assert ("keep", "binding:9") in svc._binding_authority


# ---------------------------------------------------------------------------
# P1 #8 — stale-binding refresh token
# ---------------------------------------------------------------------------


def test_stale_binding_refresh_token_hit_vs_miss():
    """Kill [1524,29] refreshed == 'hit'. Assert the returned token, not just settlement."""
    from cli_agent_orchestrator.services.message_trace_service import BindingStalenessObservation

    stale_path = Path("/old.jsonl")
    candidate = Path("/new.jsonl")
    stale_obs = BindingStalenessObservation(17, stale_path, True, (candidate,))
    prior = {
        "attempt_uuid": "a-stale",
        "payload_hash": "abc",
        "started_at": None,
    }
    prior_lookups = [(prior, "absent", {"resolution_kind": "binding"})]
    svc = InboxService()

    def _run(refreshed: str):
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service.observe_binding_absence",
                return_value=stale_obs,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_latest_compact_transcript_binding",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.scan_binding_candidates",
                return_value=("hit", {"kind": "candidate"}, candidate),
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.recover_transcript_binding_if_current",
                return_value="authority_changed",
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
                return_value=(refreshed, {"kind": "refresh", "token": refreshed}),
            ),
        ):
            return svc._resolve_stale_binding_prior_hits(
                "term-stale", {"id": "term-stale"}, prior_lookups
            )

    hit = _run("hit")
    assert hit is not None
    assert hit[0] == "hit"
    assert hit[1] is prior
    assert hit[2]["token"] == "hit"

    miss = _run("absent")
    assert miss is not None
    assert miss[0] == "authority_changed"
    assert miss[1] is None


# ---------------------------------------------------------------------------
# P1 #9 — REMOVED by WP-ARCH 3c K2.
#
# Two arms lived here, plus the ``_seed_supervisor_mailbox`` helper that built
# their two-mailbox fixture:
#
#   * ``test_reconcile_pull_mode_pending_count_filters_by_mailbox_id`` killed
#     [3401,68] ``logical_receiver_id == mb.id`` on the gate-5 pending count --
#     the mutant that made mailbox A's WARN report mailbox B's backlog.
#   * ``test_reconcile_pull_mode_push_selects_only_own_mailbox_rows`` was its
#     sibling on the push payload: rows selected for terminal A must be A's.
#
# Both drove ``InboxService.reconcile_pull_mode_notifications``, and that method
# is deleted. It was the fx158 pull-mode reconciler and all three of its gates
# went with K2 -- ``supervisor.mailbox_pull``, ``is_supervisor_mailbox_pull_terminal``
# and ``teammate_push_service._should_teammate_push`` -- so on top of the method
# being gone, every monkeypatch target these arms installed no longer resolves.
# The push they asserted (``attempt_teammate_push_reported`` returning a
# ``PushOutcome``) was the legacy second carrier, which is the thing the phase
# exists to remove; ``PushOutcome`` itself is deleted.
#
# There is no surviving predicate to re-point at. The equality these kills
# protected was a per-mailbox scoping filter inside the reconciler's own query,
# and the work the reconciler did for a seat holding rows the queue does not own
# is now done by the delivery tick's adoption pass, which hands those rows to
# the queue rather than pushing them down a second carrier. That pass has its
# own scoping and its own arms in ``test/app/delivery/test_adoption.py``; a
# filter mutant there is that file's kill to own, not a re-pointing of this one.
# ---------------------------------------------------------------------------
