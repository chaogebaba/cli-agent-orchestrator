"""Regression pins for MSGTRACE's provider-native acceptance oracle."""

import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.message_trace_service import (
    _fd_codex_session, confirm_delivery, resolve_session_transcript, transcript_lookup,
    transcript_ref, wire_hash,
)
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

START = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _write(path: Path, *records: dict) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.mark.parametrize(("record", "payload"), [
    ({"type": "user", "timestamp": "2026-07-11T00:00:01Z", "message": "line 1\nline 2"}, "line 1\nline 2"),
    ({"type": "response_item", "timestamp": "2026-07-11T00:00:01Z", "payload": {"role": "user", "content": "pasted full text"}}, "pasted full text"),
    ({"timestamp": "2026-07-11T00:00:01Z", "message": "<user_query>\ngrok turn\n</user_query>"}, "grok turn"),
])
def test_provider_user_turns_match_exact_shaped_hash_after_start(tmp_path, record, payload):
    transcript = tmp_path / "session.jsonl"
    _write(transcript, record)
    result, evidence = transcript_lookup(transcript, wire_hash(payload), START)
    assert result == "hit"
    assert evidence["kind"] == "transcript_user_turn"


def test_claude_queued_command_attachment_matches_exact_wire_hash(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(transcript, {
        "type": "attachment",
        "timestamp": "2026-07-11T00:00:01Z",
        "attachment": {"type": "queued_command", "prompt": "exact queued wire"},
    })
    result, evidence = transcript_lookup(
        transcript, wire_hash("exact queued wire"), START)
    assert result == "hit"
    assert evidence["kind"] == "transcript_queued_command"


def test_native_user_turn_has_priority_over_earlier_queued_command(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(
        transcript,
        {
            "type": "attachment",
            "timestamp": "2026-07-11T00:00:01Z",
            "attachment": {"type": "queued_command", "prompt": "same wire"},
        },
        {"type": "user", "timestamp": "2026-07-11T00:00:02Z", "message": "same wire"},
    )
    result, evidence = transcript_lookup(transcript, wire_hash("same wire"), START)
    assert result == "hit"
    assert evidence["kind"] == "transcript_user_turn"


def test_queue_operation_record_remains_non_confirming(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(transcript, {
        "type": "queue-operation",
        "timestamp": "2026-07-11T00:00:01Z",
        "operation": "enqueue",
        "content": "same wire",
    })
    assert transcript_lookup(transcript, wire_hash("same wire"), START)[0] == "absent"


def test_malformed_unrelated_timestamp_before_valid_native_turn_is_ignored(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(
        transcript,
        {"type": "assistant", "timestamp": "not-a-timestamp", "message": "noise"},
        {"type": "user", "timestamp": "2026-07-11T00:00:01Z", "message": "wire"},
    )
    result, evidence = transcript_lookup(transcript, wire_hash("wire"), START)
    assert result == "hit"
    assert evidence["kind"] == "transcript_user_turn"


def test_malformed_unrelated_timestamp_before_valid_queued_command_is_ignored(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(
        transcript,
        {"type": "metadata", "timestamp": "not-a-timestamp", "value": "noise"},
        {
            "type": "attachment",
            "timestamp": "2026-07-11T00:00:01Z",
            "attachment": {"type": "queued_command", "prompt": "wire"},
        },
    )
    result, evidence = transcript_lookup(transcript, wire_hash("wire"), START)
    assert result == "hit"
    assert evidence["kind"] == "transcript_queued_command"


def test_grok_type_user_block_list_matches_wire_hash(tmp_path):
    transcript = tmp_path / "chat_history.jsonl"
    _write(transcript, {
        "type": "user",
        "timestamp": "2026-07-11T00:00:01Z",
        "content": [{"type": "text", "text": "<user_query>\nGROK_WIRE\n</user_query>"}],
    })
    assert transcript_lookup(transcript, wire_hash("GROK_WIRE"), START)[0] == "hit"


def test_grok_role_user_block_list_matches_wire_hash(tmp_path):
    transcript = tmp_path / "chat_history.jsonl"
    _write(transcript, {
        "role": "user",
        "timestamp": "2026-07-11T00:00:01Z",
        "content": [{"type": "text", "text": "<user_query>\nGROK_WIRE\n</user_query>"}],
    })
    assert transcript_lookup(transcript, wire_hash("GROK_WIRE"), START)[0] == "hit"


def test_paste_without_enter_is_absent_and_old_turn_is_ignored(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(transcript, {"type": "user", "timestamp": "2026-07-10T23:59:59Z", "message": "wire"})
    assert transcript_lookup(transcript, wire_hash("wire"), START)[0] == "absent"
    assert transcript_lookup(transcript, wire_hash("paste never submitted"), START)[0] == "absent"


def test_assistant_echo_with_user_facing_note_is_not_a_user_turn(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(transcript, {
        "role": "assistant",
        "content": "exact-wire",
        "note": "user-facing",
    })
    assert transcript_lookup(transcript, wire_hash("exact-wire"), START)[0] == "absent"


def test_wrapper_normalization_preserves_literal_wrapper_like_payload(tmp_path):
    transcript = tmp_path / "chat_history.jsonl"
    literal = "prefix <user_query>literal</user_query> suffix"
    _write(transcript, {"role": "user", "timestamp": "2026-07-11T00:00:01Z", "message": f"<user_query>{literal}</user_query>"})
    assert transcript_lookup(transcript, wire_hash(literal), START)[0] == "hit"
    assert transcript_lookup(transcript, wire_hash("literal"), START)[0] == "absent"


def test_malformed_tail_is_unresolved_never_proven_absent(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":"different"}\n{"partial":', encoding="utf-8")
    result, evidence = transcript_lookup(transcript, wire_hash("wire"), START)
    assert result == "unresolved"
    assert evidence["kind"] == "transcript_malformed"


def test_inode_swap_and_truncation_are_unresolved(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write(transcript, {"type": "user", "message": "different"})
    original = transcript_ref(transcript)
    transcript.unlink()
    _write(transcript, {"type": "user", "message": "wire"})
    assert transcript_lookup(transcript, wire_hash("wire"), START, original)[0] == "unresolved"
    current = transcript_ref(transcript)
    current["size"] += 100
    assert transcript_lookup(transcript, wire_hash("wire"), START, current)[0] == "unresolved"


def test_confirm_delivery_no_oracle_is_explicit_unverified():
    with patch("cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript", return_value=None):
        assert confirm_delivery({}, wire_hash("wire"), timeout=0)[0] == "unverified"


def test_confirmation_hard_deadline_returns_without_polling():
    with (
        patch("cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
              return_value=Path("/trace")),
        patch("cli_agent_orchestrator.services.message_trace_service.transcript_lookup") as lookup,
    ):
        result, _evidence = confirm_delivery({}, wire_hash("wire"), timeout=0)
    assert result == "ambiguous"
    lookup.assert_not_called()


def test_absent_start_poll_pins_first_readable_inode_before_swap():
    absent_start = {"path": "/trace", "inode": None, "size": 0}
    absent = ("absent", {"kind": "transcript_absent", "path": "/trace",
                         "inode": 101, "size": 12})
    swapped = ("unresolved", {"kind": "transcript_continuity_uncertain",
                              "path": "/trace"})
    with (
        patch("cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
              return_value=Path("/trace")),
        patch("cli_agent_orchestrator.services.message_trace_service.transcript_lookup",
              side_effect=[absent, swapped]) as lookup,
        patch("cli_agent_orchestrator.services.message_trace_service.time.monotonic",
              side_effect=[0.0, 0.0, 0.5, 2.0]),
        patch("cli_agent_orchestrator.services.message_trace_service.time.sleep"),
    ):
        result, evidence = confirm_delivery(
            {}, wire_hash("wire"), START, absent_start, timeout=1.0)
    assert result == "ambiguous"
    assert evidence["kind"] == "transcript_continuity_uncertain"
    assert lookup.call_args_list[0].args[3] == absent_start
    assert lookup.call_args_list[1].args[3]["inode"] == 101
    assert lookup.call_args_list[1].args[3]["size"] == 12


def test_resolve_provider_transcripts_uses_exact_session_ids(tmp_path):
    claude = {
        "provider": "claude_code",
        "provider_session_id": "c-id",
        "working_directory": "/home/user/work_space/.claude/repo",
    }
    grok = {"provider": "grok_cli", "provider_session_id": "g-id", "working_directory": "/work/repo"}
    encoded = tmp_path / ".claude/projects/-home-user-work-space--claude-repo/c-id.jsonl"
    encoded.parent.mkdir(parents=True)
    encoded.touch()
    fallback = tmp_path / ".claude/projects/drifted-convention/c-id.jsonl"
    fallback.parent.mkdir()
    fallback.touch()
    with patch.object(Path, "home", return_value=tmp_path):
        assert resolve_session_transcript(claude) == encoded
        assert resolve_session_transcript(grok) == tmp_path / ".grok/sessions/%2Fwork%2Frepo/g-id/chat_history.jsonl"


def test_resolve_claude_transcript_falls_back_across_encoding_drift(tmp_path):
    transcript = tmp_path / ".claude/projects/new_encoding/session-id.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()
    metadata = {
        "provider": "claude_code",
        "provider_session_id": "session-id",
        "working_directory": "/work_space/.repo",
    }

    with patch.object(Path, "home", return_value=tmp_path):
        assert resolve_session_transcript(metadata) == transcript


def test_codex_null_session_uses_fd_capture_only_and_never_overwrites_non_null(tmp_path):
    metadata = {"id": "term", "provider": "codex", "provider_session_id": None}
    with patch("cli_agent_orchestrator.services.message_trace_service._fd_codex_session", return_value=None), patch("cli_agent_orchestrator.services.message_trace_service.update_terminal_provider_session_id_if_null") as update:
        assert resolve_session_transcript(metadata) is None
        update.assert_not_called()


def test_codex_lazy_self_heal_uses_persisted_cas_winner(tmp_path):
    metadata = {"id": "term", "provider": "codex", "provider_session_id": None}
    winner = tmp_path / ".codex/sessions/rollout-winner.jsonl"
    winner.parent.mkdir(parents=True)
    winner.touch()
    with (
        patch.object(Path, "home", return_value=tmp_path),
        patch("cli_agent_orchestrator.services.message_trace_service._fd_codex_session",
              return_value="stale"),
        patch("cli_agent_orchestrator.services.message_trace_service."
              "update_terminal_provider_session_id_if_null", return_value="winner") as update,
    ):
        assert resolve_session_transcript(metadata) == winner
    update.assert_called_once_with("term", "stale")
    assert metadata["provider_session_id"] == "winner"
    metadata["provider_session_id"] = "owned"
    rollout = tmp_path / ".codex/sessions/rollout-owned.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.touch()
    with patch.object(Path, "home", return_value=tmp_path), patch("cli_agent_orchestrator.services.message_trace_service._fd_codex_session") as capture, patch("cli_agent_orchestrator.services.message_trace_service.update_terminal_provider_session_id_if_null") as update:
        assert resolve_session_transcript(metadata) == rollout
        capture.assert_not_called()
        update.assert_not_called()


def test_claude_allocated_session_launch_resolves_first_accepted_turn(tmp_path):
    provider = ClaudeCodeProvider("term", "session", "window")
    command = shlex.split(provider._build_claude_command())
    session_id = provider.allocated_session_uuid
    assert command[command.index("--session-id") + 1] == session_id

    transcript = tmp_path / ".claude/projects/-work-repo" / f"{session_id}.jsonl"
    assert not transcript.exists()
    transcript.parent.mkdir(parents=True)
    _write(transcript, {
        "type": "user",
        "timestamp": "2026-07-11T00:00:01Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "accepted"}]},
    })
    metadata = {
        "id": "term", "provider": "claude_code",
        "provider_session_id": session_id, "working_directory": "/work/repo",
    }
    with patch.object(Path, "home", return_value=tmp_path):
        resolved = resolve_session_transcript(metadata)
    assert resolved == transcript
    assert transcript_lookup(resolved, wire_hash("accepted"), START)[0] == "hit"


def test_codex_fd_capture_owns_descendant_and_rejects_same_cwd_decoy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    owned_id = "11111111-1111-1111-1111-111111111111"
    decoy_id = "22222222-2222-2222-2222-222222222222"
    owned = tmp_path / ".codex/sessions/2026/07/11" / f"rollout-{owned_id}.jsonl"
    decoy = tmp_path / ".codex/sessions/2026/07/11" / f"rollout-{decoy_id}.jsonl"
    owned.parent.mkdir(parents=True)
    _write(owned, {"type": "session_meta", "payload": {"id": owned_id, "cwd": "/same/cwd"}})
    _write(decoy, {"type": "session_meta", "payload": {"id": decoy_id, "cwd": "/same/cwd"}})

    fd_dir = tmp_path / "proc/42/fd"
    fd_dir.mkdir(parents=True)
    fd = fd_dir / "9"
    fd.touch()
    real_iterdir = Path.iterdir
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service.pane_pid", lambda *_a: 10)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service._descendants", lambda _pid: [42])
    monkeypatch.setattr(Path, "iterdir", lambda path: real_iterdir(fd_dir)
                        if str(path) == "/proc/42/fd" else real_iterdir(path))
    real_readlink = os.readlink
    monkeypatch.setattr(os, "readlink", lambda path: str(owned)
                        if Path(path) == fd else real_readlink(path))

    assert _fd_codex_session({"tmux_session": "s", "tmux_window": "w"}) == owned_id
    assert decoy_id != owned_id
