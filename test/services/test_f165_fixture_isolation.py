"""Guard: real_sqlite_env hands the test a clean process, not just a clean DB.

surviving between tests in the same xdist worker: two tests reuse terminal id
"sup00001", so the earlier one's high-water suppressed the later one's push
with reason="already_notified" while its DB was empty. These assertions fail
if the cache-clearing in the fixture is reverted.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import inbox_service


@pytest.fixture()
def _poisoned_shadow_caches():
    """Simulate an earlier test in the same worker leaving state behind."""
    inbox_service._failure_streaks["sup00001"] = 3


def test_real_sqlite_env_clears_shadow_caches(_poisoned_shadow_caches, real_sqlite_env):
    assert inbox_service._failure_streaks == {}
