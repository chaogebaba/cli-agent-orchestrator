import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
    _validate_binding,
    confirm_delivery,
    resolve_session_transcript,
    transcript_ref,
    wire_hash,
)


def _record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_binding_precedence_and_inert_candidate_exclusion(tmp_path):
    stub = tmp_path / "launch.jsonl"
    live = tmp_path / "effective.jsonl"
    _record(stub, {"type": "bridge-session"})
    _record(live, {"type": "assistant", "message": {"role": "assistant", "content": "ok"}})
    metadata = {"id": "abcd1234", "provider": "claude_code",
                "provider_session_id": "launch", "working_directory": "/work"}
    binding = {"transcript_path": str(live), "inode": live.stat().st_ino}
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding):
        result = resolve_session_transcript(metadata)
    assert result == TranscriptResolution(live, "binding", live.stat().st_ino)

    rejected = {"transcript_path": str(stub), "inode": stub.stat().st_ino}
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=rejected), patch.object(Path, "home", return_value=tmp_path):
        exact = tmp_path / ".claude/projects/-work/launch.jsonl"
        exact.parent.mkdir(parents=True)
        exact.write_bytes(stub.read_bytes())
        result = resolve_session_transcript(metadata)
    assert result is None or result.path not in {stub, exact}


def test_missing_binding_fallback_keeps_stale_note(tmp_path):
    missing = tmp_path / "missing.jsonl"
    fallback = tmp_path / ".claude/projects/encoding/session.jsonl"
    _record(fallback, {"type": "user", "message": "turn"})
    metadata = {"id": "abcd1234", "provider": "claude_code",
                "provider_session_id": "session", "working_directory": "/different"}
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value={"transcript_path": str(missing), "inode": 1}), \
         patch.object(Path, "home", return_value=tmp_path):
        result = resolve_session_transcript(metadata)
    assert result.path == fallback
    assert result.stale_note == "binding_stale:missing"


def test_zero_binding_inert_exact_id_remains_resolvable(tmp_path):
    stub = tmp_path / ".claude/projects/-work/launch.jsonl"
    _record(stub, {"type": "bridge-session"})
    metadata = {"id": "abcd1234", "provider": "claude_code",
                "provider_session_id": "launch", "working_directory": "/work"}
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=None), patch.object(Path, "home", return_value=tmp_path):
        result = resolve_session_transcript(metadata)
    assert result.path == stub
    assert result.resolution_kind == "exact_id"
    assert result.stale_note is None


def test_confirm_re_resolves_until_inert_binding_becomes_live(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _record(transcript, {"type": "bridge-session"})
    metadata = {"id": "abcd1234", "provider": "claude_code",
                "provider_session_id": "launch", "working_directory": "/work"}
    binding = {"transcript_path": str(transcript), "inode": transcript.stat().st_ino}

    def append_turn():
        time.sleep(0.05)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": "wire"}) + "\n")

    thread = threading.Thread(target=append_turn)
    thread.start()
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding):
        outcome, evidence = confirm_delivery(metadata, wire_hash("wire"), timeout=1)
    thread.join()
    assert outcome == "hit"
    assert evidence["kind"] == "transcript_user_turn"
    assert evidence["resolution_kind"] == "binding"


def test_confirm_permanent_none_positive_timeout_stays_unverified():
    with patch("cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
               return_value=None):
        outcome, evidence = confirm_delivery({}, wire_hash("wire"), timeout=0.02)
    assert outcome == "unverified"
    assert evidence == {"kind": "send_returned_unverified"}


def test_transcript_ref_keeps_resolution_provenance_separate(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _record(transcript, {"type": "user", "message": "wire"})
    evidence = transcript_ref(TranscriptResolution(
        transcript, "binding", transcript.stat().st_ino, "binding_stale:missing"))
    assert evidence["resolution_kind"] == "binding"
    assert evidence["binding_stale"] == "binding_stale:missing"
    assert "kind" not in evidence


def test_null_binding_missing_does_not_screen_fallback(tmp_path):
    missing = tmp_path / ".claude/projects/-work/session.jsonl"
    other = tmp_path / ".claude/projects/other/session.jsonl"
    _record(other, {"type": "user", "message": "turn"})
    metadata = {"id": "term", "provider": "claude_code",
                "provider_session_id": "session", "working_directory": "/work"}
    binding = {"transcript_path": str(missing), "inode": None, "session_id": "session"}
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding), patch.object(Path, "home", return_value=tmp_path):
        result = resolve_session_transcript(metadata)
    assert result.path == other
    assert result.resolution_kind == "uuid_glob"
    assert result.stale_note == "binding_stale:missing"


def test_null_binding_identity_mismatch_and_absent_session_id(tmp_path):
    transcript = tmp_path / "session.jsonl"
    metadata = {"id": "term", "provider": "claude_code",
                "provider_session_id": "wanted", "working_directory": "/none"}
    binding = {"transcript_path": str(transcript), "inode": None, "session_id": "wanted"}
    _record(transcript, {"sessionId": "different", "type": "user", "message": "turn"})
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding):
        assert resolve_session_transcript(metadata) is None
    _record(transcript, {"type": "user", "message": "turn"})
    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding):
        result = resolve_session_transcript(metadata)
    assert result.resolution_kind == "binding"
    assert result.live_reference.inode == transcript.stat().st_ino


