"""Guard: real_sqlite_env hands the test a clean process, not just a clean DB.

The f165 pull-mode flake came from teammate_push_service._last_notified
surviving between tests in the same xdist worker: two tests reuse terminal id
"sup00001", so the earlier one's high-water suppressed the later one's push
with reason="already_notified" while its DB was empty. These assertions fail
if the cache-clearing in the fixture is reverted.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import (
    delivery_service,
    doorbell_service,
    inbox_service,
    teammate_push_service,
)


@pytest.fixture()
def _poisoned_shadow_caches():
    """Simulate an earlier test in the same worker leaving state behind."""
    teammate_push_service._last_notified["sup00001"] = 999
    doorbell_service._last_doorbell_row_id["sup00001"] = 999
    doorbell_service._last_warn_time["sup00001"] = 1.0
    inbox_service._failure_streaks["sup00001"] = 3
    delivery_service._health_warning_dedup[("sup00001", 1, "x")] = None


def test_real_sqlite_env_clears_shadow_caches(_poisoned_shadow_caches, real_sqlite_env):
    assert teammate_push_service._last_notified == {}
    assert doorbell_service._last_doorbell_row_id == {}
    assert doorbell_service._last_warn_time == {}
    assert inbox_service._failure_streaks == {}
    assert delivery_service._health_warning_dedup == {}
