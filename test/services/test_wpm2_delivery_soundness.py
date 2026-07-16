"""Frozen WPM2 proof boundaries and mutation-killing evidence."""

import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base, InboxDeliveryAttemptModel, InboxModel, WPM2_CURSOR_VERSION,
    advance_wpm2_continuity_cursor, begin_delivery_attempt,
    begin_delivery_attempt_if_no_other_delivering, create_inbox_message,
    create_transcript_binding, get_message_trace, get_pending_messages,
    list_delivering_attempts_for_terminal, list_stale_open_claude_attempts,
    make_admission_proof, recover_wpm2_stale_attempt, settle_delivery_attempt,
    settle_delivery_attempt_proof_safe, settle_wpm1_terminal_batch,
    record_wpm1_stalled_notice,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services.inbox_service import (
    InboxService, WPM2_STALE_OPEN_AGE_SECONDS, _wpm2_lookup,
    classify_permanently_d2_only,
    get_delivery_lock,
)
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.message_trace_service import (
    MAX_IN_TXN_TRANSCRIPT_DELTA_BYTES, bounded_transcript_suffix_lookup,
    TranscriptLiveReference, TranscriptResolution, transcript_lookup, transcript_ref,
    wire_hash, wpm2_cursor_baseline,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation, StatusMonitor

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def wpm2_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wpm2.sqlite'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.create_terminal("caller", "s", "caller", "codex")
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code", caller_id="caller")
    yield sessions
    engine.dispose()


def _cursor(size=10):
    return {"path": "/trace", "inode": 7, "size": size,
            "resolution_kind": "binding", "cursor_version": WPM2_CURSOR_VERSION}


def _attempt(evidence, *, outcome="ambiguous", reason="confirmation_timeout"):
    return {"outcome": outcome, "reason": reason, "evidence": evidence}


@pytest.mark.parametrize(("evidence", "reason"), [
    ("not-json", "anchor_missing"), ("[]", "anchor_missing"),
    ("{}", "anchor_missing"),
    (json.dumps({"injection_completed_seq": None}), "anchor_missing"),
    (json.dumps({"injection_completed_seq": {"observation_epoch": "", "seq": 1}}),
     "anchor_missing"),
    (json.dumps({"injection_completed_seq": {"observation_epoch": "a", "seq": True}}),
     "anchor_missing"),
    (json.dumps({"injection_completed_seq": {"observation_epoch": "a", "seq": 1}}),
     "epoch_mismatch"),
    (json.dumps({"injection_completed_seq": {"observation_epoch": "b", "seq": 1},
                 "busy_initial_submit": {}}), "busy_initial"),
])
def test_wpm2_permanent_d2_classifier_validation_matrix(evidence, reason):
    assert classify_permanently_d2_only(_attempt(evidence), "b") == reason


def test_wpm2_old_token_snapshot_cannot_qualify_current_monitor():
    monitor = StatusMonitor()
    before = monitor.get_boundary_observation("t")
    monitor.reset_buffer("t")
    after = monitor.get_boundary_observation("t")
    assert before.observation_epoch != after.observation_epoch


def test_wpm2_mixed_token_latches_cannot_qualify():
    evidence = json.dumps({"injection_completed_seq": {
        "observation_epoch": "old", "seq": 100}})
    assert classify_permanently_d2_only(_attempt(evidence), "current") == "epoch_mismatch"


def test_wpm2_s4_preflight_and_post_open_allow_only_created_attempt(wpm2_db):
    first = create_inbox_message("sender", "receiver", "one")
    proof = make_admission_proof("s4_initial", [first.id])
    result = begin_delivery_attempt_if_no_other_delivering(
        [first], "receiver", "claude_code", "h1", 3, admission_proof=proof)
    assert result.kind == "opened" and result.attempt_uuid
    second = create_inbox_message("sender", "receiver", "two")
    blocked = begin_delivery_attempt_if_no_other_delivering(
        [second], "receiver", "claude_code", "h2", 3,
        admission_proof=make_admission_proof("ordinary", [second.id]))
    assert blocked.kind == "delivering_conflict"


def test_wpm2_d2_confirm_vs_open_both_commit_orders(wpm2_db):
    for order in ("settlement_first", "open_first"):
        message, prior = _settled(wpm2_db, text=f"d2-{order}")
        proof = make_admission_proof("ordinary", [message.id])
        start = threading.Barrier(2)
        first_done = threading.Event()
        results = {}
        sends = []

        def settle():
            with wpm2_db() as db:
                assert db.get(InboxModel, message.id) is not None
            start.wait()
            if order == "open_first":
                assert first_done.wait(5)
            results["settle"] = settle_wpm1_terminal_batch(
                [message.id], MessageStatus.DELIVERED, "receiver",
                confirmation_evidence=(prior, {"kind": "transcript_queued_command"}))
            if order == "settlement_first":
                first_done.set()

        def open_candidate():
            with wpm2_db() as db:
                assert db.get(InboxModel, message.id) is not None
            start.wait()
            if order == "settlement_first":
                assert first_done.wait(5)
            opened = begin_delivery_attempt_if_no_other_delivering(
                [message], "receiver", "claude_code", "h", 3,
                admission_proof=proof)
            results["open"] = opened.kind
            if opened.kind == "opened":
                sends.append(opened.attempt_uuid)
            if order == "open_first":
                first_done.set()

        threads = [threading.Thread(target=settle), threading.Thread(target=open_candidate)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        if order == "settlement_first":
            assert results == {"settle": "settled", "open": "stale_admission"}
            assert sends == []
            expected_status = MessageStatus.DELIVERED.value
        else:
            assert results == {"open": "opened", "settle": "stale"}
            assert len(sends) == 1
            expected_status = MessageStatus.DELIVERING.value
        with wpm2_db() as db:
            assert db.get(InboxModel, message.id).status == expected_status


@pytest.mark.parametrize("superset_first", [True, False])
def test_wpm2_superset_attempt_blocks_subset_initial_repaste(wpm2_db, superset_first):
    m1 = create_inbox_message("sender", "receiver", "one")
    m2 = create_inbox_message("sender", "receiver", "two")
    original = [m1, m2] if superset_first else [m1]
    attempt = begin_delivery_attempt(original, "receiver", "claude_code", "h", 3)
    settle_delivery_attempt(attempt, MessageStatus.PENDING, "ambiguous",
                            reason="confirmation_timeout", evidence="{}")
    candidate = [m1] if superset_first else get_pending_messages("receiver", limit=2)
    proof = make_admission_proof("s4_initial", [item.id for item in candidate])
    result = begin_delivery_attempt_if_no_other_delivering(
        candidate, "receiver", "claude_code", "h2", 3, admission_proof=proof)
    assert result.kind == "stale_admission"


def test_wpm2_subset_attempt_blocks_superset_initial_repaste(wpm2_db):
    test_wpm2_superset_attempt_blocks_subset_initial_repaste(wpm2_db, False)


def test_wpm2_versioned_nested_cursor_wins_over_conflicting_legacy_top_level():
    evidence = {**_cursor(999), "last_observed_ref": _cursor(10)}
    mode, cursor = wpm2_cursor_baseline(evidence)
    assert (mode, cursor["size"]) == ("versioned", 10)


def test_wpm2_legacy_top_level_cursor_migrates_once():
    mode, cursor = wpm2_cursor_baseline({key: value for key, value in _cursor().items()
                                         if key != "cursor_version"})
    assert (mode, cursor["size"]) == ("migration", 10)


@pytest.mark.parametrize("nested", ["broken", {"cursor_version": 99}])
def test_wpm2_malformed_nested_cursor_never_falls_back(nested):
    evidence = {**_cursor(4), "last_observed_ref": nested}
    assert wpm2_cursor_baseline(evidence) == ("unresolved", None)


def test_wpm2_unknown_cursor_version_with_valid_top_level_unresolved():
    test_wpm2_malformed_nested_cursor_never_falls_back({"cursor_version": 2})


def test_wpm2_advance_cursor_ambiguous_and_interrupted_rows(wpm2_db):
    message = create_inbox_message("sender", "receiver", "one")
    attempt = begin_delivery_attempt([message], "receiver", "claude_code", "h", 3,
                                     evidence=json.dumps({"last_observed_ref": _cursor()}))
    settle_delivery_attempt(attempt, MessageStatus.PENDING, "ambiguous",
                            reason="confirmation_timeout",
                            evidence=json.dumps({"last_observed_ref": _cursor()}))
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(), _cursor(20)) == "advanced"


def test_wpm2_concurrent_cursor_advances_converge_on_max_size(wpm2_db):
    message = create_inbox_message("sender", "receiver", "one")
    attempt = begin_delivery_attempt([message], "receiver", "claude_code", "h", 3)
    settle_delivery_attempt(attempt, MessageStatus.PENDING, "interrupted",
                            reason="terminal_not_found",
                            evidence=json.dumps({"last_observed_ref": _cursor()}))
    assert advance_wpm2_continuity_cursor(attempt, [message.id], _cursor(), _cursor(30)) == "advanced"
    assert advance_wpm2_continuity_cursor(attempt, [message.id], _cursor(), _cursor(20)) == "already_advanced"


def test_wpm2_concurrent_mid_advance_converges_on_observed_size(wpm2_db):
    message = create_inbox_message("sender", "receiver", "one")
    attempt = begin_delivery_attempt([message], "receiver", "claude_code", "h", 3)
    settle_delivery_attempt(attempt, MessageStatus.PENDING, "interrupted",
                            reason="terminal_not_found",
                            evidence=json.dumps({"last_observed_ref": _cursor()}))
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(), _cursor(20)) == "advanced"
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(), _cursor(30)) == "advanced"
    stored = json.loads(_attempt_row(wpm2_db, attempt).evidence)["last_observed_ref"]
    assert stored["size"] == 30


