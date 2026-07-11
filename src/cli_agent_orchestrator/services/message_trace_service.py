"""Provider transcript evidence for honest inbox delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from cli_agent_orchestrator.clients.database import update_terminal_provider_session_id_if_null

logger = logging.getLogger(__name__)
_unresolved_warned: set[str] = set()
_unresolved_warned_lock = threading.Lock()


def wire_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fd_codex_session(metadata: dict) -> str | None:
    try:
        from cli_agent_orchestrator.services.fork_context_service import _descendants, pane_pid
        candidates = set()
        for pid in _descendants(pane_pid(metadata["tmux_session"], metadata["tmux_window"])):
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    path = Path(os.readlink(fd)).resolve()
                    if ("/.codex/sessions/" in str(path) and
                            path.name.startswith("rollout-") and path.suffix == ".jsonl"):
                        first = json.loads(path.open(encoding="utf-8").readline())
                        session_id = first.get("payload", {}).get("id")
                        if first.get("type") == "session_meta" and session_id in path.name:
                            candidates.add(session_id)
                except (OSError, KeyError, json.JSONDecodeError):
                    pass
        return next(iter(candidates)) if len(candidates) == 1 else None
    except Exception:
        return None


def resolve_session_transcript(metadata: dict) -> Path | None:
    provider = metadata.get("provider")
    session_id = metadata.get("provider_session_id")
    if provider == "codex" and not session_id:
        session_id = _fd_codex_session(metadata)
        if session_id:
            session_id = update_terminal_provider_session_id_if_null(
                metadata["id"], session_id)
            metadata["provider_session_id"] = session_id
    if not session_id:
        terminal_id = str(metadata.get("id") or "unknown")
        with _unresolved_warned_lock:
            first_warning = terminal_id not in _unresolved_warned
            _unresolved_warned.add(terminal_id)
        if first_warning:
            logger.warning(
                "No authoritative session transcript is resolvable for terminal %s; "
                "deliveries will be recorded as send_returned_unverified",
                terminal_id,
            )
        return None
    if provider == "codex":
        matches = list((Path.home() / ".codex" / "sessions").glob(f"**/*{session_id}*.jsonl"))
        return matches[0] if len(matches) == 1 else None
    cwd = metadata.get("working_directory") or metadata.get("cwd")
    if not cwd:
        try:
            from cli_agent_orchestrator.backends.registry import get_backend
            cwd = get_backend().get_pane_working_directory(
                metadata["tmux_session"], metadata["tmux_window"])
        except Exception:
            cwd = None
    if provider == "grok_cli" and cwd:
        path = Path.home() / ".grok" / "sessions" / quote(cwd, safe="") / session_id / "chat_history.jsonl"
        return path
    if provider == "claude_code" and cwd:
        encoded = cwd.replace("/", "-")
        return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
    return None


def transcript_ref(path: Path | None) -> dict:
    if path is None:
        return {"kind": "send_returned_unverified"}
    try:
        stat = path.stat()
        return {"path": str(path), "inode": stat.st_ino, "size": stat.st_size}
    except OSError:
        return {"path": str(path), "inode": None, "size": 0}


def _content_texts(content) -> list[str]:
    """Extract text only from a native user message's content field."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            texts.append(text)
    if len(texts) > 1:
        texts.append("".join(texts))
    return texts


def _native_user_turn_texts(record: dict) -> list[str]:
    """Return payload candidates from provider-native user-turn fields only."""
    record_type = record.get("type")
    payload = record.get("payload")

    # Claude JSONL: type=user, message={role:user, content:...}. Older/native
    # rows may store the exact user text directly in message.
    if record_type == "user":
        message = record.get("message")
        if isinstance(message, str):
            return [message]
        if isinstance(message, dict) and message.get("role") in (None, "user"):
            return _content_texts(message.get("content"))
        return _content_texts(record.get("content"))

    # Codex rollout response_item user message.
    if record_type == "response_item" and isinstance(payload, dict):
        if payload.get("role") == "user":
            return _content_texts(payload.get("content"))

    # Codex rollout event_msg user_message.
    if record_type == "event_msg" and isinstance(payload, dict):
        if payload.get("type") == "user_message":
            message = payload.get("message")
            return [message] if isinstance(message, str) else []

    # Grok chat history: a role=user message, or its native <user_query> block.
    if record.get("role") == "user":
        value = record.get("message", record.get("content"))
        return _content_texts(value)
    message = record.get("message")
    if (isinstance(message, str) and message.startswith("<user_query>") and
            message.endswith("</user_query>")):
        value = message[len("<user_query>"):-len("</user_query>")]
        if value.startswith("\n"):
            value = value[1:]
        if value.endswith("\n"):
            value = value[:-1]
        return [value]
    return []


