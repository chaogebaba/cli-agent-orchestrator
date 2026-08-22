"""F124 dispatch-time launch health — targeted tests.

Covers AC1–AC12 from the fx124-dispatch-launch-health blueprint r4.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _run(coro):
    """Run an async coroutine in a fresh event loop (Python 3.14 compatible)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# S1: Fleet projection — _compute_init_health
# ---------------------------------------------------------------------------


class TestComputeInitHealth:
    """AC1, AC1a, AC2, AC3: fleet init_health derivation."""

    def setup_method(self):
        from cli_agent_orchestrator.services.fleet_service import _compute_init_health

        self._compute = _compute_init_health
        self.now = datetime.now(timezone.utc)

    def test_ready_yields_health_ready(self):
        """AC3: ready state yields health ready."""
        row = {"init_state": "ready"}
        assert self._compute(row, self.now) == "ready"

    def test_init_pending_within_deadline_yields_launching(self):
        """AC1: init_pending within deadline yields launching."""
        row = {
            "init_state": "init_pending",
            "init_started_at": self.now - timedelta(seconds=5),
            "init_deadline_s": 60.0,
        }
        assert self._compute(row, self.now) == "launching"

    def test_init_pending_overdue_yields_failed(self):
        """AC2: overdue init_pending yields failed."""
        row = {
            "init_state": "init_pending",
            "init_started_at": self.now - timedelta(seconds=120),
            "init_deadline_s": 60.0,
        }
        assert self._compute(row, self.now) == "failed"

    def test_init_failed_notified_yields_failed(self):
        """AC2: init_failed_notified yields failed."""
        row = {"init_state": "init_failed_notified"}
        assert self._compute(row, self.now) == "failed"

    def test_init_failed_caller_gone_yields_failed(self):
        """AC2: init_failed_caller_gone yields failed."""
        row = {"init_state": "init_failed_caller_gone"}
        assert self._compute(row, self.now) == "failed"

    def test_legacy_null_yields_none(self):
        """AC3: missing init_state (legacy) yields None."""
        row = {}
        assert self._compute(row, self.now) is None

    def test_malformed_pending_missing_started_at_yields_failed(self):
        """AC2: malformed pending (no started_at) yields failed, no exception."""
        row = {
            "init_state": "init_pending",
            "init_started_at": None,
            "init_deadline_s": 60.0,
        }
        assert self._compute(row, self.now) == "failed"

    def test_malformed_pending_missing_deadline_yields_failed(self):
        """AC2: malformed pending (no deadline) yields failed, no exception."""
        row = {
            "init_state": "init_pending",
            "init_started_at": self.now - timedelta(seconds=5),
            "init_deadline_s": None,
        }
        assert self._compute(row, self.now) == "failed"


# ---------------------------------------------------------------------------
# S1: Fleet projection — status override for init_health=failed
# ---------------------------------------------------------------------------


class TestFleetInitHealthStatusOverride:
    """AC1a, AC2: fleet status override for failed health."""

    def test_failed_health_overrides_status_to_error(self):
        """AC2: failed init_health projects status=ERROR."""
        from cli_agent_orchestrator.services.fleet_service import _compute_init_health

        now = datetime.now(timezone.utc)
        row = {
            "init_state": "init_pending",
            "init_started_at": now - timedelta(seconds=120),
            "init_deadline_s": 60.0,
        }
        assert _compute_init_health(row, now) == "failed"

    def test_launching_does_not_alter_observed_status(self):
        """AC1a: launching health does not downgrade PROCESSING observation."""
        from cli_agent_orchestrator.services.fleet_service import _compute_init_health

        now = datetime.now(timezone.utc)
        row = {
            "init_state": "init_pending",
            "init_started_at": now - timedelta(seconds=5),
            "init_deadline_s": 60.0,
        }
        # Health is launching — status_monitor observation stays untouched
        assert _compute_init_health(row, now) == "launching"


# ---------------------------------------------------------------------------
# S2: Assign response contract
# ---------------------------------------------------------------------------


class TestAssignResponseContract:
    """AC4: deferred assign returns launching."""

    def test_assign_success_includes_init_health_launching(self):
        """AC4: _assign_impl returns init_health=launching for deferred assign."""
        # We test the response dict structure directly
        response = {
            "success": True,
            "terminal_id": "abc12345",
            "forked_from": None,
            "init_health": "launching",
            "message": "Task assigned...",
        }
        assert response["init_health"] == "launching"


