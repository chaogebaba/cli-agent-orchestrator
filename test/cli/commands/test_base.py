"""CLI contract tests for ``cao base register``."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.base import base
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.constants import API_BASE_URL


ARGS = [
    "register", "offline", "--provider", "codex", "--uuid",
    "11111111-1111-4111-8111-111111111111", "--cwd", "/repo",
    "--profile", "codex_profile", "--summary", "stored history",
]
ROW = {
    "name": "offline", "provider": "codex", "profile": "codex_profile",
    "cwd": "/repo", "session_uuid": ARGS[5], "kind": "base",
    "session_name": None, "source_terminal_id": None, "git_sha": "a" * 40,
    "dirty_hashes": "{}", "superseded": False,
}


def test_base_register_posts_exact_body_and_prints_row():
    response = MagicMock(status_code=200)
    response.json.return_value = ROW
    with patch(
        "cli_agent_orchestrator.cli.commands.base.requests.post",
        return_value=response,
    ) as post:
        result = CliRunner().invoke(base, ARGS)

    assert result.exit_code == 0
    post.assert_called_once_with(
        f"{API_BASE_URL}/bases/register",
        json={
            "name": "offline", "provider": "codex",
            "session_uuid": "11111111-1111-4111-8111-111111111111",
            "cwd": "/repo", "profile": "codex_profile",
            "summary": "stored history",
        },
    )
    assert '"source_terminal_id": null' in result.output
    assert '"superseded": false' in result.output


def test_base_register_domain_reject_prints_code_message_and_exits_one():
    response = MagicMock(status_code=400)
    response.json.return_value = {
        "detail": {"code": "artifact_not_found", "message": "missing history"}
    }
    with patch(
        "cli_agent_orchestrator.cli.commands.base.requests.post",
        return_value=response,
    ):
        result = CliRunner().invoke(base, ARGS)

    assert result.exit_code == 1
    assert result.output.strip() == "artifact_not_found: missing history"
    response.raise_for_status.assert_not_called()


def test_base_register_provider_choice_is_exact_and_command_is_installed():
    invalid = CliRunner().invoke(
        base,
        [
            "register", "offline", "--provider", "kiro_cli", "--uuid", "uuid",
            "--cwd", "/repo", "--profile", "kiro",
        ],
    )
    assert invalid.exit_code == 2
    assert "'codex', 'grok_cli'" in invalid.output

    help_result = CliRunner().invoke(cli, ["base", "--help"])
    assert help_result.exit_code == 0
    assert "register" in help_result.output