def test_wpm2_stale_open_threshold_boundaries(wpm2_db):
    messages = [create_inbox_message("sender", "receiver", str(i)) for i in range(3)]
    attempts = [begin_delivery_attempt([message], "receiver", "claude_code", str(i), 1)
                for i, message in enumerate(messages)]
    now = datetime.now(timezone.utc)
    with wpm2_db.begin() as db:
        for attempt, age in zip(attempts, [59.9, 60, 60.1]):
            db.get(InboxDeliveryAttemptModel, attempt).started_at = now - timedelta(seconds=age)
    selected = {row["attempt_uuid"] for row in
                list_stale_open_claude_attempts(WPM2_STALE_OPEN_AGE_SECONDS)}
    assert attempts[0] not in selected
    assert set(attempts[1:]) <= selected


def test_wpm2_large_transcript_multi_hash_admission_reads_one_bounded_suffix(tmp_path):
    path = tmp_path / "trace.jsonl"
    prefix = MAX_IN_TXN_TRANSCRIPT_DELTA_BYTES + 100
    with path.open("wb") as stream:
        stream.seek(prefix - 1)
        stream.write(b"\n")
        baseline = stream.tell()
        stream.write(json.dumps({"type": "user", "message": "hit"}).encode() + b"\n")
    ref = {"path": str(path), "inode": path.stat().st_ino, "size": baseline,
           "resolution_kind": "binding"}
    outcome, _ = bounded_transcript_suffix_lookup(
        ref, [(wire_hash("miss"), None), (wire_hash("hit"), None)])
    assert outcome == "hit"


def _composer_state(fixture):
    capture = (FIXTURES / "claude_busy_processing" / fixture).read_text()
    provider = ClaudeCodeProvider("t", "s", "w", "dev")
    backend = MagicMock()
    backend.get_history.return_value = capture
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        return provider.read_composer_draft_state()


def _observation(status, epoch="epoch", seq=1):
    return BoundaryObservation(epoch, status, 1, 1, seq,
                               seq if status == TerminalStatus.PROCESSING else None,
                               seq if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED} else None)


def test_wpm2_busy_claude_real_empty_frame_reaches_initial_paste(wpm2_db):
    assert _composer_state("empty.txt") == "empty"
    message = create_inbox_message("sender", "receiver", "payload")
    provider = MagicMock()
    provider.read_composer_draft_state.side_effect = lambda: _composer_state("empty.txt")
    submitted = _observation(TerminalStatus.PROCESSING, seq=2)

    def send(*_args, **kwargs):
        kwargs["on_submitted"](submitted)
        return submitted

    with (
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=None),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="payload"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send) as paste,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", {"kind": "transcript_absent"})),
    ):
        monitor.get_boundary_observation.return_value = _observation(
            TerminalStatus.PROCESSING)
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        InboxService().deliver_pending("receiver")
    assert paste.call_count == 1
    trace = get_message_trace(message.id)
    assert len(trace["attempts"]) == 1
    evidence = trace["attempts"][0]["evidence"]
    assert evidence["busy_initial_submit"]["status_at_submit"] == "processing"
    assert trace["message"]["status"] == MessageStatus.PENDING.value


def test_wpm2_busy_claude_real_nonempty_frame_holds():
    assert _composer_state("nonempty.txt") == "nonempty"


def test_wpm2_busy_claude_parser_ambiguity_is_unresolved():
    assert _composer_state("parser_ambiguous.txt") == "unresolved"


def test_wpm2_busy_claude_capture_failure_is_unresolved():
    provider = ClaudeCodeProvider("t", "s", "w", "dev")
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("capture failed")
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        assert provider.read_composer_draft_state() == "unresolved"


def test_wpm2_seed_stderr_only_and_duplicate_value_capture(monkeypatch):
    output = (FIXTURES / "wp2s3_c1" / "prod-argv-stderr.txt").read_text()
    uuids = __import__("re").findall(
        r"(?im)^\s*session id:\s*([0-9a-f]{8}-[0-9a-f-]{27,})\s*$", output)
    assert len(set(uuids)) == 1
    completed = SimpleNamespace(returncode=0, stdout=output + "\n" + output)
    run = MagicMock(return_value=completed)
    monkeypatch.setattr("cli_agent_orchestrator.providers.codex.subprocess.run", run)
    monkeypatch.setattr("cli_agent_orchestrator.providers.codex.load_agent_profile",
                        lambda _n: SimpleNamespace(model=None, codexConfig={}))
    monkeypatch.setattr("cli_agent_orchestrator.providers.codex.get_provider_defaults",
                        lambda _n: {})
    monkeypatch.setattr(CodexProvider, "validate_session_artifact", lambda *_a: None)
    assert CodexProvider.seed_resume_identity("/work", "dev") == uuids[0]
    assert run.call_args.kwargs["stderr"] is subprocess.STDOUT
    for suffix in ("", "\n"):
        run.return_value = SimpleNamespace(returncode=0, stdout=output.rstrip("\n") + suffix)
        assert CodexProvider.seed_resume_identity("/work", "dev") == uuids[0]
    other = "11111111-1111-4111-8111-111111111111"
    run.return_value = SimpleNamespace(
        returncode=0, stdout=output + f"\nSession ID: {other}")
    with pytest.raises(RuntimeError, match="seed_uuid_unparseable"):
        CodexProvider.seed_resume_identity("/work", "dev")


def _settled(wpm2_db, *, outcome="ambiguous", reason="confirmation_timeout",
             evidence=None, provider="claude_code", text="payload"):
    message = create_inbox_message("sender", "receiver", text)
    attempt = begin_delivery_attempt(
        [message], "receiver", provider, wire_hash(text), len(text),
        evidence=json.dumps(evidence or {}))
    settle_delivery_attempt(attempt, MessageStatus.PENDING, outcome,
                            reason=reason, evidence=json.dumps(evidence or {}))
    return message, attempt


def _attempt_row(sessions, attempt_uuid):
    with sessions() as db:
        return db.get(InboxDeliveryAttemptModel, attempt_uuid)


