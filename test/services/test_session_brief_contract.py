import json
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.services import terminal_service as svc


def _complete_manifest():
    return {
        "schema_version": "cao.session-manifest/v1", "generated_at": "now",
        "complete": True, "errors": [],
        "sections": {name: "ok" for name in (
            "profiles", "ready_bases", "skills", "workflows", "terminals", "activation"
        )},
        "session": {"name": "cao-test", "supervisor_terminal_id": "term0001", "epoch": None, "epoch_started_at": None},
        "profiles": [{"name": "supervisor", "role": "supervisor", "provider": "codex", "skills": [], "charter_digest": "digest"}],
        "ready_bases": [], "skills": [], "workflows": [], "tools": None,
        "terminals": [{"id": "term0001", "profile": "supervisor", "provider": "codex", "status": "idle", "kind": "supervisor"}],
        "ledger": {"pending_rows": None},
        "activation": {"cli_path": "current", "differing_files": 0, "server": "current", "source_root": "/repo"},
    }


def test_rollback_order_stops_pipe_before_fifo_and_window(monkeypatch):
    calls = []
    backend = Mock()
    backend.stop_pipe_pane.side_effect = lambda *_: calls.append("pipe")
    backend.kill_window.side_effect = lambda *_: calls.append("window")
    monkeypatch.setattr(svc, "get_backend", lambda: backend)
    monkeypatch.setattr(svc, "db_delete_terminal", lambda *_: calls.append("db"))
    monkeypatch.setattr(svc.fifo_manager, "stop_reader", lambda *_: calls.append("fifo"))
    svc._rollback_terminal_creation(
        "term0001", "cao-test", "worker-1", False, True, True, True
    )
    assert calls == ["db", "pipe", "fifo", "window"]


@pytest.mark.asyncio
async def test_session_brief_provider_fence_runs_before_window(monkeypatch):
    profile = AgentProfile(
        name="supervisor", description="", sessionBrief="required"
    )
    monkeypatch.setattr(svc, "load_agent_profile", lambda _name: profile)
    backend = Mock()
    monkeypatch.setattr(svc, "get_backend", lambda: backend)
    with pytest.raises(ValueError, match="runtime-context provider"):
        await svc.create_terminal("kiro_cli", "supervisor", new_session=True)
    backend.create_session.assert_not_called()


@pytest.mark.parametrize(
    "provider",
    ["claude_code", "codex", "grok_cli", "kimi_cli", "antigravity_cli"],
)
def test_runtime_provider_matrix_dispatches_launch_context(provider, monkeypatch):
    monkeypatch.setattr(svc.provider_manager, "_providers", {})
    instance = svc.provider_manager.create_provider(
        provider, "term0001", "cao-test", "worker-1", "worker", None,
        skill_prompt="BRIEF-BYTES",
    )
    assert instance._skill_prompt == "BRIEF-BYTES"


def test_absent_field_keeps_generated_settings_literal_bytes(tmp_path):
    raw = "---\nname: worker\ndescription: Worker\n---\nCharter\n"
    provider = ClaudeCodeProvider("term0001", "cao-test", "worker-1", "worker")
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.read_agent_profile_source",
        return_value=raw,
    ):
        path = provider._write_terminal_settings()
    try:
        payload = json.loads(path.read_bytes())
    finally:
        path.unlink(missing_ok=True)
    hooks = payload["hooks"]["SessionStart"][0]["hooks"]
    assert len(hooks) == 1
    assert hooks[0] == {
        "type": "command",
        "command": (
            f"env CAO_API_BASE_URL=http://127.0.0.1:9889 {sys.executable} "
            "-m cli_agent_orchestrator.hooks.transcript_binding"
        ),
        "timeout": 5,
    }