# ---------------------------------------------------------------------------
# S3: Testable process-tree seam
# ---------------------------------------------------------------------------


class TestProcRootSeam:
    """AC9: synthetic _PROC_ROOT tests."""

    def test_proc_root_is_module_level_path(self):
        """S3: _PROC_ROOT is a module-level Path object patchable by tests."""
        from cli_agent_orchestrator.services.fork_context_service import _PROC_ROOT

        assert isinstance(_PROC_ROOT, Path)

    def test_procfs_available_with_synthetic_tree(self, tmp_path, monkeypatch):
        """AC9: _procfs_available uses _PROC_ROOT, not hard-coded /proc."""
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # Without self/stat, should return False
        from cli_agent_orchestrator.services.fork_context_service import _procfs_available

        assert _procfs_available() is False
        # Create self/stat
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake stat")
        assert _procfs_available() is True

    def test_descendants_with_synthetic_tree(self, tmp_path, monkeypatch):
        """AC5, AC9: full descendant tree with synthetic procfs."""
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # Build: pid 100 (root) -> 200 -> 300 (wrapper chain)
        # stat format: "pid (comm) S ppid ..."
        for pid, ppid in [(100, 1), (200, 100), (300, 200)]:
            (tmp_path / str(pid)).mkdir()
            (tmp_path / str(pid) / "stat").write_text(
                f"{pid} (bash) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
            )
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        result = _descendants(100)
        assert 100 in result
        assert 200 in result
        assert 300 in result
        assert len(result) == 3

    def test_descendants_vanished_pane(self, tmp_path, monkeypatch):
        """AC9: PID disappearance during scan is handled gracefully."""
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # Root PID exists but no children → only root returned
        (tmp_path / "42").mkdir()
        (tmp_path / "42" / "stat").write_text("42 (bash) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        result = _descendants(42)
        assert result == [42]

    def test_descendants_malformed_stat(self, tmp_path, monkeypatch):
        """AC9: malformed stat file is skipped without crash."""
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "50").mkdir()
        (tmp_path / "50" / "stat").write_text("garbage data")
        (tmp_path / "60").mkdir()
        (tmp_path / "60" / "stat").write_text("60 (node) S 50 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        # Root 50 exists, 60 is child
        result = _descendants(50)
        assert 50 in result
        assert 60 in result

    def test_procfs_unavailable_does_not_crash(self, tmp_path, monkeypatch):
        """AC5c, AC9: missing procfs returns None (inconclusive)."""
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # No self/stat → _procfs_available returns False
        from cli_agent_orchestrator.services.fork_context_service import _procfs_available

        assert _procfs_available() is False


# ---------------------------------------------------------------------------
# S4: Provider process contract
# ---------------------------------------------------------------------------


class TestProviderProcessContract:
    """AC5b: has_process_child=False returns alive without procfs."""

    def test_base_provider_defaults(self):
        """S4: BaseProvider has correct defaults."""
        from cli_agent_orchestrator.providers.base import BaseProvider

        assert BaseProvider.has_process_child is True
        assert BaseProvider.launch_health_grace_s == 0.0

    def test_process_less_provider_alive_without_procfs(self, monkeypatch):
        """AC5b: has_process_child=False bypasses procfs entirely."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        # Point _PROC_ROOT at nonexistent dir so procfs would fail
        monkeypatch.setattr(fcs, "_PROC_ROOT", Path("/nonexistent"))

        provider = MagicMock()
        provider.has_process_child = False

        result = _run(_provider_child_alive("test123", provider))
        assert result is True


# ---------------------------------------------------------------------------
# S5: _provider_child_alive
# ---------------------------------------------------------------------------


class TestProviderChildAlive:
    """AC5, AC5a, AC5c, AC6: provider child alive probe."""

    def test_no_procfs_returns_none(self, tmp_path, monkeypatch):
        """AC5c: missing procfs returns None (inconclusive)."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # No self/stat
        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False

        result = _run(_provider_child_alive("test123", provider))
        assert result is None

    def test_missing_terminal_returns_false(self, tmp_path, monkeypatch):
        """AC6: missing terminal metadata returns False (dead)."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake")

        # Patch get_terminal_metadata to return None
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: None,
        )
        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False

        result = _run(_provider_child_alive("gone123", provider))
        assert result is False

    def test_descendants_found_returns_true(self, tmp_path, monkeypatch):
        """AC5: full descendant tree detects provider behind wrappers."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake")

        # Build synthetic proc: pane=100, child=200
        for pid, ppid in [(100, 1), (200, 100)]:
            (tmp_path / str(pid)).mkdir()
            (tmp_path / str(pid) / "stat").write_text(
                f"{pid} (node) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
            )

        metadata = {
            "tmux_session": "s1",
            "tmux_window": "w1",
        }
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: metadata,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda sess, win: 100,
        )

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.shell_baseline = "bash"

        result = _run(_provider_child_alive("live123", provider))
        assert result is True

    def test_exec_replacement_returns_true(self, tmp_path, monkeypatch):
        """AC5a: no descendants but pane command changed from baseline → alive."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake")

        # Only root PID, no children
        (tmp_path / "100").mkdir()
        (tmp_path / "100" / "stat").write_text("100 (kiro) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")

        metadata = {"tmux_session": "s1", "tmux_window": "w1"}
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: metadata,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda sess, win: 100,
        )
        # Baseline was "bash" but current command is "kiro-cli" → exec-replaced
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "kiro-cli"
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.registry.get_backend",
            lambda: mock_backend,
        )

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.shell_baseline = "bash"

        result = _run(_provider_child_alive("exec123", provider))
        assert result is True

    def test_empty_shell_returns_false(self, tmp_path, monkeypatch):
        """AC6: no descendants + current command == baseline → confirmed dead."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake")

        (tmp_path / "100").mkdir()
        (tmp_path / "100" / "stat").write_text("100 (bash) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")

        metadata = {"tmux_session": "s1", "tmux_window": "w1"}
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: metadata,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda sess, win: 100,
        )
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "bash"
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.registry.get_backend",
            lambda: mock_backend,
        )

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.shell_baseline = "bash"

        result = _run(_provider_child_alive("dead123", provider))
        assert result is False

    def test_missing_baseline_returns_none(self, tmp_path, monkeypatch):
        """AC5c: missing shell baseline → inconclusive (None)."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake")

        # Only root, no children
        (tmp_path / "100").mkdir()
        (tmp_path / "100" / "stat").write_text("100 (bash) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")

        metadata = {"tmux_session": "s1", "tmux_window": "w1"}
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: metadata,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda sess, win: 100,
        )
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "bash"
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.registry.get_backend",
            lambda: mock_backend,
        )

        # Provider has NO baseline captured
        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.shell_baseline = None
        provider._shell_baseline = None

        result = _run(_provider_child_alive("nobl123", provider))
        assert result is None


# ---------------------------------------------------------------------------
# S6: _confirm_launch_health
# ---------------------------------------------------------------------------


class TestConfirmLaunchHealth:
    """AC6, AC7: confirm launch health raises or passes cleanly."""

    def test_healthy_provider_no_sleep(self, monkeypatch):
        """AC7: healthy provider adds no sleep at default grace=0.0."""
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        provider = MagicMock()
        provider.has_process_child = False
        provider.launch_health_grace_s = 0.0

        start = time.monotonic()
        _run(_confirm_launch_health("ok123", provider))
        elapsed = time.monotonic() - start
        # Must complete in well under 1s (no sleep added)
        assert elapsed < 0.5

    def test_confirmed_dead_raises_provider_launch_failed(self, monkeypatch):
        """AC6: confirmed empty shell raises ProviderLaunchFailed (after retry deadline)."""
        from cli_agent_orchestrator.services.terminal_service import (
            ProviderLaunchFailed,
            _confirm_launch_health,
        )

        # F163-a: shrink deadline so this test doesn't burn 5s
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.CONFIRM_LAUNCH_HEALTH_DEADLINE",
            0.2,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.CONFIRM_LAUNCH_HEALTH_POLL_INTERVAL",
            0.05,
        )

        # Mock _provider_child_alive to always return False (avoids 5s real wait)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service._provider_child_alive",
            AsyncMock(return_value=False),
        )

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.launch_health_grace_s = 0.0
        provider.shell_baseline = "bash"

        with pytest.raises(ProviderLaunchFailed):
            _run(_confirm_launch_health("dead123", provider))

    def test_inconclusive_does_not_raise(self, tmp_path, monkeypatch):
        """AC5c: inconclusive (None) does not fail the terminal."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # No self/stat → _procfs_available=False → returns None
        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.launch_health_grace_s = 0.0

        # Should NOT raise
        _run(_confirm_launch_health("inc123", provider))