def transcript_lookup(path: Path, payload_hash: str, started_at=None,
                      expected_ref: dict | None = None) -> tuple[str, dict]:
    """Return hit, absent, or unresolved with bounded continuity evidence."""
    try:
        before = path.stat()
        raw_bytes = path.read_bytes()
        after = path.stat()
        if (before.st_ino != after.st_ino or after.st_size < before.st_size or
            (expected_ref and expected_ref.get("inode") not in (None, before.st_ino)) or
            (expected_ref and before.st_size < int(expected_ref.get("size", 0)))):
            return "unresolved", {"kind": "transcript_continuity_uncertain", "path": str(path)}
    except OSError:
        return "unresolved", {"kind": "transcript_unreadable", "path": str(path)}
    try:
        baseline_size = int((expected_ref or {}).get("size") or 0)
        if baseline_size > len(raw_bytes):
            return "unresolved", {"kind": "transcript_continuity_uncertain", "path": str(path)}
        raw = raw_bytes[baseline_size:].decode("utf-8")
        threshold = started_at
        if isinstance(threshold, str):
            threshold = datetime.fromisoformat(threshold.replace("Z", "+00:00"))
        if threshold is not None and threshold.tzinfo is None:
            threshold = threshold.replace(tzinfo=timezone.utc)
        byte_offset = baseline_size
        for line in raw.splitlines(keepends=True):
            line_offset = byte_offset
            byte_offset += len(line.encode("utf-8"))
            obj = json.loads(line)
            candidates = _native_user_turn_texts(obj)
            if not candidates:
                continue
            stamp = obj.get("timestamp") or obj.get("created_at")
            if threshold is not None and isinstance(stamp, str):
                try:
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when < threshold:
                        continue
                except ValueError:
                    return "unresolved", {"kind": "transcript_malformed_timestamp", "path": str(path)}
            for candidate in candidates:
                normalized = candidate
                if normalized.startswith("<user_query>") and normalized.endswith("</user_query>"):
                    normalized = normalized[len("<user_query>"):-len("</user_query>")]
                    if normalized.startswith("\n"):
                        normalized = normalized[1:]
                    if normalized.endswith("\n"):
                        normalized = normalized[:-1]
                if wire_hash(normalized) == payload_hash:
                    return "hit", {"kind": "transcript_user_turn", "path": str(path),
                                   "offset": line_offset, "inode": after.st_ino,
                                   "size": after.st_size}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unresolved", {"kind": "transcript_malformed", "path": str(path)}
    return "absent", {"kind": "transcript_absent", "path": str(path),
                      "inode": after.st_ino, "size": after.st_size}


def confirm_delivery(metadata: dict, payload_hash: str, started_at=None,
                     expected_ref: dict | None = None,
                     timeout: float = 10.0) -> tuple[str, dict]:
    path = resolve_session_transcript(metadata)
    if path is None:
        return "unverified", {"kind": "send_returned_unverified"}
    deadline = time.monotonic() + timeout
    last = ("unresolved", {"kind": "transcript_unreadable", "path": str(path)})
    continuity_ref = dict(expected_ref or {})
    while time.monotonic() < deadline:
        last = transcript_lookup(path, payload_hash, started_at, continuity_ref)
        if last[0] == "hit":
            return last
        if last[0] == "absent":
            # An absent-start reference has no inode to protect. Pin the first
            # readable file immediately so later replacement/truncation cannot
            # manufacture authoritative evidence under the same attempt.
            continuity_ref = {
                **continuity_ref,
                "path": last[1].get("path", str(path)),
                "inode": last[1].get("inode"),
                "size": last[1].get("size", 0),
            }
        time.sleep(0.25)
    return "ambiguous", last[1]