def test_wpm2_corrective_d2_hit_between_preflight_and_open_is_stale_admission(
        wpm2_db, tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "seed"}) + "\n")
    binding = create_transcript_binding(
        "receiver", "session-a", str(path), path.stat().st_ino, "test")
    cursor = {"path": str(path), "inode": path.stat().st_ino,
              "size": path.stat().st_size, "resolution_kind": "binding",
              "cursor_version": 1}
    evidence = {
        "last_observed_ref": cursor,
        "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1},
        "boundary_exhausted_at": "2030-01-01T00:00:00Z",
        "boundary_snapshot": {
            "observation_epoch": "epoch", "status": "completed", "status_gen": 3,
            "input_gen": 1, "seq": 4, "last_non_ready_seq": 2,
            "last_ready_seq": 4,
        },
    }
    message, attempt = _settled(wpm2_db, evidence=evidence)
    proof = make_admission_proof("corrective", [message.id], attempt)
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "user", "timestamp": "2030-01-01T00:00:01Z",
                                 "message": "payload"}) + "\n")
    result = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "new", 3,
        prior_attempt_uuid=attempt, admission_proof=proof)
    assert binding["id"] and result.kind == "stale_admission"


def test_wpm2_corrective_binding_rotation_between_preflight_and_open_is_stale_admission(
        wpm2_db, tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps({"type": "user", "message": "seed"}) + "\n")
    second.write_text(json.dumps({"type": "user", "message": "seed"}) + "\n")
    create_transcript_binding("receiver", "one", str(first), first.stat().st_ino, "test")
    cursor = {"path": str(first), "inode": first.stat().st_ino, "size": first.stat().st_size,
              "resolution_kind": "binding", "cursor_version": 1}
    message, attempt = _settled(wpm2_db, evidence={"last_observed_ref": cursor})
    proof = make_admission_proof("corrective", [message.id], attempt)
    create_transcript_binding("receiver", "two", str(second), second.stat().st_ino, "compact")
    result = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "new", 3,
        prior_attempt_uuid=attempt, admission_proof=proof)
    assert result.kind == "stale_admission"


def test_wpm2_unversioned_nested_over_advanced_by_unresolved_lookup_migrates(tmp_path):
    path = tmp_path / "trace.jsonl"
    first = json.dumps({"type": "user", "message": "seed"}) + "\n"
    hit = json.dumps({"type": "user", "message": "payload"}) + "\n"
    path.write_text(first + hit)
    evidence = {"path": str(path), "inode": path.stat().st_ino, "size": len(first),
                "resolution_kind": "binding",
                "last_observed_ref": {"path": str(path), "inode": path.stat().st_ino,
                                      "size": path.stat().st_size,
                                      "resolution_kind": "binding"}}
    mode, baseline = wpm2_cursor_baseline(evidence)
    assert mode == "migration" and baseline["size"] == len(first)
    outcome, _ = bounded_transcript_suffix_lookup(
        baseline, [(wire_hash("payload"), None)])
    assert outcome == "hit"


def test_wpm2_unversioned_nested_alone_full_rescan(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "payload"}) + "\n")
    evidence = {"last_observed_ref": {
        "path": str(path), "inode": path.stat().st_ino, "size": path.stat().st_size,
        "resolution_kind": "binding"}}
    mode, baseline = wpm2_cursor_baseline(evidence)
    assert mode == "migration" and baseline["size"] == 0
    assert bounded_transcript_suffix_lookup(
        baseline, [(wire_hash("payload"), None)])[0] == "hit"


def test_wpm2_version_upgrade_writes_smaller_size(wpm2_db):
    unversioned = {"path": "/trace", "inode": 7, "size": 999,
                   "resolution_kind": "binding"}
    message, attempt = _settled(
        wpm2_db, outcome="interrupted", reason="terminal_not_found",
        evidence={"last_observed_ref": unversioned})
    expected = {**unversioned, "size": 0}
    observed = {**unversioned, "size": 10}
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], expected, observed) == "advanced"
    evidence = json.loads(_attempt_row(wpm2_db, attempt).evidence)
    assert evidence["last_observed_ref"]["size"] == 10
    assert evidence["last_observed_ref"]["cursor_version"] == 1


def test_wpm2_malformed_nested_with_valid_top_level_unresolved():
    assert wpm2_cursor_baseline({**_cursor(4), "last_observed_ref": {"size": 3}}) == (
        "unresolved", None)


def test_wpm2_versioned_cursor_survives_status_and_transcript_activity_merge(wpm2_db):
    cursor = _cursor(42)
    message, attempt = _settled(
        wpm2_db, evidence={"resolution_kind": "binding", "last_observed_ref": cursor})
    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("absent", {"kind": "transcript_absent"})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        monitor.get_boundary_observation.return_value = _observation(
            TerminalStatus.PROCESSING, "epoch", 2)
        InboxService()._handle_wpm1_gate(
            "receiver", [message], {"provider": "claude_code"}, None,
            "sender", message.orchestration_type)
    evidence = json.loads(_attempt_row(wpm2_db, attempt).evidence)
    assert evidence["last_observed_status"] == "processing"
    assert evidence["last_observed_ref"] == cursor


def test_wpm2_boundary_exhaustion_persists_atomic_snapshot(wpm2_db):
    cursor = _cursor(10)
    evidence = {"resolution_kind": "binding", "last_observed_ref": cursor,
                "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1}}
    message, attempt = _settled(wpm2_db, evidence=evidence)
    cycle = BoundaryObservation("epoch", TerminalStatus.COMPLETED, 3, 1, 4, 2, 4)
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("absent", {"kind": "transcript_absent"})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        monitor.get_boundary_observation.return_value = cycle
        state, _ = InboxService()._handle_wpm1_gate(
            "receiver", [message], {"provider": "claude_code"}, provider,
            "sender", message.orchestration_type)
    assert state == "inject"
    durable = json.loads(_attempt_row(wpm2_db, attempt).evidence)
    assert durable["boundary_exhausted_at"]
    assert durable["boundary_snapshot"]["last_non_ready_seq"] == 2


def test_wpm2_cursor_refresh_routes_through_w5_only(wpm2_db):
    cursor = _cursor(10)
    message, attempt = _settled(
        wpm2_db, evidence={"resolution_kind": "binding", "last_observed_ref": cursor})
    observed = {**cursor, "size": 20}
    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("absent", {"kind": "transcript_absent",
                                       "last_observed_ref": observed})),
        patch("cli_agent_orchestrator.services.inbox_service."
              "advance_wpm2_continuity_cursor", return_value="advanced") as w5,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True) as merge,
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        monitor.get_boundary_observation.return_value = _observation(
            TerminalStatus.PROCESSING, "epoch", 2)
        InboxService()._handle_wpm1_gate(
            "receiver", [message], {"provider": "claude_code"}, None,
            "sender", message.orchestration_type)
    w5.assert_called_once_with(attempt, [message.id], cursor, observed)
    assert all("last_observed_ref" not in call.args[2] for call in merge.call_args_list)


def test_wpm2_advance_cursor_rowcount_stale_and_busy_results(wpm2_db, monkeypatch):
    message, attempt = _settled(wpm2_db, evidence={"last_observed_ref": _cursor()})
    assert advance_wpm2_continuity_cursor(attempt, [999], _cursor(), _cursor(20)) == "stale"
    database.update_message_status(message.id, MessageStatus.DELIVERED)
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(), _cursor(20)) == "stale"
    monkeypatch.setattr(database, "_run_wpm1_immediate", lambda _operation: "busy_aborted")
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(), _cursor(20)) == "busy_aborted"


def test_wpm2_admission_proof_vs_w5_advance_both_commit_orders(wpm2_db):
    message, attempt = _settled(wpm2_db, evidence={"last_observed_ref": _cursor()})
    proof = make_admission_proof("ordinary", [message.id])
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(), _cursor(20)) == "advanced"
    with patch("cli_agent_orchestrator.services.message_trace_service."
               "bounded_transcript_suffix_lookup", return_value=("absent", {})):
        assert begin_delivery_attempt_if_no_other_delivering(
            [message], "receiver", "claude_code", "h", 1,
            admission_proof=proof).kind == "stale_admission"
    fresh = make_admission_proof("ordinary", [message.id])
    with patch("cli_agent_orchestrator.services.message_trace_service."
               "bounded_transcript_suffix_lookup", return_value=("absent", {})):
        opened = begin_delivery_attempt_if_no_other_delivering(
            [message], "receiver", "claude_code", "h", 1,
            admission_proof=fresh)
    assert opened.kind == "opened"
    assert advance_wpm2_continuity_cursor(
        attempt, [message.id], _cursor(20), _cursor(30)) == "stale"