@pytest.mark.asyncio
async def test_required_brief_is_built_after_db_and_fifo_and_before_provider(monkeypatch, tmp_path):
    order = []
    provider_kwargs = {}
    profile = AgentProfile(
        name="supervisor", description="", provider="codex",
        sessionBrief="required", skills=[],
    )
    backend = Mock()
    backend.session_exists.return_value = False
    backend.supports_event_inbox.return_value = False
    backend.create_session.side_effect = lambda *_args, **_kwargs: order.append("window")
    backend.pipe_pane.side_effect = lambda *_args: order.append("fifo")
    provider = AsyncMock()
    provider.initialize.side_effect = lambda: order.append("provider")

    monkeypatch.setattr(svc, "load_agent_profile", lambda _name: profile)
    monkeypatch.setattr(svc, "build_skill_catalog", lambda _filter: "SKILLS")
    monkeypatch.setattr(svc, "generate_terminal_id", lambda: "term0001")
    monkeypatch.setattr(svc, "generate_session_name", lambda: "cao-test")
    monkeypatch.setattr(svc, "generate_window_name", lambda _name: "supervisor-1")
    monkeypatch.setattr(svc, "get_backend", lambda: backend)
    monkeypatch.setattr(svc, "clear_session_env", lambda *_: None)
    monkeypatch.setattr(svc, "db_create_terminal", lambda *_args, **_kwargs: order.append("db"))
    monkeypatch.setattr(svc.fifo_manager, "create_reader", lambda *_: None)
    monkeypatch.setattr(svc, "FIFO_DIR", tmp_path)
    def create_provider(*_args, **kwargs):
        provider_kwargs.update(kwargs)
        return provider

    monkeypatch.setattr(svc.provider_manager, "create_provider", create_provider)
    monkeypatch.setattr(svc, "dispatch_plugin_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(svc, "get_herdr_inbox_service", lambda: None)

    from cli_agent_orchestrator.services import session_manifest_service as manifest_svc

    monkeypatch.setattr(manifest_svc, "list_agent_profiles", lambda: [])
    monkeypatch.setattr(manifest_svc, "list_skills", lambda: [])
    monkeypatch.setattr(manifest_svc, "list_bases", lambda: [])
    monkeypatch.setattr(manifest_svc, "list_workflows", lambda: [])
    monkeypatch.setattr(
        manifest_svc,
        "list_terminals_by_session",
        lambda _name: [{
            "id": "term0001", "agent_profile": "supervisor", "provider": "codex",
            "caller_id": None,
        }] if "db" in order else [],
    )
    monkeypatch.setattr(
        manifest_svc.status_monitor, "get_status", lambda _tid: Mock(value="idle")
    )
    monkeypatch.setattr(svc, "get_working_directory", lambda _tid: str(tmp_path))
    monkeypatch.setattr(
        manifest_svc,
        "render_session_brief",
        lambda manifest: order.append("manifest") or f"terminals={manifest['terminals']}",
    )
    result = await svc.create_terminal("codex", "supervisor", new_session=True)
    assert result.id == "term0001"
    assert order == ["window", "fifo", "db", "manifest", "provider"]
    assert "term0001" in provider_kwargs["skill_prompt"]


def _install_failure_harness(monkeypatch, tmp_path, *, build_effect):
    calls = []
    profile = AgentProfile(
        name="supervisor", description="", provider="codex",
        sessionBrief="required", skills=[],
    )
    backend = Mock()
    backend.session_exists.return_value = False
    backend.supports_event_inbox.return_value = False
    backend.create_session.side_effect = lambda *_args, **_kwargs: calls.append("window")
    backend.pipe_pane.side_effect = lambda *_args: calls.append("fifo")
    backend.stop_pipe_pane.side_effect = lambda *_args: calls.append("stop-pipe")
    backend.kill_session.side_effect = lambda *_args: calls.append("session")
    provider = AsyncMock()
    provider.initialize.return_value = True
    create_provider = Mock(return_value=provider)

    monkeypatch.setattr(svc, "load_agent_profile", lambda _name: profile)
    monkeypatch.setattr(svc, "build_skill_catalog", lambda _filter: "SKILLS")
    monkeypatch.setattr(svc, "generate_terminal_id", lambda: "term0001")
    monkeypatch.setattr(svc, "generate_session_name", lambda: "cao-test")
    monkeypatch.setattr(svc, "generate_window_name", lambda _name: "supervisor-1")
    monkeypatch.setattr(svc, "get_backend", lambda: backend)
    monkeypatch.setattr(svc, "clear_session_env", lambda *_: None)
    monkeypatch.setattr(svc, "db_create_terminal", lambda *_args, **_kwargs: calls.append("db"))
    monkeypatch.setattr(svc, "db_delete_terminal", lambda *_args: calls.append("delete-db"))
    monkeypatch.setattr(svc.fifo_manager, "create_reader", lambda *_: None)
    monkeypatch.setattr(svc.fifo_manager, "stop_reader", lambda *_: calls.append("stop-fifo"))
    monkeypatch.setattr(svc, "FIFO_DIR", tmp_path)
    monkeypatch.setattr(svc.provider_manager, "create_provider", create_provider)
    monkeypatch.setattr(svc.provider_manager, "cleanup_provider", lambda *_: None)
    monkeypatch.setattr(svc.status_monitor, "clear_terminal", lambda *_: None)
    monkeypatch.setattr(svc, "dispatch_plugin_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(svc, "get_herdr_inbox_service", lambda: None)

    from cli_agent_orchestrator.services import session_manifest_service as manifest_svc

    monkeypatch.setattr(manifest_svc, "build_session_manifest", Mock(side_effect=build_effect))
    monkeypatch.setattr(manifest_svc, "render_session_brief", lambda _manifest: "BRIEF")
    return calls, backend, create_provider


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_init", [False, True])
async def test_required_core_failure_rolls_back_sync_and_deferred(
    monkeypatch, tmp_path, defer_init
):
    manifest = {
        "complete": False,
        "sections": {"profiles": "error", "skills": "ok"},
    }
    calls, backend, create_provider = _install_failure_harness(
        monkeypatch, tmp_path, build_effect=lambda *_args: manifest
    )
    with pytest.raises(ValueError, match="core section failed"):
        await svc.create_terminal(
            "codex", "supervisor", new_session=True, defer_init=defer_init
        )
    assert calls == [
        "window", "fifo", "db", "delete-db", "stop-pipe", "stop-fifo", "session"
    ]
    create_provider.assert_not_called()
    assert not list(tmp_path.glob("*prompt*"))
    assert not list(tmp_path.glob("*settings*"))
    backend.kill_session.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["flag", "env"])
async def test_required_failure_escape_hatches_boot_with_loud_marker(
    monkeypatch, tmp_path, escape
):
    _calls, _backend, create_provider = _install_failure_harness(
        monkeypatch, tmp_path, build_effect=RuntimeError("profiles unavailable")
    )
    if escape == "env":
        monkeypatch.setenv("CAO_SESSION_BRIEF_RELAX", "1")
    result = await svc.create_terminal(
        "codex", "supervisor", new_session=True,
        allow_incomplete_brief=escape == "flag",
    )
    assert result.id == "term0001"
    prompt = create_provider.call_args.kwargs["skill_prompt"]
    assert prompt == f"SKILLS\n\n{svc.SESSION_BRIEF_MARKER}"


@pytest.mark.asyncio
async def test_cli_and_mcp_render_the_same_captured_snapshot(monkeypatch):
    from cli_agent_orchestrator.cli.main import cli
    from cli_agent_orchestrator.mcp_server import server
    from cli_agent_orchestrator.services.session_manifest_service import render_session_brief

    manifest = _complete_manifest()
    expected = render_session_brief(manifest)

    cli_response = Mock()
    cli_response.json.return_value = manifest
    cli_response.raise_for_status.return_value = None
    cli_get = Mock(return_value=cli_response)
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.session.requests.get", cli_get
    )
    cli_result = CliRunner().invoke(
        cli, ["session", "manifest", "--session", "cao-test", "--brief"]
    )
    assert cli_result.exit_code == 0
    assert cli_result.stdout == expected + "\n"
    assert cli_get.call_count == 1

    mcp_response = Mock()
    mcp_response.json.return_value = manifest
    mcp_response.raise_for_status.return_value = None
    mcp_get = Mock(return_value=mcp_response)
    monkeypatch.setattr(server.requests, "get", mcp_get)
    fn = server.session_manifest.fn if hasattr(server.session_manifest, "fn") else server.session_manifest
    result = await fn(session_name="cao-test", brief=True)
    assert result == {"success": True, "brief": expected}
    assert mcp_get.call_count == 1
