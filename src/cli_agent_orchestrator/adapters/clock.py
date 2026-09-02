"""The system clock adapter (WP-ARCH phase 1).

Trivial, and still worth existing: every timestamp in the new tree comes through
a ``core.ports.Clock``, so a test can hold time still without patching
``datetime`` globally, and the fork's deterministic-simulation clock can be
wired in later without touching a single call site.

``datetime.now(UTC)`` — always aware, never the deprecated naive helper,
matching the fork-wide convention that ``test/test_datetime_convention.py``
enforces. That test greps for the deprecated call as a plain string, so writing
its name here (even inside prose) would trip it.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["SystemClock"]


class SystemClock:
    """``core.ports.Clock`` backed by the wall clock, in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)
