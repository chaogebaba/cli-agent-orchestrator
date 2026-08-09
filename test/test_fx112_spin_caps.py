"""fx112 — Iteration cap pinning tests for latent busy-spin loops.

Each test freezes time and mocks sleep to return instantly, then asserts
the loop exits via the iteration cap (not wall-clock timeout).
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Test 1: copilot _accept_trust_prompts cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copilot_accept_trust_prompts_cap() -> None:
    """Loop must exit at timeout*3 iterations when time is frozen."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

    provider = CopilotCliProvider.__new__(CopilotCliProvider)
    provider.session_name = "ses"
    provider.window_name = "win"
    provider.terminal_id = "t1"

    frozen_time = 1000.0
    timeout = 10.0
    expected_cap = int(timeout / 1.0 * 3)  # 30

    call_count = 0

    async def mock_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        return "nothing here"

    with (
        patch("time.time", return_value=frozen_time),
        patch("asyncio.to_thread", side_effect=mock_to_thread),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await provider._accept_trust_prompts(timeout=timeout)

    assert call_count <= expected_cap + 1


# ---------------------------------------------------------------------------
# Test 2: copilot _wait_for_shell_ready cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copilot_wait_for_shell_ready_cap() -> None:
    """Loop must exit at timeout/polling_interval*3 iterations when time is frozen."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

    provider = CopilotCliProvider.__new__(CopilotCliProvider)
    provider.session_name = "ses"
    provider.window_name = "win"

    frozen_time = 1000.0
    timeout = 6.0
    polling_interval = 0.5
    expected_cap = int(timeout / polling_interval * 3)  # 36

    call_count = 0

    async def mock_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        return "changing output " + str(call_count)

    with (
        patch("time.time", return_value=frozen_time),
        patch("asyncio.to_thread", side_effect=mock_to_thread),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        result = await provider._wait_for_shell_ready(
            timeout=timeout, polling_interval=polling_interval
        )

    assert result is False
    assert call_count <= expected_cap + 1


# ---------------------------------------------------------------------------
# Test 3: copilot initialize readiness cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copilot_initialize_readiness_cap() -> None:
    """Initialize readiness loop must exit at 180 iterations when time is frozen."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider

    provider = CopilotCliProvider.__new__(CopilotCliProvider)
    provider.session_name = "ses"
    provider.window_name = "win"
    provider.terminal_id = "t1"
    provider._initialized = False
    provider._agent_profile = None
    provider._model = None
    provider._copilot_help_text_cache = None

    frozen_time = 1000.0
    expected_cap = int(60.0 / 1.0 * 3)  # 180

    call_count = 0

    async def mock_sleep(t):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1

    from cli_agent_orchestrator.models.terminal import TerminalStatus

    mock_status_monitor = MagicMock()
    mock_status_monitor.get_status.return_value = TerminalStatus.UNKNOWN

    async def mock_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return "copilot --fake"

    with (
        patch("time.time", return_value=frozen_time),
        patch("asyncio.sleep", side_effect=mock_sleep),
        patch("asyncio.to_thread", side_effect=mock_to_thread),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor",
            mock_status_monitor,
        ),
        patch(
            "cli_agent_orchestrator.providers.copilot_cli.get_server_settings",
            return_value={"provider_init_timeout": 30},
        ),
        patch(
            "cli_agent_orchestrator.providers.copilot_cli.wait_for_shell",
            new_callable=lambda: AsyncMock(return_value=True),
        ),
        patch(
            "cli_agent_orchestrator.providers.copilot_cli.get_backend",
        ),
        pytest.raises(TimeoutError),
    ):
        await provider.initialize()

    # call_count covers both _accept_trust_prompts and the readiness loop sleeps;
    # the readiness loop alone is bounded at 180.
    assert call_count <= expected_cap + 100  # generous headroom for trust phase


