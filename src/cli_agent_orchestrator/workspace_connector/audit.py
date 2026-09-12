"""Attempt-scoped audit projection (CAO addition, blueprint D5 r3 N4).

The connector records, per attempt: tool name, path or query, result content
digest or refusal code, and time — never the returned file body.  The
projection is the verifier input for D5's branch correlation (run id + result
digest).
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any


class AttemptAudit:
    """Append-only audit projection for one attempt."""

    def __init__(self, attempt_id: str) -> None:
        self.attempt_id = attempt_id
        self._lock = Lock()
        self._sequence = 0
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        tool: str,
        subject: str,
        digest_or_code: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            entry: dict[str, Any] = {
                "attempt_id": self.attempt_id,
                "sequence": self._sequence,
                "tool": tool,
                "subject": subject,  # canonical path or query — never a body
                "result_digest": digest_or_code if digest_or_code.startswith("sha256:") else None,
                "refusal_code": None if digest_or_code.startswith("sha256:") else digest_or_code,
                "recorded_at": time.time(),
            }
            if detail:
                entry["detail"] = dict(detail)
            self.entries.append(entry)
            return dict(entry)

    def refusal(self, *, tool: str, subject: str, code: str) -> dict[str, Any]:
        return self.record(tool=tool, subject=subject, digest_or_code=code)

    def projection(self) -> list[dict[str, Any]]:
        """The audit projection: entries without bodies, by construction."""
        with self._lock:
            return [dict(entry) for entry in self.entries]
