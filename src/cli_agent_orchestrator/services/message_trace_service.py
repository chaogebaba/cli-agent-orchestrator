"""Provider transcript evidence for honest inbox delivery."""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Literal

from cli_agent_orchestrator.clients.database import (
    get_current_transcript_binding,
    update_terminal_provider_session_id_if_null,
)

logger = logging.getLogger(__name__)
_unresolved_warned: set[str] = set()
_unresolved_warned_lock = threading.Lock()


@dataclass(frozen=True, eq=False)
class TranscriptLiveReference:
    path: Path
    inode: int
    size: int


@dataclass(frozen=True, eq=False)
class TranscriptResolution:
    path: Path
    resolution_kind: Literal["binding", "exact_id", "uuid_glob"]
    inode: int | None = None
    stale_note: str | None = None
    live_reference: TranscriptLiveReference | None = None

    def __eq__(self, other) -> bool:
        if isinstance(other, TranscriptResolution):
            return (
                self.path, self.resolution_kind, self.inode, self.stale_note
            ) == (other.path, other.resolution_kind, other.inode, other.stale_note)
        if isinstance(other, Path):
            return self.path == other
        return NotImplemented


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


def _binding_path_state(path: Path) -> str | None:
    """Return a stale reason, or None when a binding has native conversation rows."""
    try:
        if not path.is_file():
            return "missing"
        found_native = False
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record.get("type") in {"user", "assistant"}:
                    found_native = True
                    break
                message = record.get("message")
                if isinstance(message, dict) and message.get("role") in {"user", "assistant"}:
                    found_native = True
                    break
        return None if found_native else "inert"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unreadable"


def _validate_binding(
    path: Path, session_id: str | None, *, check_identity: bool
) -> tuple[str | None, TranscriptLiveReference | None]:
    """Validate one binding against one descriptor and carry its exact identity."""
    try:
        with path.open("rb") as stream:
            first_line = stream.readline()
            first = json.loads(first_line) if first_line else {}
            if check_identity:
                recorded_session = first.get("sessionId")
                if recorded_session is not None and recorded_session != session_id:
                    return "session_mismatch", None
            found_native = False
            for raw_line in itertools.chain((first_line,), stream):
                if not raw_line:
                    continue
                record = json.loads(raw_line)
                if record.get("type") in {"user", "assistant"}:
                    found_native = True
                    break
                message = record.get("message")
                if isinstance(message, dict) and message.get("role") in {"user", "assistant"}:
                    found_native = True
                    break
            stat = os.fstat(stream.fileno())
            reference = TranscriptLiveReference(path, stat.st_ino, stat.st_size)
            return (None, reference) if found_native else ("inert", reference)
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unreadable", None


def resolve_session_transcript(metadata: dict) -> TranscriptResolution | None:
    provider = metadata.get("provider")
    session_id = metadata.get("provider_session_id")
    binding = get_current_transcript_binding(str(metadata.get("id") or ""))
    stale_note = None
    excluded: set[Path] = set()
    screen_fallback = False
    if binding is not None:
        binding_path = Path(binding["transcript_path"])
        deferred_inode = binding.get("inode") is None
        stale_reason, live_reference = _validate_binding(
            binding_path, binding.get("session_id"), check_identity=deferred_inode
        )
        if stale_reason is None:
            return TranscriptResolution(
                binding_path,
                "binding",
                inode=(live_reference.inode if deferred_inode else int(binding["inode"])),
                live_reference=live_reference,
            )
        stale_note = f"binding_stale:{stale_reason}"
        if stale_reason in {"inert", "unreadable"}:
            excluded.add(binding_path.resolve(strict=False))
            screen_fallback = True

    def candidate(path: Path, kind: Literal["exact_id", "uuid_glob"]):
        resolved = path.resolve(strict=False)
        if (binding is not None and stale_note in {
                "binding_stale:missing", "binding_stale:session_mismatch"
        } and resolved == binding_path.resolve(strict=False)):
            reason, reference = _validate_binding(
                binding_path, binding.get("session_id"), check_identity=True
            )
            if reason is None:
                return TranscriptResolution(
                    binding_path, "binding", inode=reference.inode,
                    stale_note=stale_note, live_reference=reference,
                )
            return None
        if screen_fallback and (
            resolved in excluded or _binding_path_state(path) is not None
        ):
            return None
        return TranscriptResolution(path, kind, stale_note=stale_note)

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
        return candidate(matches[0], "uuid_glob") if len(matches) == 1 else None
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
        return candidate(path, "exact_id")
    if provider == "claude_code" and cwd:
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        projects = Path.home() / ".claude" / "projects"
        path = projects / encoded / f"{session_id}.jsonl"
        if path.exists():
            result = candidate(path, "exact_id")
            if result is not None:
                return result
        matches = list(projects.glob(f"*/{session_id}.jsonl"))
        for match in sorted(matches, key=lambda item: item.stat().st_mtime_ns, reverse=True):
            result = candidate(match, "uuid_glob")
            if result is not None:
                return result
    return None


def transcript_ref(resolution: TranscriptResolution | None) -> dict:
    if resolution is None:
        return {"kind": "send_returned_unverified"}
    if isinstance(resolution, Path):
        resolution = TranscriptResolution(resolution, "exact_id")
    path = resolution.path
    try:
        if resolution.resolution_kind == "binding" and resolution.live_reference is not None:
            reference = resolution.live_reference
            evidence = {
                "path": str(reference.path),
                "inode": reference.inode,
                "size": reference.size,
                "resolution_kind": resolution.resolution_kind,
            }
            if resolution.stale_note:
                evidence["binding_stale"] = resolution.stale_note
            return evidence
        stat = path.stat()
        evidence = {
            "path": str(path),
            "inode": resolution.inode if resolution.resolution_kind == "binding" else stat.st_ino,
            "size": stat.st_size,
            "resolution_kind": resolution.resolution_kind,
        }
    except OSError:
        evidence = {"path": str(path), "inode": None, "size": 0,
                    "resolution_kind": resolution.resolution_kind}
    if resolution.stale_note:
        evidence["binding_stale"] = resolution.stale_note
    return evidence


