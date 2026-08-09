"""RSS spike regression guard.

Fails any single test whose VmRSS delta exceeds a configurable threshold.
Prevents busy-spin / allocation-bomb tests from silently landing.

Threshold: CAO_TEST_RSS_DELTA_MB env var (default 200).
Skips cleanly on non-Linux (no /proc/self/status).

O(1) per test — reads /proc/self/status only.
"""

import os
import sys

import pytest

_LINUX = sys.platform == "linux"
_PROC_STATUS = "/proc/self/status"
_THRESHOLD_MB = int(os.environ.get("CAO_TEST_RSS_DELTA_MB", "200"))


def _read_vmrss_kb() -> int | None:
    """Read VmRSS from /proc/self/status. Returns kB or None on failure."""
    try:
        with open(_PROC_STATUS) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


class RSSGuardPlugin:
    """Pytest plugin that tracks per-test RSS delta."""

    def __init__(self, threshold_mb: int):
        self.threshold_kb = threshold_mb * 1024
        self._pre_rss_kb: int | None = None

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self._pre_rss_kb = _read_vmrss_kb()

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        if self._pre_rss_kb is None:
            return
        post_rss_kb = _read_vmrss_kb()
        if post_rss_kb is None:
            return
        delta_kb = post_rss_kb - self._pre_rss_kb
        if delta_kb > self.threshold_kb:
            delta_mb = delta_kb / 1024
            raise pytest.fail.Exception(
                f"RSS spike guard: test '{item.nodeid}' increased VmRSS by "
                f"{delta_mb:.0f} MB (threshold: {_THRESHOLD_MB} MB). "
                f"Pre={self._pre_rss_kb // 1024} MB, "
                f"Post={post_rss_kb // 1024} MB.",
                pytrace=False,
            )


def pytest_configure(config: pytest.Config) -> None:
    if _LINUX:
        config.pluginmanager.register(RSSGuardPlugin(_THRESHOLD_MB), "rss_guard")