def test_same_path_materialization_race_is_revalidated_as_binding(tmp_path):
    transcript = tmp_path / ".claude/projects/-work/session.jsonl"
    metadata = {"id": "term", "provider": "claude_code",
                "provider_session_id": "session", "working_directory": "/work"}
    binding = {"transcript_path": str(transcript), "inode": None, "session_id": "session"}
    real_validate = __import__(
        "cli_agent_orchestrator.services.message_trace_service", fromlist=["_validate_binding"]
    )._validate_binding
    calls = 0

    def materialize(path, session_id, *, check_identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            _record(transcript, {"sessionId": "session", "type": "user", "message": "turn"})
            return "missing", None
        return real_validate(path, session_id, check_identity=check_identity)

    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding), patch.object(Path, "home", return_value=tmp_path), \
         patch("cli_agent_orchestrator.services.message_trace_service._validate_binding",
               side_effect=materialize):
        result = resolve_session_transcript(metadata)
    assert result.resolution_kind == "binding"
    assert result.live_reference.inode == transcript.stat().st_ino


def test_same_path_mismatch_race_skips_only_bound_candidate(tmp_path):
    transcript = tmp_path / ".claude/projects/-work/session.jsonl"
    other = tmp_path / ".claude/projects/other/session.jsonl"
    _record(other, {"type": "user", "message": "other"})
    metadata = {"id": "term", "provider": "claude_code",
                "provider_session_id": "session", "working_directory": "/work"}
    binding = {"transcript_path": str(transcript), "inode": None, "session_id": "session"}
    real_validate = __import__(
        "cli_agent_orchestrator.services.message_trace_service", fromlist=["_validate_binding"]
    )._validate_binding
    calls = 0

    def materialize_mismatch(path, session_id, *, check_identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            _record(transcript, {"sessionId": "different", "type": "user", "message": "turn"})
            return "missing", None
        return real_validate(path, session_id, check_identity=check_identity)

    with patch("cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
               return_value=binding), patch.object(Path, "home", return_value=tmp_path), \
         patch("cli_agent_orchestrator.services.message_trace_service._validate_binding",
               side_effect=materialize_mismatch):
        result = resolve_session_transcript(metadata)
    assert result.path == other
    assert result.resolution_kind == "uuid_glob"
    assert result.stale_note == "binding_stale:missing"


def test_binding_epoch_reseed_hits_using_carried_reference(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    _record(old, {"type": "user", "message": "old"})
    _record(new, {"sessionId": "new", "type": "user", "message": "wire"})
    stat = new.stat()
    resolutions = [
        TranscriptResolution(old, "exact_id"),
        TranscriptResolution(new, "binding", stat.st_ino, live_reference=
                             TranscriptLiveReference(new, stat.st_ino, stat.st_size)),
    ]
    with patch("cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
               side_effect=resolutions), patch(
                   "cli_agent_orchestrator.services.message_trace_service.time.sleep"):
        outcome, evidence = confirm_delivery(
            {}, wire_hash("wire"), expected_ref={"path": str(old),
            "inode": old.stat().st_ino, "size": old.stat().st_size}, timeout=1)
    assert outcome == "hit"
    assert evidence["continuity_reseed"] == "binding_epoch"
    assert evidence["continuity_reseed_old_path"] == str(old)
    assert evidence["continuity_reseed_new_path"] == str(new)


def test_carried_reference_replacement_settles_uncertain(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _record(transcript, {"sessionId": "s", "type": "user", "message": "wire"})
    original = transcript.stat()
    resolution = TranscriptResolution(
        transcript, "binding", original.st_ino,
        live_reference=TranscriptLiveReference(transcript, original.st_ino, original.st_size))
    replacement = tmp_path / "replacement.jsonl"
    _record(replacement, {"sessionId": "other", "type": "user", "message": "wire"})
    replacement.replace(transcript)
    with patch("cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
               return_value=resolution), patch(
                   "cli_agent_orchestrator.services.message_trace_service.time.sleep"):
        outcome, evidence = confirm_delivery(
            {}, wire_hash("wire"), expected_ref={"path": str(tmp_path / "old"),
            "inode": 1, "size": 0}, timeout=0.01)
    assert outcome == "ambiguous"
    assert evidence["kind"] == "transcript_continuity_uncertain"


def test_validation_reference_is_fstat_of_open_fd_during_replacement(tmp_path):
    transcript = tmp_path / "session.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    _record(transcript, {"sessionId": "s", "type": "user", "message": "wire"})
    old_inode = transcript.stat().st_ino
    _record(replacement, {"sessionId": "other", "type": "user", "message": "wire"})
    real_loads = json.loads
    replaced = False

    def replace_after_read(value):
        nonlocal replaced
        parsed = real_loads(value)
        if not replaced:
            replaced = True
            replacement.replace(transcript)
        return parsed

    with patch("cli_agent_orchestrator.services.message_trace_service.json.loads",
               side_effect=replace_after_read):
        reason, reference = _validate_binding(transcript, "s", check_identity=True)
    assert reason is None
    assert reference.inode == old_inode
    assert transcript.stat().st_ino != reference.inode
