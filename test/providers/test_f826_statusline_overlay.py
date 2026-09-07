"""F826 (#683) D3 / SHOULD-1 — the Claude statusLine overlay + toggle.

The fork writes a top-level `statusLine` key beside `hooks` (command =
`python -m ...hooks.status_emit`, refreshInterval 1500) UNLESS
`observe.claude_statusline=false` in CAO_HOME_DIR/settings.json. The toggle is
read at overlay-generation time (S1), not at emitter runtime.
"""

from __future__ import annotations

import json

import pytest

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


def _settings(provider):
    path = provider._write_terminal_settings()
    return json.loads(path.read_text(encoding="utf-8"))


def test_statusline_overlay_present_by_default(monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.claude_statusline_enabled", lambda: True
    )
    provider = ClaudeCodeProvider("slterm", "session", "window", None)
    settings = _settings(provider)
    assert "statusLine" in settings
    sl = settings["statusLine"]
    assert sl["type"] == "command"
    assert "cli_agent_orchestrator.hooks.status_emit" in sl["command"]
    assert sl["refreshInterval"] == 1500
    # The hooks block is unchanged (statusLine is additive, beside hooks).
    assert "hooks" in settings


def test_statusline_overlay_absent_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.claude_statusline_enabled", lambda: False
    )
    provider = ClaudeCodeProvider("slterm2", "session", "window", None)
    settings = _settings(provider)
    assert "statusLine" not in settings
    # The rest of the overlay is intact.
    assert "hooks" in settings


def test_toggle_reads_cao_settings_default_on(monkeypatch, tmp_path):
    """claude_statusline_enabled() is default-on and reads observe.claude_statusline."""
    from cli_agent_orchestrator.services import settings_service

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
    # No file -> default on.
    assert settings_service.claude_statusline_enabled() is True
    # observe.claude_statusline=false -> off.
    settings_file.write_text(json.dumps({"observe": {"claude_statusline": False}}))
    assert settings_service.claude_statusline_enabled() is False
    # Any other value -> on.
    settings_file.write_text(json.dumps({"observe": {"claude_statusline": True}}))
    assert settings_service.claude_statusline_enabled() is True
    settings_file.write_text(json.dumps({"observe": "malformed"}))
    assert settings_service.claude_statusline_enabled() is True
