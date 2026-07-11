import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptResolution,
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
