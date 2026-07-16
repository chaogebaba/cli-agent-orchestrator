from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.utils.grok_config import ensure_grok_mcp_servers


@pytest.fixture(autouse=True)
def provider_defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.toml"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.PROVIDER_DEFAULTS_FILE",
        path,
    )
    return path


def _provider() -> GrokCliProvider:
    return GrokCliProvider(
        terminal_id="term-grok",
        session_name="session",
        window_name="window",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )


def _profile(
    *,
    name: str = "grok_dev",
    model: str | None = None,
    reasoning_effort: str | None = None,
):
    return SimpleNamespace(
        name=name,
        model=model,
        reasoningEffort=reasoning_effort,
        mcpServers=None,
        system_prompt=None,
    )


def test_grok_status_idle_minimal() -> None:
    provider = _provider()
    output = """
   minimal · /help
   ❯
   Composer 2.5 · always-approve · ctrl+o transcript
"""

    assert provider.get_status(output) == TerminalStatus.IDLE


def test_grok_status_idle_without_composer_footer_prefix() -> None:
    provider = _provider()
    output = """
   minimal · /help
   ❯
   grok-composer-2.5-fast · always-approve · ctrl+o transcript
"""

    assert provider.get_status(output) == TerminalStatus.IDLE


def test_grok_status_processing_minimal() -> None:
    provider = _provider()
    output = """
   ❯ Reply with exactly: GROK_SECOND_minimal

   ⠋ Waiting for response… 1.0s                                      1.0s ⇣9.51k
   ❯
   Composer 2.5 · always-approve · 9.5K / 200K (5%) · ctrl+o transcript
"""

    assert provider.get_status(output) == TerminalStatus.PROCESSING


def test_grok_screen_status_processing_from_real_tool_capture() -> None:
    fixture = (
        Path(__file__).parents[1] / "fixtures" / "fx4" / "fx4-grok-toollist-capture.txt"
    )
    screen = [
        line
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_grok_screen_status_processing_from_topanchored_capture() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "fx5"
        / "fx5-topanchored-capture.txt"
    )
    screen = fixture.read_text(encoding="utf-8").splitlines()

    assert _provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_grok_screen_status_processing_thinking_spinner() -> None:
    screen = [
        "⠋ Thinking… 1.0s",
        "❯",
        "Grok 4.5 (high) · always-approve · ctrl+o transcript",
    ]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_grok_screen_status_idle_footer_without_spinner() -> None:
    screen = ["❯", "Grok 4.5 (high) · always-approve · ctrl+o transcript"]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.IDLE


def test_grok_screen_status_completed_marker_with_idle_footer() -> None:
    screen = [
        "Turn completed in 1.5s.",
        "❯",
        "Grok 4.5 (high) · always-approve · ctrl+o transcript",
    ]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.COMPLETED


def test_grok_screen_status_quoted_spinner_line_is_processing() -> None:
    screen = [
        "Quoted capture follows:",
        "⠴ Locate cao-worker-protocols skill… 1.9s",
        "❯",
        "Grok 4.5 (high) · always-approve · ctrl+o transcript",
    ]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_grok_screen_status_spinner_outside_bottom_twelve_is_processing() -> None:
    screen = ["⠴ Old tool run… 1.9s", *[f"output row {i}" for i in range(11)]]
    screen.extend(["❯", "Grok 4.5 (high) · always-approve · ctrl+o transcript"])

    assert _provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_grok_screen_status_glyph_only_line_above_prompt_is_idle() -> None:
    screen = [
        "⠴",
        "❯",
        "Grok 4.5 (high) · always-approve · ctrl+o transcript",
    ]

    assert _provider().get_status_from_screen(screen) == TerminalStatus.IDLE


def test_grok_status_completed_minimal() -> None:
    provider = _provider()
    provider.mark_input_received()
    output = """
   ❯ Reply with exactly: GROK_SECOND_minimal

   ◆ Thought for 0.1s
   GROK_SECOND_minimal
   Turn completed in 1.5s.
   minimal · /help
   ❯
   Composer 2.5 · always-approve · 9.6K / 200K (5%) · ctrl+o transcript
"""

    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_grok_status_completed_when_model_discusses_errors() -> None:
    provider = _provider()
    provider.mark_input_received()
    output = """
   ❯ Explain common error handling failures.

   Error handling can fail when a rate limit error is retried too quickly.
   Turn completed in 1.5s.
   minimal · /help
   ❯
   grok-composer-2.5-fast · always-approve · ctrl+o transcript
"""

    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_grok_status_error_only_after_last_completion() -> None:
    provider = _provider()
    provider.mark_input_received()
    output = """
   ❯ Explain common error handling failures.

   Error: this line is model prose before completion.
   Turn completed in 1.5s.
   minimal · /help
   ❯
   Error: authentication required
"""

    assert provider.get_status(output) == TerminalStatus.ERROR


