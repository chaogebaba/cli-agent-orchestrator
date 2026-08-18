"""F297: GROK_CLAUDE_HOOKS_ENABLED=0 must be present in every grok spawn path.

Issue #151 / #140: grok-build v1.0.5 wedges after the first tool round when it
loads Claude project hooks from the launch cwd. The env var is grok's native
import toggle — when set to 0, hooks are skipped and the wedge does not occur.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.models.terminal import ForkContext
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider


@pytest.fixture(autouse=True)
def provider_defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.toml"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.PROVIDER_DEFAULTS_FILE",
        path,
    )
    return path


def _provider(**kwargs) -> GrokCliProvider:
    defaults = dict(
        terminal_id="term-f297",
        session_name="session",
        window_name="window",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )
    defaults.update(kwargs)
    return GrokCliProvider(**defaults)


def _profile():
    return SimpleNamespace(
        name="grok_dev",
        model=None,
        reasoningEffort=None,
        mcpServers=None,
        system_prompt=None,
    )


class TestF297GrokClaudeHooksDisabled:
    """AC1/AC2: every spawn path must include GROK_CLAUDE_HOOKS_ENABLED=0."""

    def test_basic_spawn_contains_hooks_env_var(self, monkeypatch) -> None:
        """Normal launch command includes GROK_CLAUDE_HOOKS_ENABLED=0."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )

        command = _provider()._build_grok_command()

        assert "GROK_CLAUDE_HOOKS_ENABLED=0" in command

    def test_spawn_with_model_override_contains_hooks_env_var(self, monkeypatch) -> None:
        """Model-override path still includes GROK_CLAUDE_HOOKS_ENABLED=0."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )

        command = _provider(model="grok-3-mini")._build_grok_command()

        assert "GROK_CLAUDE_HOOKS_ENABLED=0" in command
        assert "-m grok-3-mini" in command

    def test_spawn_with_fork_context_contains_hooks_env_var(self, monkeypatch) -> None:
        """Fork-based launch path still includes GROK_CLAUDE_HOOKS_ENABLED=0."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )
        fork_ctx = ForkContext(
            mode="fork",
            session_uuid="test-uuid-1234",
            base_name="base",
            provider="grok_cli",
            initial_preamble="",
        )

        command = _provider(fork_context=fork_ctx)._build_grok_command()

        assert "GROK_CLAUDE_HOOKS_ENABLED=0" in command
        assert "--fork-session" in command

    def test_spawn_with_resume_context_contains_hooks_env_var(self, monkeypatch) -> None:
        """Resume-based launch path still includes GROK_CLAUDE_HOOKS_ENABLED=0."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )
        fork_ctx = ForkContext(
            mode="resume",
            session_uuid="test-uuid-resume",
            base_name="base",
            provider="grok_cli",
            initial_preamble="",
        )

        command = _provider(fork_context=fork_ctx)._build_grok_command()

        assert "GROK_CLAUDE_HOOKS_ENABLED=0" in command
        assert "--resume" in command

    def test_env_var_value_is_zero(self, monkeypatch) -> None:
        """The env var is set to exactly '0', not 'false' or empty."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )

        command = _provider()._build_grok_command()

        # Exact token in the env prefix
        assert " GROK_CLAUDE_HOOKS_ENABLED=0 " in command

    def test_build_fork_command_contains_hooks_env_var(self, monkeypatch) -> None:
        """build_fork_command (used by fork service) also includes the var."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )

        provider = _provider()
        parts = provider.build_fork_command("source-uuid-abc", "new-uuid-xyz")
        full_cmd = " ".join(parts)

        assert "GROK_CLAUDE_HOOKS_ENABLED=0" in full_cmd

    def test_build_resume_command_contains_hooks_env_var(self, monkeypatch) -> None:
        """build_resume_command also includes the var."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
            lambda _name: _profile(),
        )

        provider = _provider()
        parts = provider.build_resume_command("session-uuid-abc")
        full_cmd = " ".join(parts)

        assert "GROK_CLAUDE_HOOKS_ENABLED=0" in full_cmd
