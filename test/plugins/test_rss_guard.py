"""Self-tests for the RSS spike regression guard.

Proves:
1. A test that allocates >threshold trips the guard.
2. Normal tests pass untouched.

Marked 'slow' so the spike test is excluded from default runs.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="RSS guard is Linux-only")


@pytest.mark.slow
def test_rss_guard_trips_on_spike():
    """Allocate >200MB (the default threshold) and verify the guard errors.

    This test is expected to FAIL due to the RSS guard.  We run it in a
    subprocess via pytest's pytester/inline infrastructure to prove the guard
    fires without contaminating the main suite.
    """
    # We use pytest.main() in-process to run a tiny inline test that spikes.
    # Can't use pytester here (requires the pytester plugin), so we verify
    # via direct plugin invocation.
    from test.plugins.rss_guard import RSSGuardPlugin, _read_vmrss_kb

    if _read_vmrss_kb() is None:
        pytest.skip("Cannot read /proc/self/status")

    plugin = RSSGuardPlugin(threshold_mb=1)  # 1MB threshold — trivial to trip

    class FakeItem:
        nodeid = "fake::test_spike"

    item = FakeItem()
    plugin.pytest_runtest_setup(item)

    # Allocate ~5MB to exceed the 1MB threshold
    _big = bytearray(5 * 1024 * 1024)

    with pytest.raises(pytest.fail.Exception, match="RSS spike guard"):
        plugin.pytest_runtest_teardown(item)

    del _big


def test_rss_guard_passes_normal_test():
    """Normal tests (trivial RSS delta) pass without guard interference."""
    from test.plugins.rss_guard import RSSGuardPlugin, _read_vmrss_kb

    if _read_vmrss_kb() is None:
        pytest.skip("Cannot read /proc/self/status")

    plugin = RSSGuardPlugin(threshold_mb=200)

    class FakeItem:
        nodeid = "fake::test_normal"

    item = FakeItem()
    plugin.pytest_runtest_setup(item)

    # Trivial work — no spike
    _ = sum(range(1000))

    # Should NOT raise
    plugin.pytest_runtest_teardown(item)