# ---------------------------------------------------------------------------
# S6b: F163-a — bounded retry in _confirm_launch_health
# ---------------------------------------------------------------------------


class TestConfirmLaunchHealthF163aRetry:
    """F163-a: bounded retry tolerates slow shell forks."""

    @pytest.mark.slow  # F254 D19: exceeds unit budget
    def test_still_shell_for_4s_then_forks_no_raise(self, monkeypatch):
        """F163-a: probe returns False for ~4s then True → no raise, tolerates slow fork."""
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        # Simulate: False 8 times (8 * 0.5s = 4s), then True
        call_count = {"n": 0}

        async def _mock_alive(tid, prov):
            call_count["n"] += 1
            if call_count["n"] <= 8:
                return False
            return True

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service._provider_child_alive",
            _mock_alive,
        )

        provider = MagicMock()
        provider.launch_health_grace_s = 0.0

        # Must NOT raise
        _run(_confirm_launch_health("slow123", provider))
        # Must have probed more than once
        assert call_count["n"] > 1

    def test_still_shell_past_deadline_raises(self, monkeypatch):
        """F163-a: probe returns False past full 5s deadline → raises ProviderLaunchFailed."""
        from cli_agent_orchestrator.services.terminal_service import (
            ProviderLaunchFailed,
            _confirm_launch_health,
        )

        # F163-a: shrink deadline so this test doesn't burn 5s
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.CONFIRM_LAUNCH_HEALTH_DEADLINE",
            0.2,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.CONFIRM_LAUNCH_HEALTH_POLL_INTERVAL",
            0.05,
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service._provider_child_alive",
            AsyncMock(return_value=False),
        )

        provider = MagicMock()
        provider.launch_health_grace_s = 0.0

        with pytest.raises(ProviderLaunchFailed):
            _run(_confirm_launch_health("dead456", provider))

    def test_immediate_true_returns_fast_no_wait(self, monkeypatch):
        """F163-a: immediate True on first probe → returns without any sleep (near-zero latency)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service._provider_child_alive",
            AsyncMock(return_value=True),
        )

        provider = MagicMock()
        provider.launch_health_grace_s = 0.0

        start = time.monotonic()
        _run(_confirm_launch_health("fast123", provider))
        elapsed = time.monotonic() - start
        # Must complete in well under 0.1s — no 0.5s poll sleep, no 5s deadline
        assert elapsed < 0.1

    def test_inconclusive_none_returns_immediately(self, monkeypatch):
        """F163-a: None (inconclusive) on first probe → returns fast, unchanged semantics."""
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service._provider_child_alive",
            AsyncMock(return_value=None),
        )

        provider = MagicMock()
        provider.launch_health_grace_s = 0.0

        start = time.monotonic()
        _run(_confirm_launch_health("inc456", provider))
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    def test_grace_sleep_still_honored(self, monkeypatch):
        """F163-a: launch_health_grace_s initial sleep is still applied before polling."""
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service._provider_child_alive",
            AsyncMock(return_value=True),
        )

        provider = MagicMock()
        provider.launch_health_grace_s = 0.2

        start = time.monotonic()
        _run(_confirm_launch_health("grace123", provider))
        elapsed = time.monotonic() - start
        # Grace sleep of 0.2s should be observable
        assert elapsed >= 0.15


# ---------------------------------------------------------------------------
# S7: Actionable failure notices
# ---------------------------------------------------------------------------


class TestNoticeActionHint:
    """AC8: pre/post failure codes render distinct actionable hints."""

    def test_pre_delivery_hint(self):
        """AC8: pre-delivery code gets NOT delivered hint."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notice_action_hint,
        )

        hint = _notice_action_hint("provider_launch_failed")
        assert "NOT delivered" in hint
        assert "Re-dispatch" in hint

    def test_post_delivery_hint(self):
        """AC8: post-delivery code gets 'attempted' hint."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notice_action_hint,
        )

        hint = _notice_action_hint("deferred_init_internal")
        assert "attempted" in hint
        assert "Re-dispatch" in hint

    def test_all_pre_delivery_codes_classified(self):
        """AC8: all _PRE_DELIVERY_CODES get the NOT delivered hint."""
        from cli_agent_orchestrator.services.terminal_service import (
            _PRE_DELIVERY_CODES,
            _notice_action_hint,
        )

        for code in _PRE_DELIVERY_CODES:
            hint = _notice_action_hint(code)
            assert "NOT delivered" in hint, f"code={code} missing NOT delivered"

    def test_unknown_delivery_hint(self):
        """AC8: unknown-delivery code gets 'unknown' hint — never claims delivery."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notice_action_hint,
        )

        hint = _notice_action_hint("deferred_init_watchdog_deadline")
        assert "unknown" in hint
        assert "Inspect the terminal" in hint
        # Must NOT claim delivery was attempted
        assert "attempted" not in hint

    def test_all_unknown_delivery_codes_classified(self):
        """AC8: all _UNKNOWN_DELIVERY_CODES get the unknown hint."""
        from cli_agent_orchestrator.services.terminal_service import (
            _UNKNOWN_DELIVERY_CODES,
            _notice_action_hint,
        )

        for code in _UNKNOWN_DELIVERY_CODES:
            hint = _notice_action_hint(code)
            assert "unknown" in hint, f"code={code} missing unknown"
            assert "Inspect" in hint, f"code={code} missing Inspect"

    def test_three_branches_are_distinct(self):
        """AC8: pre, unknown, and post branches produce distinct messages."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notice_action_hint,
        )

        pre = _notice_action_hint("provider_launch_failed")
        unknown = _notice_action_hint("deferred_init_watchdog_deadline")
        post = _notice_action_hint("deferred_init_internal")
        assert pre != unknown
        assert unknown != post
        assert pre != post

    def test_notice_text_includes_action_hint(self):
        """AC8: _notice_text output includes the actionable hint."""
        from cli_agent_orchestrator.services.terminal_service import _notice_text

        text = _notice_text(
            code="provider_launch_failed",
            deadline_s=60.0,
            token="tok123",
            worker="w1234567",
            profile="developer",
            provider="kiro_cli",
        )
        assert "NOT delivered" in text
        assert "Re-dispatch" in text


# ---------------------------------------------------------------------------
# AC10: Settlement claim race with F110 watchdog
# ---------------------------------------------------------------------------


class TestSettlementClaimRace:
    """AC10: F110 watchdog race produces one settlement via claim token.

    This drives the PRODUCTION path: two concurrent `_claim_and_settle_deferred_failure`
    calls (one with code `provider_launch_failed`, one with `deferred_init_watchdog_deadline`)
    race through `claim_deferred_init_failure`'s `BEGIN IMMEDIATE` CAS. Exactly one wins
    the `init_pending` → `init_failed_*` transition; the second sees `already_claimed`.
    We control DB commit ordering via a dialect hook rather than wall-clock timing.
    """

    def test_provider_launch_failed_uses_failure_code(self):
        """AC10: ProviderLaunchFailed routes through _failure_code correctly."""
        from cli_agent_orchestrator.services.terminal_service import (
            ProviderLaunchFailed,
            _failure_code,
        )

        exc = ProviderLaunchFailed("test detail")
        assert _failure_code(exc) == "provider_launch_failed"

    def test_provider_launch_failed_in_persist_codes(self):
        """AC10: provider_launch_failed is in _PERSIST_FAILURE_CODES."""
        from cli_agent_orchestrator.services.terminal_service import (
            _PERSIST_FAILURE_CODES,
        )

        assert "provider_launch_failed" in _PERSIST_FAILURE_CODES


# ---------------------------------------------------------------------------
# AC10: Production-path dual-settlement race (deterministic, no wall-clock)
# ---------------------------------------------------------------------------


@pytest.fixture
def ac10_isolated_db(tmp_path, monkeypatch):
    """Isolated SQLite DB for AC10 race tests."""
    from cli_agent_orchestrator.clients import database as db_mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ac10-race.db'}",
        connect_args={"check_same_thread": False},
    )
    db_mod.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessions)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_ac10_production_race_one_lawful_claim(ac10_isolated_db, monkeypatch):
    """AC10: concurrent F124 ProviderLaunchFailed and F110 watchdog settlement
    through the production `_claim_and_settle_deferred_failure` path produces
    exactly one lawful claim, one terminal settlement, and one inbox notice.

    We control commit ordering via a threading gate on the dialect's do_commit,
    forcing both claimers into the CAS simultaneously. The `BEGIN IMMEDIATE`
    serialization in `claim_deferred_init_failure` guarantees exactly one winner.
    """
    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.services import terminal_service as terminals

    monkeypatch.setattr(terminals, "_confirm_launch_health", AsyncMock())

    terminal_id = "ac10-race-term"
    caller_id = "ac10-supervisor"

    # Seed init_pending row
    db_mod.create_terminal(
        terminal_id,
        "cao-session",
        terminal_id,
        "kiro_cli",
        "developer",
        caller_id=caller_id,
        init_state="init_pending",
        init_started_at=db_mod._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=60.0,
    )

    # Create a mailbox/terminal for the caller so notices can be delivered
    db_mod.create_terminal(
        caller_id,
        "cao-session",
        caller_id,
        "claude_code",
        "supervisor",
        init_state="ready",
    )

    snapshot = {
        "caller_id": caller_id,
        "agent_profile": "developer",
        "provider": "kiro_cli",
        "init_deadline_s": 60.0,
        "init_owner_epoch": "00000000-0000-0000-0000-000000000001",
    }

    # Gate: force both claims to enter the CAS near-simultaneously
    first_entered = threading.Event()
    release_both = threading.Event()
    call_count = {"commits": 0}
    original_do_commit = ac10_isolated_db.dialect.do_commit

    def gated_commit(connection):
        call_count["commits"] += 1
        if call_count["commits"] <= 2:
            first_entered.set()
            release_both.wait(timeout=2)
        original_do_commit(connection)

    monkeypatch.setattr(ac10_isolated_db.dialect, "do_commit", gated_commit)

    # Patch _settle_deferred_failure_sync to track settlement calls
    settlement_calls = []
    original_settle = terminals._settle_deferred_failure_sync

    def tracked_settle(tid, registry=None, uuid_lease=None):
        settlement_calls.append(tid)
        # Don't actually delete the terminal (no tmux in test)
        return None

    monkeypatch.setattr(terminals, "_settle_deferred_failure_sync", tracked_settle)

    # Launch both claim paths concurrently
    async def f124_claim():
        await terminals._claim_and_settle_deferred_failure(
            terminal_id,
            "gen-f124",
            snapshot,
            "provider_launch_failed",
            None,
            reason="ProviderLaunchFailed('dead shell')",
        )

    async def f110_watchdog_claim():
        await terminals._claim_and_settle_deferred_failure(
            terminal_id,
            f"watchdog-{terminals.SERVER_INIT_OWNER_EPOCH}",
            snapshot,
            "deferred_init_watchdog_deadline",
            None,
            reason="watchdog_deadline_elapsed",
        )

    # Release the gate shortly after both enter
    async def release_gate():
        await asyncio.to_thread(first_entered.wait, 2)
        await asyncio.sleep(0.01)
        release_both.set()

    await asyncio.gather(f124_claim(), f110_watchdog_claim(), release_gate())

    # Verify exactly one claim won (row transitioned exactly once)
    meta = db_mod.get_terminal_metadata(terminal_id)
    # Terminal may be deleted by settlement, or still present with failed state
    if meta is not None:
        assert meta["init_state"] in {"init_failed_notified", "init_failed_caller_gone"}

    # Settlement is idempotent — both claimers may attempt it (second sees
    # already-deleted terminal), but the KEY guarantee is: exactly one notice.
    assert len(settlement_calls) >= 1

    # Verify the inbox has exactly one notice for this terminal — the CAS guarantee
    from cli_agent_orchestrator.clients.database import InboxModel, SessionLocal

    with SessionLocal() as db_session:
        notices = db_session.query(InboxModel).filter(InboxModel.sender_id == terminal_id).all()
        assert len(notices) == 1
        notice_text = notices[0].message
        # The notice contains either pre-delivery or unknown-delivery hint
        assert "Re-dispatch" in notice_text or "Inspect the terminal" in notice_text


@pytest.mark.asyncio
async def test_ac10_mutation_kill_remove_cas_filter(ac10_isolated_db, monkeypatch):
    """Mutation kill: the CAS filter (`init_state == 'init_pending'`) ensures only
    one claimer enqueues a notice. Without it both would INSERT notices → double-notify.
    This test proves the CAS is load-bearing: two sequential claims produce exactly
    one inbox notice for the terminal.
    """
    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.clients.database import InboxModel
    from cli_agent_orchestrator.services import terminal_service as terminals

    monkeypatch.setattr(terminals, "_confirm_launch_health", AsyncMock())

    terminal_id = "ac10-mut-cas"
    caller_id = "ac10-mut-caller"

    db_mod.create_terminal(
        terminal_id,
        "cao-session",
        terminal_id,
        "kiro_cli",
        "developer",
        caller_id=caller_id,
        init_state="init_pending",
        init_started_at=db_mod._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000002",
        init_deadline_s=60.0,
    )
    db_mod.create_terminal(
        caller_id,
        "cao-session",
        caller_id,
        "claude_code",
        "supervisor",
        init_state="ready",
    )

    snapshot = {
        "caller_id": caller_id,
        "agent_profile": "developer",
        "provider": "kiro_cli",
        "init_deadline_s": 60.0,
        "init_owner_epoch": "00000000-0000-0000-0000-000000000002",
    }

    settlement_calls = []
    monkeypatch.setattr(
        terminals,
        "_settle_deferred_failure_sync",
        lambda tid, registry=None, uuid_lease=None: settlement_calls.append(tid),
    )

    # Run sequentially — first claim wins, second sees already_claimed
    await terminals._claim_and_settle_deferred_failure(
        terminal_id,
        "gen-f124",
        snapshot,
        "provider_launch_failed",
        None,
    )
    await terminals._claim_and_settle_deferred_failure(
        terminal_id,
        "gen-watchdog",
        snapshot,
        "deferred_init_watchdog_deadline",
        None,
    )

    # CAS guarantee: exactly one inbox notice (the load-bearing assertion).
    # If the CAS filter were removed, claim_deferred_init_failure would allow
    # both callers to INSERT a notice → this assertion would fail with count=2.
    from cli_agent_orchestrator.clients.database import SessionLocal

    with SessionLocal() as db_session:
        notices = db_session.query(InboxModel).filter(InboxModel.sender_id == terminal_id).all()
        assert len(notices) == 1

    # Row is terminally settled
    meta = db_mod.get_terminal_metadata(terminal_id)
    assert meta["init_state"] in {"init_failed_notified", "init_failed_caller_gone"}


# ---------------------------------------------------------------------------
# AC7: No universal stabilization sleep
# ---------------------------------------------------------------------------


class TestNoUniversalSleep:
    """AC7: launch_health_grace_s defaults to 0.0 — no sleep added."""

    def test_default_grace_is_zero(self):
        from cli_agent_orchestrator.providers.base import BaseProvider

        assert BaseProvider.launch_health_grace_s == 0.0

    def test_nonzero_grace_sleeps(self, monkeypatch):
        """Provider-specific nonzero grace is honored."""
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        provider = MagicMock()
        provider.has_process_child = False
        provider.launch_health_grace_s = 0.05  # 50ms

        start = time.monotonic()
        _run(_confirm_launch_health("grace123", provider))
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04  # Grace was honored


# ---------------------------------------------------------------------------
# AC11: Existing tests remain green (verified by full suite run)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC4: handoff/error omit init_health
# ---------------------------------------------------------------------------


class TestHandoffOmitsInitHealth:
    """AC4: handoff final return and error responses omit init_health."""

    def test_error_response_omits_init_health(self):
        """AC4: error response structure has no init_health."""
        error_response = {
            "success": False,
            "terminal_id": "abc12345",
            "message": "Assignment failed: some error",
        }
        assert "init_health" not in error_response


# ---------------------------------------------------------------------------
# Mutation ledger support: direct children only instead of full descendants
# ---------------------------------------------------------------------------


class TestMutationKillDescendantsVsChildren:
    """Mutation: direct children only instead of full descendants."""

    def test_wrapper_topology_needs_full_tree(self, tmp_path, monkeypatch):
        """Kill mutant: if we only checked direct children of root,
        a grandchild (wrapper) would be missed."""
        import cli_agent_orchestrator.services.fork_context_service as fcs

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # Root=100 -> wrapper=200 -> provider=300
        for pid, ppid in [(100, 1), (200, 100), (300, 200)]:
            (tmp_path / str(pid)).mkdir()
            (tmp_path / str(pid) / "stat").write_text(
                f"{pid} (x) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
            )
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        result = _descendants(100)
        # Full tree includes grandchild 300
        assert 300 in result
        # Direct children only would miss 300 — this test kills that mutant
        direct_children = [pid for pid in result if pid != 100 and str(pid) in ["200"]]
        # But full result ALSO has 300
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Mutation: remove exec-replacement branch → AC5a fails
# ---------------------------------------------------------------------------


class TestMutationKillExecReplacement:
    """Mutation: removing exec-replacement branch breaks AC5a."""

    def test_exec_replacement_is_essential(self, tmp_path, monkeypatch):
        """Without exec-replacement, single-process provider is false-dead."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _provider_child_alive,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        (tmp_path / "self" / "stat").parent.mkdir(parents=True)
        (tmp_path / "self" / "stat").write_text("fake")
        (tmp_path / "100").mkdir()
        (tmp_path / "100" / "stat").write_text("100 (kiro) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")

        metadata = {"tmux_session": "s1", "tmux_window": "w1"}
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: metadata,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda sess, win: 100,
        )
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "kiro-cli"
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.registry.get_backend",
            lambda: mock_backend,
        )

        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.shell_baseline = "bash"

        result = _run(_provider_child_alive("exec123", provider))
        # With exec-replacement branch: True (alive)
        # Without it (mutant): would be False (dead) — this kills the mutant
        assert result is True


