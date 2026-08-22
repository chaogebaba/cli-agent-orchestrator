"""Deterministic monotonic clock for tests that poll time (fx147, D3).

Usage::

    clock = FakeClock()
    with clock.patch_time("time.monotonic", "time.sleep"):
        # time.monotonic() returns clock._now
        # time.sleep(n) advances clock._now by n
        clock.advance(5.0)  # jump forward explicitly
"""

import time
from contextlib import ExitStack, contextmanager
from unittest.mock import patch


class FakeClock:
    """Deterministic monotonic clock for tests that poll time."""

    def __init__(self, start: float = 1000.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds

    @contextmanager
    def patch_time(self, *targets: str):
        """Patch the given module-level names.

        Both migration targets (fifo_reader.py source and
        test_handoff_approval.py, verified 2026-08-12) use ``import time`` and
        call ``time.monotonic()`` by attribute, so patching ``"time.monotonic"``
        works for them. Explicit targets are kept as future-proofing: any module
        doing ``from time import monotonic`` binds the name at import and MUST be
        patched at its own module attribute instead.
        """
        patchers = [
            patch(
                t,
                side_effect=(self.sleep if t.endswith(".sleep") else self.monotonic),
            )
            for t in targets
        ]
        with ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            yield self