def test_wpm2_non_claude_settlement_preserves_evidence_bytes(wpm2_db):
    message = create_inbox_message("sender", "receiver", "payload")
    raw = json.dumps({"path": "/trace", "inode": 1, "size": 10,
                      "resolution_kind": "binding"}, separators=(",", ":"))
    attempt = begin_delivery_attempt(
        [message], "receiver", "codex", "h", 1, evidence=raw)
    settle_delivery_attempt(
        attempt, MessageStatus.PENDING, "interrupted",
        reason="terminal_not_found", evidence=raw)
    assert _attempt_row(wpm2_db, attempt).evidence == raw


def test_wpm2_settlement_does_not_promote_unversioned_nested_cursor(wpm2_db):
    evidence = {**_cursor(10), "last_observed_ref": {
        "path": "/trace", "inode": 7, "size": 999, "resolution_kind": "binding"}}
    _, attempt = _settled(wpm2_db, evidence=evidence)
    stored = json.loads(_attempt_row(wpm2_db, attempt).evidence)
    assert "cursor_version" not in stored["last_observed_ref"]


def test_wpm2_bounded_suffix_enforces_started_at(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z",
                    "message": "payload"}),
        json.dumps({"type": "user", "timestamp": "2040-01-01T00:00:00Z",
                    "message": "other"}), ""]))
    ref = {"path": str(path), "inode": path.stat().st_ino, "size": 0,
           "resolution_kind": "binding"}
    assert bounded_transcript_suffix_lookup(
        ref, [(wire_hash("payload"), "2030-01-01T00:00:00Z")])[0] == "absent"


def test_wpm2_binding_only_without_cursor_is_hits_only():
    evidence = {"resolution_kind": "binding"}
    with patch("cli_agent_orchestrator.services.message_trace_service.continuity_aware_lookup",
               return_value=("absent", {"kind": "transcript_absent"})):
        assert _wpm2_lookup({}, "hash", None, evidence)[0] == "unresolved"
    with patch("cli_agent_orchestrator.services.message_trace_service.continuity_aware_lookup",
               return_value=("hit", {"kind": "transcript_queued_command"})):
        assert _wpm2_lookup({}, "hash", None, evidence)[0] == "hit"


def test_wpm2_evidence_schema_rejects_unlisted_and_partial_boundary(wpm2_db):
    message, attempt = _settled(wpm2_db)
    with pytest.raises(ValueError, match="non-WPM1"):
        database.merge_wpm1_attempt_evidence(attempt, [message.id], {"invented": True})
    with pytest.raises(ValueError, match="atomic snapshot"):
        database.merge_wpm1_attempt_evidence(
            attempt, [message.id], {"boundary_exhausted_at": "2030-01-01T00:00:00Z"})


def test_wpm2_confirmation_evidence_targets_hit_attempt(wpm2_db):
    message, first = _settled(wpm2_db, text="payload")
    second = begin_delivery_attempt([message], "receiver", "claude_code", "second", 1)
    settle_delivery_attempt(second, MessageStatus.PENDING, "ambiguous",
                            reason="confirmation_timeout", evidence="{}")
    hit = {"kind": "transcript_queued_command", "offset": 17}
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver",
        confirmation_evidence=(first, hit)) == "settled"
    first_evidence = json.loads(_attempt_row(wpm2_db, first).evidence)
    second_evidence = json.loads(_attempt_row(wpm2_db, second).evidence)
    assert first_evidence["kind"] == "transcript_queued_command"
    assert "kind" not in second_evidence and "terminal_settled_at" in second_evidence


def _open_old_attempt(sessions, age=61, evidence=None):
    message = create_inbox_message("sender", "receiver", "payload")
    attempt = begin_delivery_attempt(
        [message], "receiver", "claude_code", wire_hash("payload"), 7,
        evidence=json.dumps(evidence or {}))
    with sessions.begin() as db:
        db.get(InboxDeliveryAttemptModel, attempt).started_at = (
            datetime.now(timezone.utc) - timedelta(seconds=age))
    return message, attempt


def _recovery_absent_context(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    resolution = TranscriptResolution(
        path, "binding", path.stat().st_ino,
        live_reference=TranscriptLiveReference(path, path.stat().st_ino, path.stat().st_size))
    return path, resolution


def test_wpm2_old_message_fresh_attempt_not_stale(wpm2_db):
    message, attempt = _open_old_attempt(wpm2_db, age=0)
    with wpm2_db.begin() as db:
        db.get(InboxModel, message.id).created_at = datetime.now() - timedelta(hours=12)
    assert attempt not in {row["attempt_uuid"] for row in
                           list_stale_open_claude_attempts(60)}


def test_wpm2_recovery_below_age_threshold_holds(wpm2_db):
    message, attempt = _open_old_attempt(wpm2_db, age=59)
    InboxService().recover_stale_deliveries(recurring=True)
    assert _attempt_row(wpm2_db, attempt).settled_at is None
    with wpm2_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERING.value


def test_wpm2_recovery_skips_held_lock_presubmit(wpm2_db, tmp_path):
    message, attempt = _open_old_attempt(wpm2_db)
    lock = get_delivery_lock("receiver")
    lock.acquire()
    try:
        InboxService().recover_stale_deliveries(recurring=True)
        assert _attempt_row(wpm2_db, attempt).settled_at is None
    finally:
        lock.release()


def test_wpm2_recovery_skips_held_lock_tail(wpm2_db, tmp_path):
    message, attempt = _open_old_attempt(wpm2_db, age=120)
    lock = get_delivery_lock("receiver")
    lock.acquire()
    try:
        InboxService().recover_stale_deliveries(recurring=True)
        assert list_delivering_attempts_for_terminal("receiver")[0]["attempt_uuid"] == attempt
    finally:
        lock.release()


def test_wpm2_recovery_after_lock_release(wpm2_db, tmp_path):
    message, attempt = _open_old_attempt(wpm2_db)
    _, resolution = _recovery_absent_context(tmp_path)
    lock = get_delivery_lock("receiver")
    lock.acquire()
    try:
        InboxService().recover_stale_deliveries(recurring=True)
    finally:
        lock.release()
    with patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
               return_value=resolution):
        InboxService().recover_stale_deliveries(recurring=True)
    row = _attempt_row(wpm2_db, attempt)
    assert row.outcome == "ambiguous" and row.reason == "confirmation_timeout"


def test_wpm2_recurring_recovery_repairs_stranded_delivering_without_restart(
        wpm2_db, tmp_path):
    path, resolution = _recovery_absent_context(tmp_path)
    message, attempt = _open_old_attempt(
        wpm2_db, evidence=transcript_ref(resolution))
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    with patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
               return_value=resolution):
        service = InboxService()
        service.reconcile_orphaned_messages()
    row = _attempt_row(wpm2_db, attempt)
    assert row.outcome == "ambiguous"
    assert json.loads(row.evidence)["crash_recovery"]["kind"] == (
        "possibly_submitted_without_anchor")
    with path.open("a") as stream:
        stream.write(json.dumps({
            "attachment": {"type": "queued_command", "prompt": "payload"},
        }) + "\n")
    with patch("cli_agent_orchestrator.services.inbox_service.terminal_service."
               "send_prepared_input") as paste:
        # Same service/process, later reconciliation wake: D2 repairs without reopen.
        service.deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert paste.call_count == 0 and len(trace["attempts"]) == 1
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value