def _with_resolution_evidence(evidence: dict, resolution: TranscriptResolution) -> dict:
    merged = {**evidence, "resolution_kind": resolution.resolution_kind}
    if resolution.stale_note:
        merged["binding_stale"] = resolution.stale_note
    return merged


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


def _queued_command_prompt(record: dict) -> str | None:
    """Return Claude's stable queued-command prompt field, when present."""
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    prompt = attachment.get("prompt")
    return prompt if isinstance(prompt, str) else None


def transcript_lookup(path: Path, payload_hash: str, started_at=None,
                      expected_ref: dict | None = None,
                      scan_from_start: bool = False) -> tuple[str, dict]:
    """Return hit, absent, or unresolved with bounded continuity evidence."""
    if isinstance(path, TranscriptResolution):
        path = path.path
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
        baseline_size = 0 if scan_from_start else int((expected_ref or {}).get("size") or 0)
        if baseline_size > len(raw_bytes):
            return "unresolved", {"kind": "transcript_continuity_uncertain", "path": str(path)}
        raw = raw_bytes[baseline_size:].decode("utf-8")
        threshold = started_at
        if isinstance(threshold, str):
            threshold = datetime.fromisoformat(threshold.replace("Z", "+00:00"))
        if threshold is not None and threshold.tzinfo is None:
            threshold = threshold.replace(tzinfo=timezone.utc)
        byte_offset = baseline_size
        queued_command_evidence = None
        for line in raw.splitlines(keepends=True):
            line_offset = byte_offset
            byte_offset += len(line.encode("utf-8"))
            obj = json.loads(line)
            candidates = _native_user_turn_texts(obj)
            queued_prompt = _queued_command_prompt(obj)
            if not candidates and queued_prompt is None:
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
            if (queued_command_evidence is None and queued_prompt is not None and
                    wire_hash(queued_prompt) == payload_hash):
                queued_command_evidence = {
                    "kind": "transcript_queued_command",
                    "path": str(path),
                    "offset": line_offset,
                    "inode": after.st_ino,
                    "size": after.st_size,
                }
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unresolved", {"kind": "transcript_malformed", "path": str(path)}
    if queued_command_evidence is not None:
        return "hit", queued_command_evidence
    return "absent", {"kind": "transcript_absent", "path": str(path),
                      "inode": after.st_ino, "size": after.st_size}


def continuity_aware_lookup(
    metadata: dict, payload_hash: str, started_at=None,
    expected_ref: dict | None = None,
) -> tuple[str, dict]:
    """Resolve and search one transcript epoch without cross-epoch size comparison."""
    resolution = resolve_session_transcript(metadata)
    if resolution is None:
        return "unresolved", {"kind": "transcript_unresolved"}
    if isinstance(resolution, Path):
        resolution = TranscriptResolution(resolution, "exact_id")
    continuity_ref = dict(expected_ref or {})
    current_path = continuity_ref.get("path")
    reseed = (
        resolution.resolution_kind == "binding"
        and resolution.live_reference is not None
        and bool(current_path)
        and Path(str(current_path)) != resolution.path
    )
    extra: dict[str, str] = {}
    if reseed:
        old_path = str(current_path)
        reference = resolution.live_reference
        continuity_ref = {
            "path": str(reference.path), "inode": reference.inode, "size": reference.size,
        }
        extra = {
            "continuity_reseed": "binding_epoch",
            "continuity_reseed_old_path": old_path,
            "continuity_reseed_new_path": str(reference.path),
        }
    outcome, evidence = transcript_lookup(
        resolution.path, payload_hash, started_at, continuity_ref,
        scan_from_start=reseed,
    )
    return outcome, _with_resolution_evidence({**evidence, **extra}, resolution)


def confirm_delivery(metadata: dict, payload_hash: str, started_at=None,
                     expected_ref: dict | None = None,
                     timeout: float = 10.0) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout
    last = ("unverified", {"kind": "send_returned_unverified"})
    continuity_ref = dict(expected_ref or {})
    saw_resolution = False
    if timeout <= 0:
        resolution = resolve_session_transcript(metadata)
        if resolution is None:
            return last
        if isinstance(resolution, Path):
            resolution = TranscriptResolution(resolution, "exact_id")
        saw_resolution = True
        return "ambiguous", _with_resolution_evidence(
            {"kind": "transcript_unreadable", "path": str(resolution.path)}, resolution
        )
    while time.monotonic() < deadline:
        resolution = resolve_session_transcript(metadata)
        if resolution is None:
            time.sleep(0.25)
            continue
        if isinstance(resolution, Path):
            resolution = TranscriptResolution(resolution, "exact_id")
        saw_resolution = True
        outcome, evidence = continuity_aware_lookup(
            metadata, payload_hash, started_at, continuity_ref)
        last = (outcome, evidence)
        if last[0] == "hit":
            return last
        if last[0] == "absent":
            # An absent-start reference has no inode to protect. Pin the first
            # readable file immediately so later replacement/truncation cannot
            # manufacture authoritative evidence under the same attempt.
            continuity_ref = {
                **continuity_ref,
                    "path": last[1].get("path", str(resolution.path)),
                "inode": last[1].get("inode"),
                "size": last[1].get("size", 0),
            }
        time.sleep(0.25)
    if not saw_resolution:
        return "unverified", {"kind": "send_returned_unverified"}
    return "ambiguous", last[1]
