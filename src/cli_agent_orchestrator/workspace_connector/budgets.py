"""Pull budgets (CAO addition, blueprint D5 — replaces AC-10).

Per result ``CHATGPT_PULL_RESULT_BYTES`` (provisional 64 KiB, paginated beyond
it) and per attempt aggregate ``CHATGPT_PULL_ATTEMPT_BYTES`` (provisional
2 MiB) with ``CHATGPT_PULL_ATTEMPT_CALLS`` (provisional 64).  Exceeding an
aggregate cap refuses ``PULL_BUDGET_EXHAUSTED``.
"""

from __future__ import annotations

import os
from threading import Lock

RESULT_BYTES_DEFAULT = 64 * 1024
ATTEMPT_BYTES_DEFAULT = 2 * 1024 * 1024
ATTEMPT_CALLS_DEFAULT = 64

PULL_BUDGET_EXHAUSTED = "PULL_BUDGET_EXHAUSTED"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class PullBudget:
    """Per-attempt aggregate pull budget (bytes and call count)."""

    def __init__(
        self,
        *,
        attempt_bytes: int | None = None,
        attempt_calls: int | None = None,
    ) -> None:
        self.attempt_bytes_limit = (
            attempt_bytes
            if attempt_bytes is not None
            else _env_int("CHATGPT_PULL_ATTEMPT_BYTES", ATTEMPT_BYTES_DEFAULT)
        )
        self.attempt_calls_limit = (
            attempt_calls
            if attempt_calls is not None
            else _env_int("CHATGPT_PULL_ATTEMPT_CALLS", ATTEMPT_CALLS_DEFAULT)
        )
        self.bytes_used = 0
        self.calls_used = 0
        self._exhausted = False
        self._lock = Lock()

    def check(self) -> str | None:
        """Return ``PULL_BUDGET_EXHAUSTED`` when an aggregate cap is already exceeded."""
        with self._lock:
            if self._exhausted or self.calls_used >= self.attempt_calls_limit:
                return PULL_BUDGET_EXHAUSTED
            if self.bytes_used >= self.attempt_bytes_limit:
                return PULL_BUDGET_EXHAUSTED
            return None

    def consume(self, nbytes: int) -> str | None:
        """Account one call of ``nbytes``; refuse when a cap is crossed."""
        if nbytes < 0:
            raise ValueError("nbytes must be non-negative")
        with self._lock:
            if self._exhausted or self.calls_used >= self.attempt_calls_limit:
                self._exhausted = True
                return PULL_BUDGET_EXHAUSTED
            self.calls_used += 1
            if self.bytes_used + nbytes > self.attempt_bytes_limit:
                self._exhausted = True
                return PULL_BUDGET_EXHAUSTED
            self.bytes_used += nbytes
            return None


def result_bytes_limit() -> int:
    """Per-result byte cap; results are paginated beyond it (D5)."""
    return _env_int("CHATGPT_PULL_RESULT_BYTES", RESULT_BYTES_DEFAULT)