def test_wpm2_recovery_cas_loses_to_inflight_settlement(wpm2_db, tmp_path):
    message, attempt = _open_old_attempt(wpm2_db)
    _, resolution = _recovery_absent_context(tmp_path)
    recovery_at_cas = threading.Event()
    settlement_done = threading.Event()
    real_recover = recover_wpm2_stale_attempt
    recovery_results = []

    def delayed_recovery(*args, **kwargs):
        recovery_at_cas.set()
        assert settlement_done.wait(5)
        result = real_recover(*args, **kwargs)
        recovery_results.append(result)
        return result

    def recover_service():
        with (
            patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
                  return_value=resolution),
            patch("cli_agent_orchestrator.services.inbox_service.recover_wpm2_stale_attempt",
                  side_effect=delayed_recovery),
        ):
            InboxService().recover_stale_deliveries(recurring=True)

    thread = threading.Thread(target=recover_service)
    thread.start()
    assert recovery_at_cas.wait(5)
    assert settle_delivery_attempt_proof_safe(attempt, {}, 1) == "settled"
    settlement_done.set()
    thread.join(5)
    assert not thread.is_alive() and recovery_results == ["stale"]
    row = _attempt_row(wpm2_db, attempt)
    assert row.outcome == "ambiguous" and row.settled_at is not None


def test_wpm2_recovery_lock_released_exactly_once(wpm2_db, monkeypatch):
    _, attempt = _open_old_attempt(wpm2_db)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
        MagicMock(side_effect=RuntimeError("cancel")))
    with pytest.raises(RuntimeError, match="cancel"):
        InboxService().recover_stale_deliveries(recurring=True)
    lock = get_delivery_lock("receiver")
    assert lock.acquire(blocking=False)
    lock.release()


def test_wpm2_slow_inband_settlement_vs_first_eligible_pass(wpm2_db):
    message, attempt = _open_old_attempt(wpm2_db, age=120)
    lock = get_delivery_lock("receiver")
    lock.acquire()
    try:
        InboxService().recover_stale_deliveries(recurring=True)
        assert _attempt_row(wpm2_db, attempt).settled_at is None
    finally:
        lock.release()


def _deliver_scenario(wpm2_db, *, admission_status, submit_status=None,
                      submit_epoch="epoch", send_error=None, snapshot_error=None,
                      proof_safe_result=None, malformed_snapshot=False,
                      confirm_error=None):
    message = create_inbox_message("sender", "receiver", "payload")
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    admission = _observation(admission_status, "epoch", 1)
    submit = None if submit_status is None else _observation(submit_status, submit_epoch, 2)

    def send(*_args, **kwargs):
        if submit is not None:
            kwargs["on_submitted"](submit)
        if send_error is not None:
            raise send_error
        return submit

    patches = [
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=None),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="payload"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send),
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              side_effect=confirm_error,
              return_value=("absent", {"kind": "transcript_absent"})),
    ]
    if proof_safe_result is not None:
        patches.append(patch(
            "cli_agent_orchestrator.services.inbox_service.settle_delivery_attempt_proof_safe",
            return_value=proof_safe_result))
    with patches[0], patches[1], patches[2], patches[3] as paste, patches[4]:
        extra = patches[5].start() if len(patches) > 5 else None
        try:
            with patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor:
                monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
                monitor.probe_screen_status.return_value = (
                    TerminalStatus.IDLE, {"result_status": "idle"}
                )
                if snapshot_error is not None:
                    monitor.get_boundary_observation.side_effect = snapshot_error
                elif malformed_snapshot:
                    monitor.get_boundary_observation.return_value = SimpleNamespace(
                        status="processing", observation_epoch="epoch")
                else:
                    monitor.get_boundary_observation.return_value = admission
                monitor.get_status.return_value = admission_status
                InboxService().deliver_pending("receiver")
        finally:
            if len(patches) > 5:
                patches[5].stop()
    return message, paste


def test_wpm2_s4_admission_snapshot_unavailable_holds(wpm2_db):
    message, paste = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.PROCESSING,
        snapshot_error=RuntimeError("snapshot unavailable"))
    assert paste.call_count == 0 and get_message_trace(message.id)["attempts"] == []


def test_wpm2_s4_malformed_snapshot_fallback_processing_holds(wpm2_db):
    message, paste = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.PROCESSING,
        malformed_snapshot=True)
    trace = get_message_trace(message.id)
    assert paste.call_count == 0
    assert trace["attempts"] == [] and trace["message"]["status"] == "pending"


def test_wpm2_pre_submit_exception_keeps_deferred_failed_semantics(wpm2_db):
    message, paste = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.IDLE,
        send_error=DeliveryDeferredError("draft"))
    attempt = get_message_trace(message.id)["attempts"][0]
    assert paste.call_count == 1 and attempt["outcome"] == "deferred"


def test_wpm2_submit_exception_uncertain_acceptance_settles_anchorless_ambiguous(wpm2_db):
    message, _ = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.IDLE,
        send_error=RuntimeError("uncertain"))
    attempt = get_message_trace(message.id)["attempts"][0]
    assert attempt["outcome"] == "ambiguous"
    row = _attempt_row(wpm2_db, attempt["attempt_uuid"])
    durable = {"outcome": row.outcome, "reason": row.reason, "evidence": row.evidence}
    assert classify_permanently_d2_only(durable, "epoch") == "anchor_missing"


def test_wpm2_tail_exception_after_anchor_settles_ambiguous_with_fact(wpm2_db):
    message, paste = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.PROCESSING,
        submit_status=TerminalStatus.PROCESSING,
        confirm_error=RuntimeError("confirmation tail"))
    attempt = get_message_trace(message.id)["attempts"][0]
    assert paste.call_count == 1 and attempt["outcome"] == "ambiguous"
    assert {"injection_completed_seq", "busy_initial_submit"} <= set(attempt["evidence"])


def test_wpm2_settlement_failure_leaves_delivering_never_terminal(wpm2_db):
    message, _ = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.PROCESSING,
        submit_status=TerminalStatus.PROCESSING,
        send_error=RuntimeError("tail"),
        proof_safe_result="settlement_pending_recovery")
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERING.value
    assert trace["attempts"][0]["settled_at"] is None


def test_wpm2_submit_snapshot_unavailable_or_epoch_change_settles_anchorless(wpm2_db):
    message, _ = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.PROCESSING,
        submit_status=TerminalStatus.PROCESSING, submit_epoch="new")
    attempt = get_message_trace(message.id)["attempts"][0]
    assert "injection_completed_seq" not in attempt["evidence"]
    assert classify_permanently_d2_only(attempt, "new") == "anchor_missing"


def test_wpm2_stable_ready_initial_keeps_normal_discipline(wpm2_db):
    message, _ = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.IDLE,
        submit_status=TerminalStatus.COMPLETED)
    evidence = get_message_trace(message.id)["attempts"][0]["evidence"]
    assert "busy_initial_submit" not in evidence
    assert evidence["injection_completed_seq"]["observation_epoch"] == "epoch"


def test_wpm2_ready_admission_processing_before_paste_is_protected(wpm2_db):
    message, _ = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.IDLE,
        submit_status=TerminalStatus.PROCESSING)
    evidence = get_message_trace(message.id)["attempts"][0]["evidence"]
    assert evidence["busy_initial_submit"]["status_at_admission"] == "idle"


def test_wpm2_processing_admission_ready_before_paste_still_protected(wpm2_db):
    message, _ = _deliver_scenario(
        wpm2_db, admission_status=TerminalStatus.PROCESSING,
        submit_status=TerminalStatus.COMPLETED)
    evidence = get_message_trace(message.id)["attempts"][0]["evidence"]
    assert evidence["busy_initial_submit"]["status_at_submit"] == "completed"