# ---------------------------------------------------------------------------
# Test 4: claude_code wait_until_input_ready cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_code_wait_until_input_ready_cap() -> None:
    """Loop must exit at timeout/0.5*3 iterations when time is frozen."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    provider.session_name = "ses"
    provider.window_name = "win"
    provider.terminal_id = "t1"

    timeout = 5.0
    expected_cap = int(timeout / 0.5 * 3)  # 30
    frozen_mono = 1000.0

    call_count = 0

    async def mock_sleep(t):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1

    mock_backend = MagicMock()
    # Return changing content so it never stabilizes
    mock_backend.get_history = MagicMock(side_effect=lambda *a, **kw: f"content {call_count}")

    with (
        patch("time.monotonic", return_value=frozen_mono),
        patch("asyncio.sleep", side_effect=mock_sleep),
        patch(
            "cli_agent_orchestrator.providers.claude_code.get_backend", return_value=mock_backend
        ),
    ):
        result = await provider.wait_until_input_ready(timeout=timeout)

    assert result is False
    assert call_count <= expected_cap + 1


# ---------------------------------------------------------------------------
# Test 5: terminal_service _wait_for_base_ready cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_service_wait_for_base_ready_cap() -> None:
    """Loop must exit at computed cap when time is frozen."""
    from cli_agent_orchestrator.services import terminal_service

    frozen_mono = 1000.0
    deadline = frozen_mono + 5.0  # 5s budget
    expected_cap = int((deadline - frozen_mono) / 0.1 * 3)  # 150

    call_count = 0

    async def mock_sleep(t):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1

    from cli_agent_orchestrator.models.terminal import TerminalStatus

    with (
        patch("time.monotonic", return_value=frozen_mono),
        patch("asyncio.sleep", side_effect=mock_sleep),
        patch.object(
            terminal_service.status_monitor,
            "get_status",
            return_value=TerminalStatus.UNKNOWN,
        ),
        patch.object(
            terminal_service,
            "terminal_exists",
            return_value=True,
        ),
    ):
        result = await terminal_service._wait_for_base_ready("base1", deadline)

    assert result is False
    assert call_count <= expected_cap + 1


# ---------------------------------------------------------------------------
# Test 6: herdr socket poll cap
# ---------------------------------------------------------------------------


def test_herdr_socket_poll_cap() -> None:
    """Socket poll loop must exit at 450 iterations when time is frozen."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
        from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    backend = HerdrBackend.__new__(HerdrBackend)
    backend._herdr_session = "test-session"
    backend._herdr_bin = "/usr/bin/herdr"

    frozen_time = 1000.0
    expected_cap = int(15.0 / 0.1 * 3)  # 450

    call_count = 0
    original_sleep = time.sleep

    def mock_sleep(t):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1

    with (
        patch("time.time", return_value=frozen_time),
        patch("time.sleep", side_effect=mock_sleep),
        patch("os.path.exists", return_value=False),
        patch("subprocess.Popen"),
        patch.object(backend, "_session_socket_path", return_value="/tmp/fake.sock"),
    ):
        backend._ensure_session_running()

    # Subtract 1 for the initial 0.5s sleep before the poll loop
    poll_iterations = call_count - 1
    assert poll_iterations <= expected_cap + 1


# ---------------------------------------------------------------------------
# Test 7: draft_guard _wait_for_stable_draft cap
# ---------------------------------------------------------------------------


def test_draft_guard_wait_for_stable_draft_cap() -> None:
    """Stability loop must exit at 180 iterations when time is frozen."""
    from cli_agent_orchestrator.services import draft_guard
    from cli_agent_orchestrator.services.draft_guard import _wait_for_stable_draft

    expected_cap = int(30.0 / 0.5 * 3)  # 180
    frozen_mono = 1000.0

    call_count = 0

    def mock_sleep(t):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1

    # Return changing content so it never stabilizes
    read_count = [0]

    def mock_read_draft(*args, **kwargs):  # type: ignore[no-untyped-def]
        read_count[0] += 1
        return f"draft content {read_count[0]}"

    with (
        patch("time.monotonic", return_value=frozen_mono),
        patch("time.sleep", side_effect=mock_sleep),
        patch.object(draft_guard, "_read_provider_draft", side_effect=mock_read_draft),
    ):
        result = _wait_for_stable_draft("t1", {}, MagicMock(), "initial draft")

    # Subtract 1 for the initial delay sleep before entering the loop
    loop_iterations = call_count - 1
    assert loop_iterations <= expected_cap + 1
    assert result is not None


# ---------------------------------------------------------------------------
# Test 8: floor guarantee — cap never drops to 0 (B1 regression pin)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_base_ready_floor_guarantees_one_iteration() -> None:
    """When deadline is nearly reached, cap must be >= 1 so the loop body runs."""
    from cli_agent_orchestrator.services import terminal_service

    frozen_mono = 1000.0
    # Tiny budget: 0.02s → int(0.02/0.1*3) = int(0.6) = 0 without floor
    deadline = frozen_mono + 0.02

    call_count = 0

    async def mock_sleep(t):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1

    from cli_agent_orchestrator.models.terminal import TerminalStatus

    with (
        patch("time.monotonic", return_value=frozen_mono),
        patch("asyncio.sleep", side_effect=mock_sleep),
        patch.object(
            terminal_service.status_monitor,
            "get_status",
            return_value=TerminalStatus.UNKNOWN,
        ),
        patch.object(
            terminal_service,
            "terminal_exists",
            return_value=True,
        ),
    ):
        result = await terminal_service._wait_for_base_ready("base1", deadline)

    assert result is False
    # Without max(1,...) floor this would be 0 iterations; with floor it's >= 1
    assert call_count >= 1
