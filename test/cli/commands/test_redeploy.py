"""Mock-only acceptance tests for the human-operated redeploy command."""

from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import redeploy as command
from cli_agent_orchestrator.cli.main import cli


def _status(*, current=True):
    return {
        "cli_path": "current" if current else "stale",
        "differing_files": 0 if current else 1,
        "server": "current" if current else "restart-needed",
        "source_root": "/repo",
    }


def test_e4_declined_confirmation_installs_without_restart():
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy"
    ) as install, patch.object(command, "_stdin_is_tty", return_value=True), patch.object(
        command, "_live_terminal_session_count", return_value=(7, 3)
    ), patch.object(command.click, "confirm", return_value=False) as confirm, patch.object(
        command, "_restart_server"
    ) as restart:
        result = CliRunner().invoke(cli, ["redeploy"])

    assert result.exit_code == 0
    install.assert_called_once_with("/repo")
    restart.assert_not_called()
    assert "7 live terminal(s) across 3 session(s)" in confirm.call_args.args[0]
    assert "installed, NOT restarted" in result.output


def test_e4_non_tty_without_yes_fails_closed_after_install():
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy"
    ) as install, patch.object(command, "_stdin_is_tty", return_value=False), patch.object(
        command.click, "confirm"
    ) as confirm, patch.object(command, "_restart_server") as restart:
        result = CliRunner().invoke(cli, ["redeploy"])

    assert result.exit_code == 0
    install.assert_called_once_with("/repo")
    confirm.assert_not_called()
    restart.assert_not_called()
    assert "installed, NOT restarted" in result.output


def test_e4_yes_orders_install_restart_verify_and_skips_prompt():
    events = []
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy", side_effect=lambda _root: events.append("install")
    ), patch.object(
        command, "_restart_server", side_effect=lambda: events.append("restart")
    ), patch.object(
        command, "_verify_redeploy",
        side_effect=lambda _root: events.append("verify") or _status(),
    ), patch.object(command.click, "confirm") as confirm:
        result = CliRunner().invoke(cli, ["redeploy", "--yes"])

    assert result.exit_code == 0
    assert events == ["install", "restart", "verify"]
    confirm.assert_not_called()
    assert result.output == (
        "restarting cao-server...\n"
        "waiting for server to come back up... (0s)\n"
        "CLI path: current (0 files differ)\n"
        "server: current (restarted and listening on :9889)\n"
    )


def test_e4_unavailable_count_still_prompts_and_verify_failure_is_nonzero():
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy"
    ), patch.object(command, "_stdin_is_tty", return_value=True), patch.object(
        command, "_live_terminal_session_count", return_value=None
    ), patch.object(command.click, "confirm", return_value=True) as confirm, patch.object(
        command, "_restart_server"
    ), patch.object(command, "_verify_redeploy", return_value=_status(current=False)):
        result = CliRunner().invoke(cli, ["redeploy"])

    assert result.exit_code == 1
    assert "count unavailable" in confirm.call_args.args[0]
    assert "CLI path: stale (1 files differ)" in result.output
    assert "server: restart-needed" in result.output


def test_redeploy_polls_until_listener_appears(monkeypatch):
    statuses = [
        {
            "cli_path": "current",
            "differing_files": 0,
            "server": "not-running",
            "source_root": "/repo",
        },
        _status(),
    ]
    clock = [0.0]
    monkeypatch.setattr(command.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        command.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy"
    ), patch.object(command, "_restart_server"), patch.object(
        command, "_verify_redeploy", side_effect=statuses
    ) as verify:
        result = CliRunner().invoke(cli, ["redeploy", "--yes"])

    assert result.exit_code == 0
    assert verify.call_count == 2
    assert clock[0] == 0.5
    assert "server: current (restarted and listening on :9889)" in result.output


def test_redeploy_timeout_reports_actionable_not_running(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(command.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        command.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    not_running = {
        "cli_path": "current",
        "differing_files": 0,
        "server": "not-running",
        "source_root": "/repo",
    }
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy"
    ), patch.object(command, "_restart_server"), patch.object(
        command, "_verify_redeploy", return_value=not_running
    ):
        result = CliRunner().invoke(cli, ["redeploy", "--yes"])

    assert result.exit_code == 1
    assert clock[0] == 30.0
    assert (
        "server: not-running (no listener on :9889 after 30s - check: "
        "systemctl --user status cao-server; journalctl --user -u cao-server)"
        in result.output
    )


def test_redeploy_non_tty_no_restart_output_is_byte_identical():
    with patch.object(command, "_redeploy_source_root", return_value="/repo"), patch.object(
        command, "_install_redeploy"
    ), patch.object(command, "_stdin_is_tty", return_value=False), patch.object(
        command, "_restart_server"
    ) as restart:
        result = CliRunner().invoke(cli, ["redeploy"])

    assert result.exit_code == 0
    restart.assert_not_called()
    assert result.stdout.encode() == (command._NOT_RESTARTED + "\n").encode()


def test_e4_install_mechanics_match_workspace_script(tmp_path, monkeypatch):
    source = tmp_path / "workspace" / "cli-agent-orchestrator"
    profiles = source.parent / "profiles"
    profiles.mkdir(parents=True)
    source.mkdir()
    (source.parent / "providers.toml.default").write_text("[codex]\n", encoding="utf-8")
    profile_contents = {
        "grok_doc_keeper.md": "---\nname: grok_doc_keeper\nprovider: grok_cli\n---\n",
        "codex_dev.md": "---\nname: codex_dev\nprovider: codex\n---\n",
        "chao_supervisor.md": "---\nname: chao_supervisor\n---\n",
        "kiro_dev.md": (
            "---\n# FROZEN: retained for revival only\nname: kiro_dev\n"
            "provider: kiro_cli\n---\n"
        ),
    }
    for profile, content in profile_contents.items():
        (profiles / profile).write_text(content, encoding="utf-8")
    cao_home = tmp_path / "cao-home"
    monkeypatch.setattr(command, "CAO_HOME_DIR", cao_home)
    calls = []

    with patch.object(command.shutil, "which", return_value="/bin/cao"), patch.object(
        command.subprocess, "run", side_effect=lambda args, check: calls.append(args)
    ):
        command._install_redeploy(source)

    assert calls[0] == [
        "uv", "tool", "install", "--force", "--python", "3.13", str(source)
    ]
    assert calls[1:] == [
        ["/bin/cao", "install", str(profiles / "chao_supervisor.md")],
        ["/bin/cao", "install", str(profiles / "codex_dev.md")],
        ["/bin/cao", "install", str(profiles / "grok_doc_keeper.md")],
    ]
    assert (cao_home / "providers.toml").read_text(encoding="utf-8") == "[codex]\n"