def test_wpm2_busy_initial_compact_cycles_never_exhaust(wpm2_db):
    path = Path(wpm2_db.kw["bind"].url.database).with_name("compact.jsonl")
    path.write_text(json.dumps({"type": "assistant", "message": {
        "role": "assistant", "content": "working"}}) + "\n")
    create_transcript_binding(
        "receiver", "session", str(path), path.stat().st_ino, "test")
    resolution = TranscriptResolution(
        path, "binding", path.stat().st_ino,
        live_reference=TranscriptLiveReference(path, path.stat().st_ino, path.stat().st_size))
    message = create_inbox_message("sender", "receiver", "payload")
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    observation = [_observation(TerminalStatus.PROCESSING, "epoch", 1)]
    submit = _observation(TerminalStatus.PROCESSING, "epoch", 2)
    pastes = []

    def send(*_args, **kwargs):
        pastes.append(kwargs.get("original_message"))
        kwargs["on_submitted"](submit)
        return submit

    absent = {**transcript_ref(resolution), "kind": "transcript_absent"}
    with (
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=resolution),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="payload"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send),
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", absent)),
    ):
        monitor.get_boundary_observation.side_effect = lambda _terminal: observation[0]
        monitor.get_status.side_effect = lambda _terminal: observation[0].status
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        service = InboxService()
        service.deliver_pending("receiver")
        assert len(pastes) == 1

        for index in range(3):
            observation[0] = BoundaryObservation(
                "epoch", TerminalStatus.COMPLETED, index + 2, 1, index * 2 + 4,
                index * 2 + 3, index * 2 + 4)
            service.deliver_pending("receiver")
            trace = get_message_trace(message.id)
            assert len(pastes) == 1
            assert trace["message"]["status"] == MessageStatus.PENDING.value
            assert len(trace["attempts"]) == 1
            assert "boundary_exhausted_at" not in trace["attempts"][0]["evidence"]

        with path.open("a") as stream:
            stream.write(json.dumps({
                "timestamp": "2030-01-01T00:00:00Z",
                "attachment": {"type": "queued_command", "prompt": "payload"},
            }) + "\n")
        service.deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert len(pastes) == 1 and len(trace["attempts"]) == 1
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert trace["attempts"][0]["evidence"]["kind"] == "transcript_queued_command"


def test_wpm2_busy_initial_receiver_gone_exit(wpm2_db):
    message, _ = _settled(wpm2_db, evidence={"busy_initial_submit": {}})
    state, _ = InboxService()._handle_wpm1_gate(
        "receiver", [message], {}, None, "sender", message.orchestration_type)
    assert state == "stop"
    with wpm2_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERY_FAILED.value


def _release_disjoint(wpm2_db, evidences, *, num_messages=1, transient=False):
    protected = []
    for index, evidence in enumerate(evidences):
        message, attempt = _settled(
            wpm2_db, evidence={"resolution_kind": "binding", **evidence},
            text=f"protected-{index}")
        protected.append((message, attempt))
    later = create_inbox_message("sender", "receiver", "later")
    ready = _observation(TerminalStatus.IDLE, "current", 20)
    calls = 0

    def boundary(_terminal):
        nonlocal calls
        calls += 1
        if transient and calls <= len(evidences):
            raise RuntimeError("snapshot unavailable")
        return ready

    def send(*_args, **kwargs):
        kwargs["on_submitted"](ready)
        return ready

    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("absent", {"kind": "transcript_absent"})),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=None),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              side_effect=lambda _terminal, payload, *_args: payload),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send) as paste,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("hit", {"kind": "transcript_queued_command"})),
    ):
        monitor.get_boundary_observation.side_effect = boundary
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        InboxService().deliver_pending("receiver", num_messages=num_messages)
    return protected, later, paste.call_count


def test_wpm2_permanently_absent_head_does_not_starve_later_callbacks(wpm2_db):
    protected, later, pastes = _release_disjoint(wpm2_db, [{"crash_recovery": {}}])
    assert pastes == 1
    assert get_message_trace(protected[0][0].id)["message"]["status"] == "pending"
    assert get_message_trace(later.id)["message"]["status"] == "delivered"


def test_wpm2_crash_before_submit_protects_head_and_releases_disjoint_callback(wpm2_db):
    protected, later, pastes = _release_disjoint(
        wpm2_db, [{"crash_recovery": {"kind": "possibly_submitted_without_anchor"}}])
    assert pastes == 1 and _attempt_row(wpm2_db, protected[0][1]).outcome == "ambiguous"


def test_wpm2_crash_after_submit_protects_head_and_releases_disjoint_callback(wpm2_db):
    protected, later, pastes = _release_disjoint(wpm2_db, [{
        "injection_completed_seq": {"observation_epoch": "old", "seq": 3}}])
    assert pastes == 1
    row = _attempt_row(wpm2_db, protected[0][1])
    assert classify_permanently_d2_only(
        {"evidence": row.evidence, "outcome": row.outcome, "reason": row.reason},
        "current") == "epoch_mismatch"


def test_wpm2_construction_restart_protects_mismatch_and_releases_disjoint_callback(wpm2_db):
    protected, later, pastes = _release_disjoint(wpm2_db, [{
        "injection_completed_seq": {"observation_epoch": "construction", "seq": 1}}])
    assert pastes == 1 and get_message_trace(later.id)["message"]["status"] == "delivered"


def test_wpm2_rebind_reset_protects_mismatch_and_releases_disjoint_callback(wpm2_db):
    protected, later, pastes = _release_disjoint(wpm2_db, [{
        "injection_completed_seq": {"observation_epoch": "before-rebind", "seq": 99}}])
    assert pastes == 1


def test_wpm2_default_one_skips_all_protected_sets_before_each_disjoint_row(wpm2_db):
    protected, later, pastes = _release_disjoint(
        wpm2_db, [{"crash_recovery": {}}, {
            "injection_completed_seq": {"observation_epoch": "old", "seq": 1}}])
    assert len(protected) == 2 and pastes == 1


def test_wpm2_default_one_mixed_protection_checks_and_releases_in_order(wpm2_db):
    protected, later, pastes = _release_disjoint(
        wpm2_db, [{"crash_recovery": {}}, {"busy_initial_submit": {}}])
    assert pastes == 1 and all(get_message_trace(item.id)["message"]["status"] == "pending"
                               for item, _ in protected)


def test_wpm2_limit_all_excludes_protected_sets_before_grouping(wpm2_db):
    protected, later, pastes = _release_disjoint(
        wpm2_db, [{"crash_recovery": {}}, {"busy_initial_submit": {}}], num_messages=0)
    assert pastes == 1 and get_message_trace(later.id)["message"]["status"] == "delivered"


def test_wpm2_limit_all_mixed_protection_excludes_before_grouping(wpm2_db):
    protected, later, pastes = _release_disjoint(wpm2_db, [
        {"crash_recovery": {}},
        {"injection_completed_seq": {"observation_epoch": "old", "seq": 1}},
        {"busy_initial_submit": {}},
    ], num_messages=0)
    assert len(protected) == 3 and pastes == 1


def test_wpm2_repeated_transient_snapshot_failures_release_disjoint_callbacks(wpm2_db):
    protected, later, pastes = _release_disjoint(
        wpm2_db, [{"injection_completed_seq": {
            "observation_epoch": "current", "seq": 1}}], transient=True)
    assert pastes == 1 and get_message_trace(protected[0][0].id)["message"]["status"] == "pending"


def test_wpm2_crash_recovery_stays_d2_only_across_reconcile_wakes(wpm2_db):
    message, attempt = _settled(
        wpm2_db, evidence={"resolution_kind": "binding", "crash_recovery": {}})
    row = {"evidence": _attempt_row(wpm2_db, attempt).evidence,
           "outcome": "ambiguous", "reason": "confirmation_timeout"}
    for epoch in ("one", "two", "three"):
        assert classify_permanently_d2_only(row, epoch) == "anchor_missing"


def test_wpm2_rebind_reset_cycle_with_absent_payload_stays_d2_only(wpm2_db):
    message, attempt = _settled(wpm2_db, evidence={
        "injection_completed_seq": {"observation_epoch": "old", "seq": 1}})
    row = {"evidence": _attempt_row(wpm2_db, attempt).evidence,
           "outcome": "ambiguous", "reason": "confirmation_timeout"}
    assert classify_permanently_d2_only(row, "new") == "epoch_mismatch"
    assert "boundary_exhausted_at" not in json.loads(row["evidence"])