def test_grok_status_waiting_for_project_directory_picker() -> None:
    provider = _provider()
    output = """
  ┃  Run Grok Build in a project directory?
  ┃
  ┃  1 (○) minimal (current)   /tmp/cao-grok-probe.AlB5Ka/minimal
  ┃  z (○) Type your answer here
  ┃
  ┃  ↑/↓ navigate · y copy                                    Enter:submit
"""

    assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
    assert (
        provider.get_status_from_screen(output.splitlines())
        == TerminalStatus.WAITING_USER_ANSWER
    )


def test_grok_extract_last_message_from_minimal_scrollback() -> None:
    provider = _provider()
    output = """
   ❯ Reply with exactly: GROK_PROBE_minimal_DONE

   ◆ Thought for 0.3s
   GROK_PROBE_minimal_DONE
   Turn completed in 0.0s.

   ❯ Reply with exactly: GROK_SECOND_minimal

   ◆ Thought for 0.1s
   GROK_SECOND_minimal
   Turn completed in 1.5s.
   minimal · /help
   ❯
   Composer 2.5 · always-approve · 9.6K / 200K (5%) · ctrl+o transcript
"""

    assert provider.extract_last_message_from_script(output) == "GROK_SECOND_minimal"


def test_grok_read_composer_draft() -> None:
    provider = _provider()
    screen = """
   ❯ Reply with exactly: GROK_SECOND_minimal
   Turn completed in 1.5s.
   minimal · /help
   ❯ HUMAN_DRAFT_MINIMAL_ABC
   Composer 2.5 · always-approve · 9.6K / 200K (5%) · ctrl+o transcript
""".splitlines()

    assert provider.read_composer_draft(screen) == "HUMAN_DRAFT_MINIMAL_ABC"


def test_grok_read_empty_composer() -> None:
    provider = _provider()
    screen = """
   minimal · /help
   ❯
   Composer 2.5 · always-approve · ctrl+o transcript
""".splitlines()

    assert provider.read_composer_draft(screen) == ""


def test_ensure_grok_mcp_servers_upserts_user_config_without_clobber(
    tmp_path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[ui]\n"
        'theme = "dark"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-mcp"\n'
        "enabled = true\n"
        "\n"
        "[mcp_servers.cao-mcp-server]\n"
        'command = "old"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.grok_config.GROK_CONFIG_FILE",
        config_file,
    )

    ensure_grok_mcp_servers(
        {
            "cao-mcp-server": {
                "command": "cao-mcp-server",
                "args": ["--stdio"],
                "env": {"CAO_MODE": "test"},
            }
        }
    )

    text = config_file.read_text(encoding="utf-8")
    assert '[ui]\ntheme = "dark"' in text
    assert "[mcp_servers.other]" in text
    assert 'command = "other-mcp"' in text
    assert "[mcp_servers.cao-mcp-server]" in text
    assert 'command = "cao-mcp-server"' in text
    assert '    "--stdio",' in text
    assert "[mcp_servers.cao-mcp-server.env]" in text
    assert 'CAO_MODE = "test"' in text
    assert 'command = "old"' not in text


