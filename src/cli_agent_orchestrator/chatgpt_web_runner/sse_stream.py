"""Safe side projection of ChatGPT's native SSE stream (F862 Amendment D).

This parser never owns or rewrites the caller stream. It receives a copy of
decoded bytes and emits a small allowlist of progress facts; credential-bearing
resume/conduit/proof fields and unknown frames? No: unknown schema freezes the
projection and the detached conversation GET remains authoritative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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