@pytest.mark.parametrize("evidence", ["not-json", json.dumps({"crash_recovery": {}})])
def test_wpm2_receiver_gone_precedes_malformed_protected_evidence(wpm2_db, evidence):
    message = create_inbox_message("sender", "receiver", "payload")
    attempt = begin_delivery_attempt([message], "receiver", "claude_code", "h", 1)
    settle_delivery_attempt(
        attempt, MessageStatus.PENDING, "ambiguous", reason="confirmation_timeout",
        evidence=evidence)
    state, _ = InboxService()._handle_wpm1_gate(
        "receiver", [message], {}, None, "sender", message.orchestration_type)
    assert state == "stop"
    with wpm2_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERY_FAILED.value


def test_wpm2_receiver_gone_precedes_ordinary_protected_evidence(wpm2_db):
    test_wpm2_receiver_gone_precedes_malformed_protected_evidence(
        wpm2_db, json.dumps({"injection_completed_seq": {
            "observation_epoch": "old", "seq": 1}}))


def test_wpm2_transient_snapshot_failure_d2_hit_confirms_immediately(wpm2_db):
    message, attempt = _settled(
        wpm2_db, evidence={"resolution_kind": "binding",
                           "injection_completed_seq": {
                               "observation_epoch": "epoch", "seq": 1}})
    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("hit", {"kind": "transcript_queued_command"})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        monitor.get_boundary_observation.side_effect = RuntimeError("unavailable")
        state, _ = InboxService()._handle_wpm1_gate(
            "receiver", [message], {"provider": "claude_code"}, None,
            "sender", message.orchestration_type)
    assert state == "stop"
    assert get_message_trace(message.id)["message"]["status"] == "delivered"


def test_wpm2_transient_recovery_same_token_resumes_without_cap_consumption(wpm2_db):
    evidence = {"resolution_kind": "binding", "injection_completed_seq": {
        "observation_epoch": "epoch", "seq": 1}}
    message, attempt = _settled(wpm2_db, evidence=evidence)
    row = {"evidence": _attempt_row(wpm2_db, attempt).evidence,
           "outcome": "ambiguous", "reason": "confirmation_timeout"}
    assert classify_permanently_d2_only(row, None) == "transient_snapshot_unavailable"
    assert classify_permanently_d2_only(row, "epoch") == "normal"
    assert database.count_ambiguous_attempts([message.id]) == 1


def test_wpm2_opener_outer_preflight_conflict_never_sends(wpm2_db):
    first = create_inbox_message("sender", "receiver", "first")
    old = begin_delivery_attempt([first], "receiver", "claude_code", "old", 3)
    settle_delivery_attempt(old, MessageStatus.PENDING, "ambiguous",
                            reason="confirmation_timeout", evidence="{}")
    with wpm2_db.begin() as db:
        db.get(InboxModel, first.id).status = MessageStatus.DELIVERING.value
    assert list_delivering_attempts_for_terminal("receiver")[0]["attempt_uuid"] == old
    second = create_inbox_message("sender", "receiver", "second")
    result = begin_delivery_attempt_if_no_other_delivering(
        [second], "receiver", "claude_code", "new", 3,
        admission_proof=make_admission_proof("ordinary", [second.id]))
    assert result.kind == "delivering_conflict"
    assert get_message_trace(second.id)["attempts"] == []


def test_wpm2_opener_busy_at_begin_write_or_commit_never_sends(wpm2_db, monkeypatch):
    message = create_inbox_message("sender", "receiver", "payload")
    proof = make_admission_proof("ordinary", [message.id])
    monkeypatch.setattr(database, "_run_wpm1_immediate", lambda _operation: "busy_aborted")
    result = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "h", 1, admission_proof=proof)
    assert result.kind == "busy_aborted"
    assert get_message_trace(message.id)["message"]["status"] == "pending"
    assert get_message_trace(message.id)["attempts"] == []


def test_wpm2_opener_post_open_invariant_failure_never_sends(wpm2_db, monkeypatch):
    message = create_inbox_message("sender", "receiver", "payload")
    proof = make_admission_proof("ordinary", [message.id])
    authority = MagicMock(side_effect=[[], [{"attempt_uuid": "peer", "message_ids": [999]}]])
    monkeypatch.setattr(database, "_delivering_authority_in_db", authority)
    result = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "h", 1, admission_proof=proof)
    assert result.kind == "delivering_conflict"
    assert get_message_trace(message.id)["attempts"] == []
    assert get_message_trace(message.id)["message"]["status"] == "pending"


def _race_open(wpm2_db, candidates):
    barrier = threading.Barrier(len(candidates))
    results = []

    def worker(message, proof, prior=None):
        barrier.wait()
        results.append(begin_delivery_attempt_if_no_other_delivering(
            [message], "receiver", "claude_code", "h", 1,
            prior_attempt_uuid=prior, admission_proof=proof).kind)

    threads = [threading.Thread(target=worker, args=item) for item in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    return results


def test_wpm2_s4_vs_ordinary_initial_share_atomic_delivering_opener(wpm2_db):
    first = create_inbox_message("sender", "receiver", "first")
    second = create_inbox_message("sender", "receiver", "second")
    results = _race_open(wpm2_db, [
        (first, make_admission_proof("s4_initial", [first.id]), None),
        (second, make_admission_proof("ordinary", [second.id]), None),
    ])
    assert sorted(results) == ["delivering_conflict", "opened"]
    assert len(list_delivering_attempts_for_terminal("receiver")) == 1


def test_wpm2_s4_vs_corrective_share_atomic_delivering_opener(wpm2_db, tmp_path):
    path = tmp_path / "corrective-race.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    evidence = {
        "last_observed_ref": {
            "path": str(path), "inode": path.stat().st_ino, "size": path.stat().st_size,
            "resolution_kind": "binding", "cursor_version": 1},
        "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1},
        "boundary_exhausted_at": "2030-01-01T00:00:00Z",
        "boundary_snapshot": {
            "observation_epoch": "epoch", "status": "completed", "status_gen": 3,
            "input_gen": 1, "seq": 4, "last_non_ready_seq": 2,
            "last_ready_seq": 4},
    }
    corrective, prior = _settled(wpm2_db, text="corrective", evidence=evidence)
    fresh = create_inbox_message("sender", "receiver", "fresh")
    results = _race_open(wpm2_db, [
        (fresh, make_admission_proof("s4_initial", [fresh.id]), None),
        (corrective, make_admission_proof("corrective", [corrective.id], prior), prior),
    ])
    assert results.count("opened") == 1 and results.count("delivering_conflict") == 1