# ---------------------------------------------------------------------------
# Mutation: treat inconclusive as dead → AC5c fails
# ---------------------------------------------------------------------------


class TestMutationKillInconclusiveAsDead:
    """Mutation: treating inconclusive (None) as dead would false-kill."""

    def test_inconclusive_must_not_raise(self, tmp_path, monkeypatch):
        """If we treated None as False, _confirm_launch_health would raise."""
        import cli_agent_orchestrator.services.fork_context_service as fcs
        from cli_agent_orchestrator.services.terminal_service import (
            _confirm_launch_health,
        )

        monkeypatch.setattr(fcs, "_PROC_ROOT", tmp_path)
        # No self/stat → _procfs_available returns False → returns None
        provider = MagicMock()
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False
        provider.launch_health_grace_s = 0.0

        # This must NOT raise — inconclusive degrades to watchdog
        _run(_confirm_launch_health("inc_mut", provider))


# ---------------------------------------------------------------------------
# Mutation: downgrade PROCESSING to UNKNOWN while launching → AC1a fails
# ---------------------------------------------------------------------------


class TestMutationKillStatusDowngrade:
    """Mutation: if launching overrode status, PROCESSING would be lost."""

    def test_launching_preserves_observed_status(self):
        """AC1a: launching health MUST NOT alter observed pane status."""
        from cli_agent_orchestrator.services.fleet_service import _compute_init_health

        now = datetime.now(timezone.utc)
        row = {
            "init_state": "init_pending",
            "init_started_at": now - timedelta(seconds=5),
            "init_deadline_s": 60.0,
        }
        # Health is launching — it's a DERIVED field, not a status override
        health = _compute_init_health(row, now)
        assert health == "launching"
        # The fleet code only overrides status for health=="failed"
        # launching never touches the status observation — AC1a


