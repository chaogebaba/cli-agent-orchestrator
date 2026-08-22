"""Per-tier time budget enforcement plugin (F254 D19).

Structurally a copy of ``test/plugins/rss_guard.py``:
``pytest_runtest_setup`` records the start time, ``pytest_runtest_teardown``
compares, ``pytest.fail(..., pytrace=False)`` on breach, env-tunable, O(1).

Budget table (call phase wall-clock):

    unit:        1.0 s
    contract:   10.0 s
    sim:         5.0 s
    integration: 10.0 s
    slow/live/e2e/pty: no budget (None)

Mode is ``CAO_TEST_TIER_BUDGET in {off, warn, enforce}``, default **warn**.
``enforce`` is set by ``make test-hygiene`` (which runs ``-n 0``) and by CI.

On breach the failure message contains the exact line to paste::

    tier budget: test/providers/test_codex_provider_unit.py::...::test_initialize_success
    took 22.11 s (unit budget 1.0 s). Either fix the wait, or declare the tier:
        @pytest.mark.slow

Precedent: P-RESGUARD — test/plugins/rss_guard.py.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass

# Budget per tier (seconds). None = no budget enforced.
_TIER_BUDGETS: dict[str, float | None] = {
    "unit": 1.0,
    "contract": 10.0,
    "sim": 5.0,
    "integration": 10.0,
    "slow": None,
    "live": None,
    "e2e": None,
    "pty": None,
}


def _get_item_tier(item: pytest.Item) -> str | None:
    """Return the tier mark assigned by tier_marks.py, or None."""
    for mark in item.iter_markers():
        if mark.name in _TIER_BUDGETS:
            return mark.name
    return None


class TierBudgetPlugin:
    """Pytest plugin that enforces per-tier wall-clock budgets."""

    def __init__(self, mode: str):
        self.mode = mode  # "warn" or "enforce"
        self._start_time: float | None = None

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        """Record start time before the test call phase."""
        self._start_time = time.perf_counter()

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        """Check elapsed time against the tier's budget."""
        if self._start_time is None:
            return

        elapsed = time.perf_counter() - self._start_time
        self._start_time = None

        tier = _get_item_tier(item)
        if tier is None:
            return

        budget = _TIER_BUDGETS.get(tier)
        if budget is None:
            return

        if elapsed <= budget:
            return

        msg = (
            f"tier budget: {item.nodeid}\n"
            f"took {elapsed:.2f} s ({tier} budget {budget} s). "
            f"Either fix the wait, or declare the tier:\n"
            f"    @pytest.mark.slow"
        )

        if self.mode == "enforce":
            raise pytest.fail.Exception(msg, pytrace=False)
        else:
            # warn mode: print but do not fail
            import warnings

            warnings.warn(f"[tier-budget WARN] {msg}", stacklevel=1)


def pytest_configure(config: pytest.Config) -> None:
    """Register the plugin if mode is not 'off'."""
    mode = os.environ.get("CAO_TEST_TIER_BUDGET", "warn").lower()
    if mode in ("warn", "enforce"):
        config.pluginmanager.register(TierBudgetPlugin(mode), "tier_budget")