def _peer_settlement_race(wpm2_db, peer_outcome):
    for order in ("peer_first", "caller_first"):
        message = create_inbox_message("sender", "receiver", f"{peer_outcome}-{order}")
        caller_proof = make_admission_proof("ordinary", [message.id])
        peer_proof = make_admission_proof("ordinary", [message.id])
        start = threading.Barrier(2)
        first_done = threading.Event()
        results = {}
        sends = []

        def peer():
            with wpm2_db() as db:
                assert db.get(InboxModel, message.id) is not None
            start.wait()
            if order == "caller_first":
                assert first_done.wait(5)
            opened = begin_delivery_attempt_if_no_other_delivering(
                [message], "receiver", "claude_code", "peer", 1,
                admission_proof=peer_proof)
            results["peer_open"] = opened.kind
            if opened.kind == "opened":
                sends.append("peer")
                settle_delivery_attempt(
                    opened.attempt_uuid, MessageStatus.PENDING, peer_outcome,
                    reason=("confirmation_timeout" if peer_outcome == "ambiguous"
                            else "delivery_deferred"), evidence="{}")
                results["peer_settle"] = "settled"
            if order == "peer_first":
                first_done.set()

        def caller():
            with wpm2_db() as db:
                assert db.get(InboxModel, message.id) is not None
            start.wait()
            if order == "peer_first":
                assert first_done.wait(5)
            opened = begin_delivery_attempt_if_no_other_delivering(
                [message], "receiver", "claude_code", "caller", 1,
                admission_proof=caller_proof)
            results["caller"] = opened.kind
            if opened.kind == "opened":
                sends.append("caller")
            if order == "caller_first":
                first_done.set()

        threads = [threading.Thread(target=peer), threading.Thread(target=caller)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert len(sends) == 1
        if order == "peer_first":
            assert results["peer_open"] == "opened"
            assert results["caller"] == "stale_admission"
        else:
            assert results["caller"] == "opened"
            assert results["peer_open"] == "delivering_conflict"


def test_wpm2_preflight_vs_peer_ambiguous_settle_both_commit_orders(wpm2_db):
    _peer_settlement_race(wpm2_db, "ambiguous")


def test_wpm2_preflight_vs_peer_deferred_settle_both_commit_orders(wpm2_db):
    _peer_settlement_race(wpm2_db, "deferred")


def test_wpm2_terminal_settlement_vs_open_both_commit_orders(wpm2_db):
    first, prior = _settled(wpm2_db, text="first")
    proof = make_admission_proof("ordinary", [first.id])
    assert settle_wpm1_terminal_batch(
        [first.id], MessageStatus.DELIVERED, "receiver") == "settled"
    assert begin_delivery_attempt_if_no_other_delivering(
        [first], "receiver", "claude_code", "h", 1,
        admission_proof=proof).kind == "stale_admission"
    second = create_inbox_message("sender", "receiver", "second")
    opened = begin_delivery_attempt_if_no_other_delivering(
        [second], "receiver", "claude_code", "h2", 1,
        admission_proof=make_admission_proof("ordinary", [second.id]))
    assert opened.kind == "opened"
    assert settle_wpm1_terminal_batch(
        [second.id], MessageStatus.DELIVERED, "receiver") == "stale"


def test_wpm2_protected_head_stalled_notice_once_before_skip(wpm2_db):
    message, attempt = _settled(wpm2_db, evidence={"crash_recovery": {}})
    stamp = "2030-01-01T00:00:00Z"
    assert record_wpm1_stalled_notice(attempt, [message.id], "receiver", stamp) == "recorded"
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", stamp) == "already_recorded"
    assert json.loads(_attempt_row(wpm2_db, attempt).evidence)["stalled_notified_at"] == stamp


def test_wpm2_protected_head_notice_busy_abort_stops_whole_wake(wpm2_db, monkeypatch):
    message, attempt = _settled(wpm2_db, evidence={"crash_recovery": {}})
    monkeypatch.setattr(database, "_run_wpm1_immediate", lambda _operation: "busy_aborted")
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2030-01-01T00:00:00Z") == "busy_aborted"
    assert "stalled_notified_at" not in json.loads(_attempt_row(wpm2_db, attempt).evidence)


def test_wpm2_protected_head_late_delivery_emits_corrective_notice(wpm2_db):
    message, attempt = _settled(wpm2_db, evidence={"crash_recovery": {}})
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2030-01-01T00:00:00Z") == "recorded"
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm2_db() as db:
        notices = db.query(InboxModel).filter(
            InboxModel.sender_id == "message-trace:receiver").all()
        assert sum(row.message.startswith("wpm1-notice kind=stalled") for row in notices) == 1
        assert sum(row.message.startswith("wpm1-notice kind=corrective") for row in notices) == 1


def _overflow_file(tmp_path, *, hit=False):
    path = tmp_path / ("hit.jsonl" if hit else "absent.jsonl")
    seed = json.dumps({"type": "user", "message": "seed"}) + "\n"
    path.write_text(seed)
    baseline = path.stat().st_size
    with path.open("a") as stream:
        filler = json.dumps({"type": "assistant", "message": "x" * 1024}) + "\n"
        while path.stat().st_size - baseline <= MAX_IN_TXN_TRANSCRIPT_DELTA_BYTES:
            stream.write(filler)
            stream.flush()
        if hit:
            stream.write(json.dumps({
                "attachment": {"type": "queued_command", "prompt": "payload"},
            }) + "\n")
    cursor = {"path": str(path), "inode": path.stat().st_ino, "size": baseline,
              "resolution_kind": "binding", "cursor_version": 1}
    return path, cursor


def test_wpm2_overflow_then_service_refresh_finds_hit_without_open(wpm2_db, tmp_path):
    path, cursor = _overflow_file(tmp_path, hit=True)
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    message, attempt = _settled(
        wpm2_db, evidence={"resolution_kind": "binding", "last_observed_ref": cursor})
    assert bounded_transcript_suffix_lookup(
        cursor, [(wire_hash("payload"), None)])[0] == "overflow"
    with patch("cli_agent_orchestrator.services.inbox_service.terminal_service."
               "send_prepared_input") as paste:
        InboxService().deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert paste.call_count == 0
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert len(trace["attempts"]) == 1
    assert trace["attempts"][0]["evidence"]["kind"] == "transcript_queued_command"


def test_wpm2_overflow_absent_refresh_advances_baseline_then_opens(wpm2_db, tmp_path):
    path, cursor = _overflow_file(tmp_path)
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    evidence = {"resolution_kind": "binding", "last_observed_ref": cursor,
                "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1}}
    message, attempt = _settled(wpm2_db, evidence=evidence)
    stale_proof = make_admission_proof("corrective", [message.id], attempt)
    assert begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "capped", 1,
        prior_attempt_uuid=attempt, admission_proof=stale_proof).kind == "stale_admission"
    assert json.loads(_attempt_row(wpm2_db, attempt).evidence)["last_observed_ref"] == cursor

    cycle = BoundaryObservation("epoch", TerminalStatus.COMPLETED, 3, 1, 4, 2, 4)
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    submitted = _observation(TerminalStatus.COMPLETED, "epoch", 5)

    def send(*_args, **kwargs):
        kwargs["on_submitted"](submitted)
        return submitted

    resolution = TranscriptResolution(
        path, "binding", path.stat().st_ino,
        live_reference=TranscriptLiveReference(path, path.stat().st_ino, path.stat().st_size))
    with (
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="payload"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send) as paste,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", {**transcript_ref(resolution),
                                       "kind": "transcript_absent"})),
    ):
        monitor.get_boundary_observation.return_value = cycle
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        # Fresh service object is the restart cut after the capped opener wake.
        InboxService().deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert paste.call_count == 1 and len(trace["attempts"]) == 2
    persisted = trace["attempts"][0]["evidence"]["last_observed_ref"]
    assert persisted["size"] == path.stat().st_size


def test_wpm2_overflow_cursor_crash_before_and_after_commit(wpm2_db, tmp_path):
    path, cursor = _overflow_file(tmp_path)
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    evidence = {"resolution_kind": "binding", "last_observed_ref": cursor,
                "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1}}
    message, attempt = _settled(wpm2_db, evidence=evidence)
    with (
        patch("cli_agent_orchestrator.services.inbox_service."
              "advance_wpm2_continuity_cursor", return_value="busy_aborted"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service."
              "send_prepared_input") as paste,
    ):
        InboxService().deliver_pending("receiver")
    assert paste.call_count == 0
    assert json.loads(_attempt_row(wpm2_db, attempt).evidence)["last_observed_ref"] == cursor

    cycle = BoundaryObservation("epoch", TerminalStatus.COMPLETED, 3, 1, 4, 2, 4)
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    submitted = _observation(TerminalStatus.COMPLETED, "epoch", 5)
    resolution = TranscriptResolution(
        path, "binding", path.stat().st_ino,
        live_reference=TranscriptLiveReference(path, path.stat().st_ino, path.stat().st_size))

    def send(*_args, **kwargs):
        kwargs["on_submitted"](submitted)
        return submitted

    with (
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="payload"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send) as paste,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", {**transcript_ref(resolution),
                                       "kind": "transcript_absent"})),
    ):
        monitor.get_boundary_observation.return_value = cycle
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        # Fresh service/process after the pre-commit crash rescans and commits w5.
        InboxService().deliver_pending("receiver")
    after = json.loads(_attempt_row(wpm2_db, attempt).evidence)["last_observed_ref"]
    assert paste.call_count == 1 and after["size"] == path.stat().st_size
    # A second fresh ORM session is the after-commit restart durability proof.
    with wpm2_db() as db:
        reloaded = json.loads(db.get(InboxDeliveryAttemptModel, attempt).evidence)
    assert reloaded["last_observed_ref"] == after
