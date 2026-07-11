import json
import os
import shlex
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


@pytest.mark.parametrize("profile", [
    AgentProfile(name="full", description="", model="sonnet"),
    AgentProfile(name="thin", description="", native_agent="native"),
    None,
])
def test_every_claude_route_gets_terminal_settings(profile):
    provider = ClaudeCodeProvider("hookterm", "session", "window", "route")
    loader = patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    with loader as mocked:
        if profile is None:
            mocked.side_effect = FileNotFoundError()
        else:
            mocked.return_value = profile
        command = shlex.split(provider._build_claude_command())
    settings_path = command[command.index("--settings") + 1]
    settings = json.loads(open(settings_path, encoding="utf-8").read())
    hook = settings["hooks"]["SessionStart"][0]["hooks"][0]
    assert "cli_agent_orchestrator.hooks.transcript_binding" in hook["command"]
    assert "CAO_AUTH_LOCAL_TOKEN" not in json.dumps(settings)
    assert hook["timeout"] == 5


def test_project_and_generated_session_start_hooks_both_fire(tmp_path):
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude binary is not installed")
    project_marker = tmp_path / "project-hook-fired"
    generated_marker = tmp_path / "generated-hook-fired"
    project_settings = {
        "hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": shlex.join([
                sys.executable, "-c",
                f"from pathlib import Path; Path({str(project_marker)!r}).touch()",
            ]),
        }]}]}
    }
    project_file = tmp_path / ".claude" / "settings.json"
    project_file.parent.mkdir()
    project_file.write_text(json.dumps(project_settings), encoding="utf-8")

    provider = ClaudeCodeProvider("hookterm", "session", "window", None)
    generated_path = provider._write_terminal_settings()
    try:
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        generated["hooks"]["SessionStart"][0]["hooks"][0]["command"] = shlex.join([
            sys.executable, "-c",
            f"from pathlib import Path; Path({str(generated_marker)!r}).touch()",
        ])
        generated_path.write_text(json.dumps(generated), encoding="utf-8")
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("CLAUDE")
        }
        subprocess.run(
            [claude, "-p", "Reply with exactly OK.", "--settings", str(generated_path)],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=True,
        )
    finally:
        generated_path.unlink(missing_ok=True)
    assert project_marker.exists()
    assert generated_marker.exists()


@pytest.mark.parametrize("failed_generated", [0, 1])
def test_project_and_two_generated_hooks_are_additive_and_failure_isolated(
    tmp_path, failed_generated
):
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude binary is not installed")
    project_marker = tmp_path / "project-hook-fired"
    generated_markers = [tmp_path / "generated-0-fired", tmp_path / "generated-1-fired"]
    project_file = tmp_path / ".claude" / "settings.json"
    project_file.parent.mkdir()
    project_file.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": shlex.join([
                sys.executable, "-c",
                f"from pathlib import Path; Path({str(project_marker)!r}).touch()",
            ]),
        }]}]},
    }), encoding="utf-8")

    raw = "---\nname: supervisor\ndescription: test\nsessionBrief: required\n---\ncharter\n"
    provider = ClaudeCodeProvider("hookterm", "session", "window", "supervisor")
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.read_agent_profile_source",
        return_value=raw,
    ):
        generated_path = provider._write_terminal_settings()
    try:
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        hooks = generated["hooks"]["SessionStart"][0]["hooks"]
        assert len(hooks) == 2
        for index, hook in enumerate(hooks):
            hook["command"] = (
                shlex.join([sys.executable, "-c", "raise SystemExit(7)"])
                if index == failed_generated
                else shlex.join([
                    sys.executable, "-c",
                    f"from pathlib import Path; Path({str(generated_markers[index])!r}).touch()",
                ])
            )
        generated_path.write_text(json.dumps(generated), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")}
        subprocess.run(
            [claude, "-p", "Reply with exactly OK.", "--settings", str(generated_path)],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60, check=True,
        )
    finally:
        generated_path.unlink(missing_ok=True)
    assert project_marker.exists()
    assert not generated_markers[failed_generated].exists()
    assert generated_markers[1 - failed_generated].exists()
