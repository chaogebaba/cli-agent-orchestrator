"""Fail-closed terminal-service coverage for Kiro Phase 0."""

import asyncio
import subprocess
import threading
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    KiroCapabilityError,
    probe_kiro_capabilities,
)
from cli_agent_orchestrator.services.agent_step import run_agent_step
from cli_agent_orchestrator.services.terminal_service import create_terminal

_MODULE = "cli_agent_orchestrator.services.terminal_service"


pytestmark = pytest.mark.usefixtures("isolated_memory_db")


@pytest.mark.asyncio
async def test_capability_probe_does_not_block_event_loop():
    """A synchronous capability probe runs in a worker while async work advances."""
    probe_started = threading.Event()
    release_probe = threading.Event()
    heartbeat_ticks = 0

    def blocking_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return KiroCapabilities(
            version="3.0.0", flags=frozenset({"--v3", "--agent", "--trust-all-tools"})
        )

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not release_probe.is_set():
            heartbeat_ticks += 1
            await asyncio.sleep(0)

    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None

    with (
        patch(
            f"{_MODULE}.load_agent_profile",
            return_value=AgentProfile(
                name="kas-profile",
                description="KAS profile",
                engine=KiroEngine.KAS,
            ),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="testkas1"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="kas-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider
        create_task = asyncio.create_task(
            create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                kiro_capability_probe=blocking_probe,
            )
        )
        heartbeat_task = asyncio.create_task(heartbeat())
        while not probe_started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        ticks_while_blocked = heartbeat_ticks
        release_probe.set()

        terminal = await create_task
        await heartbeat_task

    assert ticks_while_blocked > 0
    assert terminal.engine == KiroEngine.KAS
    backend.return_value.create_session.assert_called_once()
    db_create.assert_called_once()
    providers.create_provider.assert_called_once()


@pytest.mark.asyncio
async def test_kas_probes_then_proceeds_to_backend_and_persistence_allocation():
    """KAS is enabled: probe then allocate backend/provider/DB like v2."""
    probe = Mock(
        return_value=KiroCapabilities(
            version="2.13.0", flags=frozenset({"--v3", "--agent", "--trust-all-tools"})
        )
    )
    profile = AgentProfile(
        name="kas-profile",
        description="KAS profile",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
    )
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="testkas2"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="kas-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider
        terminal = await create_terminal(
            provider="kiro_cli",
            agent_profile="kas-profile",
            new_session=True,
            kiro_capability_probe=probe,
        )

    assert terminal.engine == KiroEngine.KAS
    probe.assert_called_once_with(KiroEngine.KAS, {"profile", "trust"})
    backend.return_value.create_session.assert_called_once()
    db_create.assert_called_once()
    assert db_create.call_args.kwargs["engine"] == "kas"
    assert providers.create_provider.call_args.kwargs["engine"] == KiroEngine.KAS
    provider.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_and_profile_engines_conflict_before_probe_or_allocation():
    profile = AgentProfile(name="kas-profile", description="KAS profile", engine=KiroEngine.KAS)
    probe = Mock()

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
    ):
        with pytest.raises(ValueError, match="conflict"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                engine=KiroEngine.V2,
                kiro_capability_probe=probe,
            )

    probe.assert_not_called()
    backend.return_value.create_session.assert_not_called()
    db_create.assert_not_called()


@pytest.mark.asyncio
async def test_omitted_engine_launches_as_explicitly_pinned_v2():
    """Callers that omit engine still create a Kiro provider pinned to v2."""
    probe = Mock(
        return_value=KiroCapabilities(
            version="2.13.0",
            flags=frozenset({"--agent-engine", "--agent", "--trust-all-tools"}),
        )
    )
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None

    with (
        patch(
            f"{_MODULE}.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="test1234"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider

        terminal = await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            new_session=True,
            kiro_capability_probe=probe,
        )

    assert terminal.engine == KiroEngine.V2
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "trust", "ui"})
    assert providers.create_provider.call_args.kwargs["engine"] == KiroEngine.V2
    assert db_create.call_args.kwargs["engine"] == "v2"


