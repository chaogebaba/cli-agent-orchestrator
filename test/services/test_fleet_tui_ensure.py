"""F489: Tests for fleet TUI auto-start on terminal dispatch.

Covers:
- _maybe_ensure_fleet_tui fires Popen with correct args when flag enabled
- _maybe_ensure_fleet_tui is gated by tui.autostart=False
- _maybe_ensure_fleet_tui handles missing script gracefully (no exception)
- _maybe_ensure_fleet_tui fires at most once per process (per-module reset)
- settings_service: is_tui_autostart_enabled + get_tui_ensure_script
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest


class TestMaybeEnsureFleetTui:
    """Unit tests for the fire-and-forget TUI trigger in terminal_service."""

    def _reset_ensure_state(self, monkeypatch):
        """Reset the module-level once-per-process flag between tests."""
        from cli_agent_orchestrator.services import terminal_service

        monkeypatch.setattr(terminal_service, "_fleet_tui_ensure_attempted", False)

    def test_fires_popen_when_enabled_and_script_exists(self, monkeypatch, tmp_path):
        """When autostart=True and script exists, Popen is called with absolute path."""
        from cli_agent_orchestrator.services import terminal_service

        self._reset_ensure_state(monkeypatch)

        script = tmp_path / "fleet-tui-ensure.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_tui_autostart_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_tui_ensure_script",
            lambda: str(script),
        )

        terminal_service._maybe_ensure_fleet_tui("cao-test-session")

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == [str(script), "cao-test-session"]
        assert call_args[1]["stdin"] == subprocess.DEVNULL
        assert call_args[1]["stdout"] == subprocess.DEVNULL
        assert call_args[1]["stderr"] == subprocess.DEVNULL
        assert call_args[1]["start_new_session"] is True

    def test_skipped_when_autostart_disabled(self, monkeypatch, tmp_path):
        """When autostart=False, Popen is never called."""
        from cli_agent_orchestrator.services import terminal_service

        self._reset_ensure_state(monkeypatch)

        script = tmp_path / "fleet-tui-ensure.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_tui_autostart_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_tui_ensure_script",
            lambda: str(script),
        )

        terminal_service._maybe_ensure_fleet_tui("cao-test-session")

        mock_popen.assert_not_called()

    def test_no_exception_when_script_missing(self, monkeypatch):
        """When the ensure script path doesn't exist, function returns quietly."""
        from cli_agent_orchestrator.services import terminal_service

        self._reset_ensure_state(monkeypatch)

        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_tui_autostart_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_tui_ensure_script",
            lambda: "/nonexistent/path/fleet-tui-ensure.sh",
        )

        # Must not raise
        terminal_service._maybe_ensure_fleet_tui("cao-test-session")

        mock_popen.assert_not_called()

    def test_fires_at_most_once_per_process(self, monkeypatch, tmp_path):
        """Second call is a no-op (the once-per-process flag gates it)."""
        from cli_agent_orchestrator.services import terminal_service

        self._reset_ensure_state(monkeypatch)

        script = tmp_path / "fleet-tui-ensure.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_tui_autostart_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_tui_ensure_script",
            lambda: str(script),
        )

        terminal_service._maybe_ensure_fleet_tui("session-1")
        terminal_service._maybe_ensure_fleet_tui("session-2")

        # Only one call — the second was short-circuited
        assert mock_popen.call_count == 1

    def test_popen_exception_does_not_propagate(self, monkeypatch, tmp_path):
        """If Popen raises (e.g. permission denied), it's caught and logged."""
        from cli_agent_orchestrator.services import terminal_service

        self._reset_ensure_state(monkeypatch)

        script = tmp_path / "fleet-tui-ensure.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            subprocess, "Popen", MagicMock(side_effect=PermissionError("denied"))
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_tui_autostart_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_tui_ensure_script",
            lambda: str(script),
        )

        # Must not raise
        terminal_service._maybe_ensure_fleet_tui("cao-test-session")

    def test_session_name_none_omits_arg(self, monkeypatch, tmp_path):
        """When session_name is None, script is called without session arg."""
        from cli_agent_orchestrator.services import terminal_service

        self._reset_ensure_state(monkeypatch)

        script = tmp_path / "fleet-tui-ensure.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_tui_autostart_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_tui_ensure_script",
            lambda: str(script),
        )

        terminal_service._maybe_ensure_fleet_tui(None)

        call_args = mock_popen.call_args[0][0]
        assert call_args == [str(script)]


class TestTuiSettingsService:
    """Tests for the TUI settings in settings_service."""

    def test_is_tui_autostart_enabled_default_true(self, monkeypatch, tmp_path):
        """Default is True when no settings file exists."""
        from cli_agent_orchestrator.services import settings_service

        monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
        assert settings_service.is_tui_autostart_enabled() is True

    def test_is_tui_autostart_enabled_false_from_file(self, monkeypatch, tmp_path):
        """tui.autostart=false in settings.json → returns False."""
        import json

        from cli_agent_orchestrator.services import settings_service

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"tui": {"autostart": False}}))
        monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
        assert settings_service.is_tui_autostart_enabled() is False

    def test_is_tui_autostart_env_override(self, monkeypatch, tmp_path):
        """CAO_TUI_AUTOSTART=0 overrides settings.json."""
        import json

        from cli_agent_orchestrator.services import settings_service

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"tui": {"autostart": True}}))
        monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
        monkeypatch.setenv("CAO_TUI_AUTOSTART", "0")
        assert settings_service.is_tui_autostart_enabled() is False

    def test_get_tui_ensure_script_default(self, monkeypatch, tmp_path):
        """Default ensure script path is the known root-repo path."""
        from cli_agent_orchestrator.services import settings_service

        monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
        result = settings_service.get_tui_ensure_script()
        assert result.endswith("fleet-tui-ensure.sh")
        assert os.path.isabs(result)

    def test_get_tui_ensure_script_from_settings(self, monkeypatch, tmp_path):
        """Custom ensure script path from settings.json."""
        import json

        from cli_agent_orchestrator.services import settings_service

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({"tui": {"ensure_script": "/custom/path/ensure.sh"}})
        )
        monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
        assert settings_service.get_tui_ensure_script() == "/custom/path/ensure.sh"

    def test_get_tui_ensure_script_env_override(self, monkeypatch, tmp_path):
        """CAO_TUI_ENSURE_SCRIPT env var overrides settings.json."""
        from cli_agent_orchestrator.services import settings_service

        monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setenv("CAO_TUI_ENSURE_SCRIPT", "/env/override/ensure.sh")
        assert settings_service.get_tui_ensure_script() == "/env/override/ensure.sh"
