"""Safe side projection of ChatGPT's native SSE stream (F862 Amendment D).

This parser never owns or rewrites the caller stream. It receives a copy of
decoded bytes and emits a small allowlist of progress facts; credential-bearing
resume/conduit/proof fields and unknown frames? No: unknown schema freezes the
projection and the detached conversation GET remains authoritative.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_SAFE_KEYS = {"message", "status", "model_slug", "thinking_effort", "finish_details"}
_FORBIDDEN_KEY_FRAGMENTS = (
    "token",
    "cookie",
    "authorization",
    "proof",
    "sentinel",
    "conduit",
    "credential",
)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(fragment in low for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                continue
            if low in _SAFE_KEYS or depth > 0:
                out[str(key)] = _safe_value(item, depth=depth + 1)
        return out
    return None


@dataclass
class SSEProjection:
    """Incremental, bounded parser whose output is safe to persist."""

    max_pending_bytes: int = 256 * 1024
    pending: bytearray = field(default_factory=bytearray, repr=False)
    protocol_drift: bool = False
    done: bool = False

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if self.protocol_drift or self.done:
            return []
        self.pending.extend(chunk)
        if len(self.pending) > self.max_pending_bytes:
            self.pending.clear()
            self.protocol_drift = True
            return [{"kind": "protocol_drift", "reason": "frame_too_large"}]
        emitted: list[dict[str, Any]] = []
        while b"\n\n" in self.pending:
            raw, _, rest = self.pending.partition(b"\n\n")
            self.pending = bytearray(rest)
            projected = self._project_frame(bytes(raw))
            if projected is not None:
                emitted.append(projected)
        return emitted

    def _project_frame(self, frame: bytes) -> dict[str, Any] | None:
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError:
            self.protocol_drift = True
            return {"kind": "protocol_drift", "reason": "non_utf8_frame"}
        event = "message"
        data_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()[:80]
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        data = "\n".join(data_lines)
        if data == "[DONE]":
            self.done = True
            return {"kind": "done"}
        if not data:
            return None
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            self.protocol_drift = True
            return {"kind": "protocol_drift", "reason": "non_json_data"}
        safe = _safe_value(decoded)
        if safe in ({}, [], None):
            return None
        return {"kind": "progress", "event": event, "data": safe}


#: Amendment D: the conversation and assistant ids come from the stream PYTHON
#: drained, never from the browser's address bar or the app's own GET. The safe
#: projection deliberately drops ``conversation_id`` (it is not in the depth-0
#: allowlist), so this scanner is the seam that recovers exactly the two
#: identifiers the authoritative GET needs — and nothing else.
_CONVERSATION_ID_RE = re.compile(r'"conversation_id"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})"')
_ASSISTANT_ID_RE = re.compile(
    r'"id"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})"'
    r'\s*,\s*"author"\s*:\s*\{\s*"role"\s*:\s*"assistant"'
)


@dataclass
class StreamIdScanner:
    """Recover the conversation / assistant ids from the raw origin stream.

    Bounded and write-only: it keeps at most one trailing window of bytes so an
    identifier split across two chunks is still matched, and it retains nothing
    but the two ids. It never sees a header, cookie or token, because it is fed
    only the response body.
    """

    #: Enough to span any single frame carrying the ids, never the whole stream.
    window_bytes: int = 16 * 1024
    conversation_id: Optional[str] = None
    assistant_message_id: Optional[str] = None
    _tail: str = ""

    def feed(self, chunk: bytes) -> None:
        if self.conversation_id and self.assistant_message_id:
            return
        try:
            text = self._tail + chunk.decode("utf-8", errors="ignore")
        except Exception:  # pragma: no cover - decode errors are swallowed above
            return
        if self.conversation_id is None:
            match = _CONVERSATION_ID_RE.search(text)
            if match:
                self.conversation_id = match.group(1)
        if self.assistant_message_id is None:
            match = _ASSISTANT_ID_RE.search(text)
            if match:
                self.assistant_message_id = match.group(1)
        self._tail = text[-self.window_bytes :]