def test_ensure_grok_mcp_servers_preserves_config_when_replace_fails(
    tmp_path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    original = '[ui]\ntheme = "dark"\n'
    config_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.grok_config.GROK_CONFIG_FILE",
        config_file,
    )

    def fail_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr("cli_agent_orchestrator.utils.grok_config.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        ensure_grok_mcp_servers({"cao-mcp-server": {"command": "cao-mcp-server"}})

    assert config_file.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


def test_grok_command_uses_default_model_when_profile_unset(
    provider_defaults_file, monkeypatch
) -> None:
    profile = type(
        "Profile",
        (),
        {
            "model": None,
            "mcpServers": None,
            "system_prompt": None,
        },
    )()
    provider_defaults_file.write_text(
        '[grok_cli]\nmodel = "grok-composer-2.5-fast"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: profile,
    )

    command = _provider()._build_grok_command()

    assert "-m grok-composer-2.5-fast" in command


def test_grok_command_default_model_wins_over_profile(
    provider_defaults_file, monkeypatch
) -> None:
    profile = type(
        "Profile",
        (),
        {
            "model": "grok-profile-model",
            "mcpServers": None,
            "system_prompt": None,
        },
    )()
    provider_defaults_file.write_text(
        '[grok_cli]\nmodel = "grok-composer-2.5-fast"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: profile,
    )

    command = _provider()._build_grok_command()

    assert "-m grok-composer-2.5-fast" in command
    assert "grok-profile-model" not in command


def test_grok_command_empty_default_model_suppresses_profile_model(
    provider_defaults_file, monkeypatch
) -> None:
    profile = type(
        "Profile",
        (),
        {
            "model": "grok-profile-model",
            "mcpServers": None,
            "system_prompt": None,
        },
    )()
    provider_defaults_file.write_text(
        '[grok_cli]\nmodel = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: profile,
    )

    command = _provider()._build_grok_command()

    assert " -m " not in command
    assert "grok-profile-model" not in command


def test_grok_command_uses_profile_model_when_default_absent(monkeypatch) -> None:
    profile = type(
        "Profile",
        (),
        {
            "model": "grok-profile-model",
            "mcpServers": None,
            "system_prompt": None,
        },
    )()
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: profile,
    )

    command = _provider()._build_grok_command()

    assert "-m grok-profile-model" in command


def test_grok_command_absent_defaults_preserves_current_behavior(monkeypatch) -> None:
    profile = type(
        "Profile",
        (),
        {
            "model": None,
            "mcpServers": None,
            "system_prompt": None,
        },
    )()
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: profile,
    )

    command = _provider()._build_grok_command()

    assert " -m " not in command


def test_grok_command_per_profile_effort_wins(
    provider_defaults_file, monkeypatch
) -> None:
    provider_defaults_file.write_text(
        "[grok_cli]\n"
        'reasoning_effort = "low"\n'
        "\n"
        "[grok_cli.profiles.grok_dev]\n"
        'reasoning_effort = "high"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(reasoning_effort="medium"),
    )

    command = _provider()._build_grok_command()

    assert "--reasoning-effort high" in command
    assert "medium" not in command
    assert "low" not in command


def test_grok_command_provider_effort_wins_over_profile(
    provider_defaults_file, monkeypatch
) -> None:
    provider_defaults_file.write_text(
        '[grok_cli]\nreasoning_effort = "medium"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(reasoning_effort="high"),
    )

    command = _provider()._build_grok_command()

    assert "--reasoning-effort medium" in command
    assert "high" not in command


def test_grok_command_profile_effort_used_when_toml_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(reasoning_effort="high"),
    )

    command = _provider()._build_grok_command()

    assert "--reasoning-effort high" in command


def test_grok_command_absent_effort_omits_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(),
    )

    command = _provider()._build_grok_command()

    assert " --reasoning-effort " not in command


def test_grok_command_empty_per_profile_effort_clears_lower_tiers(
    provider_defaults_file, monkeypatch
) -> None:
    provider_defaults_file.write_text(
        "[grok_cli]\n"
        'reasoning_effort = "medium"\n'
        "\n"
        "[grok_cli.profiles.grok_dev]\n"
        'reasoning_effort = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(reasoning_effort="high"),
    )

    command = _provider()._build_grok_command()

    assert " --reasoning-effort " not in command
    assert "medium" not in command
    assert "high" not in command


def test_grok_command_per_profile_model_wins(
    provider_defaults_file, monkeypatch
) -> None:
    provider_defaults_file.write_text(
        "[grok_cli]\n"
        'model = "grok-provider-model"\n'
        "\n"
        "[grok_cli.profiles.grok_dev]\n"
        'model = "grok-profile-table-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(model="grok-frontmatter-model"),
    )

    command = _provider()._build_grok_command()

    assert "-m grok-profile-table-model" in command
    assert "grok-provider-model" not in command
    assert "grok-frontmatter-model" not in command


def test_grok_command_empty_per_profile_model_clears_lower_tiers(
    provider_defaults_file, monkeypatch
) -> None:
    provider_defaults_file.write_text(
        "[grok_cli]\n"
        'model = "grok-provider-model"\n'
        "\n"
        "[grok_cli.profiles.grok_dev]\n"
        'model = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.grok_cli.load_agent_profile",
        lambda _name: _profile(model="grok-frontmatter-model"),
    )

    command = _provider()._build_grok_command()

    assert " -m " not in command
    assert "grok-provider-model" not in command
    assert "grok-frontmatter-model" not in command
