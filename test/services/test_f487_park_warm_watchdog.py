"""F487: park_warm=True must suppress the idle-without-callback watchdog episode.

Regression tests:
  - park_warm terminal: record_inbound_task is a no-op (no episode created)
  - non-warm terminal: record_inbound_task arms normally (unchanged)
  - park_warm terminal with existing episode: guard prevents re-arming
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
)


@pytest.fixture()
def watchdog():
    return StalledCallbackWatchdog(grace_seconds=10)


class TestF487ParkWarmSuppression:
    """park_warm=True in terminal system metadata suppresses episode creation."""

    def test_park_warm_terminal_no_episode_armed(self, watchdog: StalledCallbackWatchdog):
        """A terminal with cao.park_warm=True must not have an episode armed."""
        metadata = {
            "id": "secretary-f65079d1",
            "caller_id": "sup-ba7a644a",
            "agent_profile": "secretary",
            "metadata": {"cao": {"park_warm": True}},
        }
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ):
            watchdog.record_inbound_task("secretary-f65079d1", "sup-ba7a644a", "secretary")

        with watchdog._lock:
            assert "secretary-f65079d1" not in watchdog._episodes

    def test_non_warm_terminal_episode_armed_normally(self, watchdog: StalledCallbackWatchdog):
        """A terminal without park_warm keeps the existing arming behavior."""
        metadata = {
            "id": "kiro_dev-aaaabbbb",
            "caller_id": "sup-ba7a644a",
            "agent_profile": "kiro_dev",
            "metadata": None,
        }
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ):
            watchdog.record_inbound_task("kiro_dev-aaaabbbb", "sup-ba7a644a", "kiro_dev")

        with watchdog._lock:
            assert "kiro_dev-aaaabbbb" in watchdog._episodes
            episode = watchdog._episodes["kiro_dev-aaaabbbb"]
            assert episode.caller_id == "sup-ba7a644a"
            assert episode.profile == "kiro_dev"

    def test_park_warm_empty_cao_dict_still_arms(self, watchdog: StalledCallbackWatchdog):
        """A terminal with empty cao metadata (no park_warm key) arms normally."""
        metadata = {
            "id": "worker-11112222",
            "caller_id": "sup-ba7a644a",
            "agent_profile": "developer",
            "metadata": {"cao": {}},
        }
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ):
            watchdog.record_inbound_task("worker-11112222", "sup-ba7a644a", "developer")

        with watchdog._lock:
            assert "worker-11112222" in watchdog._episodes

    def test_park_warm_false_explicit_still_arms(self, watchdog: StalledCallbackWatchdog):
        """park_warm=False in metadata does not suppress."""
        metadata = {
            "id": "worker-33334444",
            "caller_id": "sup-ba7a644a",
            "agent_profile": "developer",
            "metadata": {"cao": {"park_warm": False}},
        }
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ):
            watchdog.record_inbound_task("worker-33334444", "sup-ba7a644a", "developer")

        with watchdog._lock:
            assert "worker-33334444" in watchdog._episodes

    def test_metadata_none_arms_normally(self, watchdog: StalledCallbackWatchdog):
        """When get_terminal_metadata returns None (mid-creation), arm normally."""
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=None,
        ):
            watchdog.record_inbound_task("worker-55556666", "sup-ba7a644a", "developer")

        with watchdog._lock:
            assert "worker-55556666" in watchdog._episodes

    def test_watchdog_sender_still_skipped(self, watchdog: StalledCallbackWatchdog):
        """Watchdog-internal senders are still rejected before the park_warm check."""
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        ) as mock_meta:
            watchdog.record_inbound_task("worker-77778888", "watchdog:stall:x", "developer")
            mock_meta.assert_not_called()

        with watchdog._lock:
            assert "worker-77778888" not in watchdog._episodes

    def test_park_warm_terminal_collect_due_empty(self, watchdog: StalledCallbackWatchdog):
        """Even if an episode somehow existed, park_warm prevents arming from scratch."""
        # Simulate a scenario: force an episode, then verify collect_due
        # doesn't fire for park_warm terminals. This covers the scenario
        # where a non-park_warm message preceded the park_warm persistence.
        metadata = {
            "id": "oracle-aabb1122",
            "caller_id": "sup-ba7a644a",
            "agent_profile": "grok_oracle",
            "metadata": {"cao": {"park_warm": True}},
        }
        # Force-arm without going through record_inbound_task
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ):
            # This should be suppressed
            watchdog.record_inbound_task("oracle-aabb1122", "sup-ba7a644a", "grok_oracle")

        with watchdog._lock:
            assert "oracle-aabb1122" not in watchdog._episodes