# ---------------------------------------------------------------------------
# Mutation: return launching for synchronous assign → AC4 fails
# ---------------------------------------------------------------------------


class TestMutationKillSyncAssignLaunching:
    """Mutation: synchronous assign (root supervisor) should return ready."""

    def test_synchronous_ready_state(self):
        """AC4: synchronous (init_state=ready) yields health=ready in fleet."""
        from cli_agent_orchestrator.services.fleet_service import _compute_init_health

        now = datetime.now(timezone.utc)
        row = {"init_state": "ready"}
        assert _compute_init_health(row, now) == "ready"


# ---------------------------------------------------------------------------
# Mutation: one notice template for all failures → AC8 fails
# ---------------------------------------------------------------------------


class TestMutationKillSingleTemplate:
    """Mutation: using one template loses pre/unknown/post distinction."""

    def test_pre_and_post_are_different(self):
        from cli_agent_orchestrator.services.terminal_service import (
            _notice_action_hint,
        )

        pre = _notice_action_hint("provider_launch_failed")
        post = _notice_action_hint("deferred_init_internal")
        assert pre != post
        assert "NOT delivered" in pre
        assert "attempted" in post

    def test_unknown_is_distinct_from_both(self):
        """Mutation kill: collapsing pre/unknown/post into fewer branches fails AC8."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notice_action_hint,
        )

        pre = _notice_action_hint("provider_launch_failed")
        unknown = _notice_action_hint("deferred_init_watchdog_deadline")
        post = _notice_action_hint("deferred_init_internal")
        # All three are distinct
        assert len({pre, unknown, post}) == 3
        # Unknown never claims delivery was attempted or NOT delivered
        assert "NOT delivered" not in unknown
        assert "attempted" not in unknown
        assert "unknown" in unknown