@pytest.mark.asyncio
async def test_explicit_model_override_is_probed_even_when_profile_has_none():
    """An override reaching --model must first be probed for the model capability.

    The launch path resolves `model or profile.model`; probing only the profile
    snapshot would launch a flag the wrapper was never verified to support.
    """
    probe = Mock(
        return_value=KiroCapabilities(
            version="2.13.0",
            flags=frozenset({"--agent-engine", "--agent", "--trust-all-tools", "--model"}),
        )
    )
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None

    with (
        patch(
            f"{_MODULE}.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal"),
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="test1234"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider

        await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            new_session=True,
            model="claude-sonnet-5",
            kiro_capability_probe=probe,
        )

    assert "model" in probe.call_args.args[1]
    assert providers.create_provider.call_args.kwargs["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_toml_only_model_missing_flag_rejects_before_allocation():
    """F107 B2 r1 B1: toml-only model must reach the probe and refuse before allocate.

    A providers.toml ``model = "auto"`` with no spawn override and no profile
    model must request the model capability; a wrapper lacking ``--model``
    fails closed with zero backend/DB/provider allocation.
    """

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="kiro-cli version 2.13.0", stderr="")
        # Advertise agent/trust/ui but NOT --model.
        output = (
            "--agent-engine v2|v1|v3\n--v3\n--agent NAME\n"
            "--legacy-ui\n--trust-all-tools\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        return probe_kiro_capabilities(engine, requested, runner=runner)

    with (
        patch(
            f"{_MODULE}.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(
            f"{_MODULE}.get_provider_defaults",
            return_value={"model": "auto"},
        ),
        patch(
            f"{_MODULE}.get_provider_profile_defaults",
            return_value={},
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="--model") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.capability == "--model"
    assert "model" in (exc_info.value.capability or "")
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_non_yolo_v2_missing_legacy_ui_rejects_before_allocation():
    """The optional fallback flag is probed before any v2 lifecycle allocation."""

    def missing_ui_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else "--agent-engine v2\n--agent NAME\n--trust-all-tools\n"
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=missing_ui_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="--legacy-ui") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--legacy-ui"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "trust", "ui"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_yolo_v2_prose_only_trust_flag_rejects_before_allocation():
    """A prose-only yolo flag cannot authorize terminal lifecycle allocation."""

    def prose_only_trust_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else (
                    "--agent-engine v2\n--agent NAME\n--legacy-ui\n"
                    "--trust-all-tools bypasses confirmation when enabled\n"
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=prose_only_trust_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                allowed_tools=["*"],
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--trust-all-tools"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "ui", "trust"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_yolo_v2_required_value_trust_flag_rejects_before_allocation():
    """A yolo launch cannot use a wrapper that requires a trust option value."""

    def required_value_trust_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else ("--agent-engine v2\n--agent NAME\n--legacy-ui\n" "--trust-all-tools VALUE\n")
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=required_value_trust_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id") as terminal_id,
    ):
        with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                allowed_tools=["*"],
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--trust-all-tools"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "ui", "trust"})
    terminal_id.assert_not_called()
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_v2_agent_engine_value_exclusion_rejects_before_allocation():
    """A wrapper advertising only v1/v3 must not allocate a v2 lifecycle."""

    def v1_v3_only_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else ("--agent-engine v1|v3\n--agent NAME\n" "--legacy-ui\n--trust-all-tools\n")
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=v1_v3_only_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="accept 'v2'") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--agent-engine=v2"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "trust", "ui"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


def test_send_input_allows_persisted_kas_to_reach_provider():
    """A5 flip: persisted KAS terminals reach get_provider / paste path."""
    metadata = {
        "id": "persisted-kas",
        "provider": "kiro_cli",
        "engine": "kas",
        "tmux_session": "cao-session",
        "tmux_window": "developer-window",
    }
    provider = MagicMock()
    provider.paste_enter_count = 1
    provider.composer_stash_keys = None
    provider.blocks_orchestrated_input_while_waiting_user_answer = False

    with (
        patch(f"{_MODULE}.get_terminal_metadata", return_value=metadata),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.status_monitor") as status_monitor,
        patch(f"{_MODULE}.inject_memory_context", side_effect=lambda m, _t, *_a, **_k: m),
        patch(f"{_MODULE}.preserve_draft_before_send", return_value=None),
        patch(f"{_MODULE}._append_message_contract", side_effect=lambda m, *_a, **_k: m),
    ):
        providers.get_provider.return_value = provider
        status_monitor.get_status.return_value = MagicMock()
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        status_monitor.get_status.return_value = TerminalStatus.IDLE
        from cli_agent_orchestrator.services.terminal_service import send_input

        send_input("persisted-kas", "hello kas")

    providers.get_provider.assert_called_once_with("persisted-kas")
    backend.return_value.send_keys.assert_called()


@pytest.mark.asyncio
async def test_agent_step_reuse_allows_persisted_kas_to_reach_send():
    """A5/A7 flip: reusing a KAS terminal reaches the send path (no Phase-0 guard)."""
    metadata = {
        "id": "persisted-kas",
        "provider": "kiro_cli",
        "engine": "kas",
        "tmux_session": "cao-session",
        "tmux_window": "developer-window",
    }

    with (
        patch(f"{_MODULE}.get_terminal_metadata", return_value=metadata),
        patch(
            "cli_agent_orchestrator.services.agent_step.terminal_service.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.services.agent_step.terminal_service.send_input",
            return_value=True,
        ) as send,
        patch(
            "cli_agent_orchestrator.services.agent_step.wait_until_status",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.services.agent_step._wait_for_completion",
            new_callable=AsyncMock,
        ) as wait_done,
        patch(
            "cli_agent_orchestrator.services.agent_step.terminal_service.get_output",
            return_value="done",
        ),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.get_backend") as backend,
    ):
        from cli_agent_orchestrator.services.agent_step import _CompletionOutcome
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        wait_done.return_value = _CompletionOutcome.COMPLETED
        # ready wait uses status_monitor via wait_until_status mock above
        result = await run_agent_step(
            provider="kiro_cli",
            agent="developer",
            prompt="deliver to kas",
            reuse_terminal_id="persisted-kas",
            teardown=False,
        )

    assert result.terminal_id == "persisted-kas"
    send.assert_called_once()
    # send_input is mocked at agent_step layer — pane keys not touched here
    backend.return_value.send_keys.assert_not_called()
