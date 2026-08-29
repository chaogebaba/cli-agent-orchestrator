import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CAO_HOME_DIR", "/data/cao-scratch/f548-gate-repro-home")

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import (
    ClaudeAuthError,
    ClaudeCodeProvider,
    _detect_claude_auth_failure,
)


@pytest.mark.parametrize(
    "pane",
    [
        "Failed to authenticate",
        "OAuth session expired",
        "Paste code here",
        "Paste the code here",
        "Visit the following URL to log in",
        "Visit https://claude.ai/login to authenticate",
    ],
)
def test_each_required_auth_shape_is_independently_detected(pane):
    assert _detect_claude_auth_failure(pane), pane


@pytest.mark.asyncio
async def test_initialize_timeout_branch_raises_named_auth_with_real_tail():
    provider = ClaudeCodeProvider("gate-f548", "sess", "win")
    pane = "sentinel prelude\nFailed to authenticate\nsentinel trailer\n"
    with (
        patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        patch(
            "cli_agent_orchestrator.providers.claude_code.wait_for_shell",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "cli_agent_orchestrator.providers.claude_code.wait_until_status",
            new=AsyncMock(return_value=False),
        ),
        patch.object(provider, "get_init_timeout", return_value=180),
        patch.object(provider, "_ensure_skip_bypass_prompt_setting"),
        patch.object(provider, "_ensure_sandbox_onboarding_state"),
        patch.object(provider, "_build_claude_command", return_value="claude"),
        patch.object(provider, "_handle_startup_prompts", new=AsyncMock(return_value=None)),
    ):
        backend.get_history.return_value = pane
        with pytest.raises(ClaudeAuthError) as exc:
            await provider.initialize()
    message = str(exc.value)
    assert exc.value.code == "E-CLAUDE-AUTH"
    assert "sentinel prelude" in message
    assert "sentinel trailer" in message


def test_unrecognized_choice_classifies_waiting_user_answer():
    pane = (
        "Future unrecognized startup choice\n"
        "❯ 1. Alpha\n  2. Beta\n"
        "Enter to confirm · Esc to cancel"
    )
    provider = ClaudeCodeProvider("gate-f548", "sess", "win")
    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
        return_value=pane.splitlines(),
    ):
        assert provider.get_status(pane) == TerminalStatus.WAITING_USER_ANSWER


@pytest.mark.asyncio
async def test_unrecognized_choice_handler_never_sends_keys_then_status_is_waiting():
    pane = (
        "Future unrecognized startup choice\n"
        "❯ 1. Alpha\n  2. Beta\n"
        "Enter to confirm · Esc to cancel"
    )
    provider = ClaudeCodeProvider("gate-f548", "sess", "win")
    with (
        patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        patch(
            "cli_agent_orchestrator.providers.claude_code.time.monotonic",
            side_effect=[0.0, 0.0, 1.0, 181.0],
        ),
        patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep", new=AsyncMock()),
    ):
        backend.get_history.return_value = pane
        await provider._handle_startup_prompts(idle_gap=20.0, outer_timeout=180.0)
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()
    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_rendered_screen",
        return_value=pane.splitlines(),
    ):
        assert provider.get_status(pane) == TerminalStatus.WAITING_USER_ANSWER
