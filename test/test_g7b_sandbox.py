"""G7b provider-plane, native-home, and mutation acceptances."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator import sandbox_bootstrap as bootstrap
from cli_agent_orchestrator.utils import provider_plane
from cli_agent_orchestrator.utils.provider_auth import classify_auth_refresh_output
from cli_agent_orchestrator.utils.provider_plane import ProviderHome
from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "cli_agent_orchestrator"


def _plane(tmp_path: Path, provider: str = "codex") -> ProviderHome:
    native = tmp_path / "native"
    native.mkdir(parents=True)
    credential_name = "auth.json" if provider == "codex" else ".credentials.json"
    source = native / credential_name
    source.write_text('{"token":"seed"}', encoding="utf-8")
    source.chmod(0o600)
    home = tmp_path / "sandbox" / provider
    return ProviderHome(
        provider,
        "shared-auth-read-only",
        home,
        source,
        home / credential_name,
        "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR",
    )


def _activate_plane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str) -> ProviderHome:
    plane = _plane(tmp_path, provider)
    row = {
        "classification": plane.classification,
        "home": str(plane.home),
        "home_env": plane.home_env,
        "credential_source": str(plane.credential_source),
        "credential_path": str(plane.credential_path),
    }
    manifest = {"providers": {provider: row}}
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")
    monkeypatch.setenv(str(plane.home_env), str(plane.home))
    monkeypatch.setattr(bootstrap, "validate_active_sandbox", lambda: manifest)
    return plane


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
@pytest.mark.asyncio
async def test_supported_planes_admit_through_real_public_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.services import session_service, terminal_service

    plane = _activate_plane(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(app.state, "plugin_registry", object(), raising=False)

    def admitted(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("admitted_before_side_effect")

    monkeypatch.setattr(terminal_service, "get_provider_class", admitted)
    monkeypatch.setattr(session_service, "create_terminal", admitted)
    with pytest.raises(RuntimeError, match="admitted_before_side_effect"):
        await terminal_service.create_terminal(provider, "developer")
    with pytest.raises(RuntimeError, match="admitted_before_side_effect"):
        await session_service.create_session(provider, "developer")
    with pytest.raises(RuntimeError, match="admitted_before_side_effect"):
        await session_service.start_session(provider=provider, agent_profile="developer")

    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    response = client.post(
        "/sessions",
        params={"agent_profile": "developer", "provider": provider},
        headers={"X-CAO-Instance": "deadbeef"},
    )
    assert response.status_code == 500
    assert "admitted_before_side_effect" in response.json()["detail"]
    assert plane.credential_path is not None
    assert json.loads(plane.credential_path.read_text(encoding="utf-8")) == {"token": "seed"}
    assert stat.S_IMODE(plane.credential_path.stat().st_mode) == 0o600


def test_concurrent_seed_reads_once_and_never_clobbers_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = _plane(tmp_path)
    source = plane.credential_source
    assert source is not None
    real_open = provider_plane.os.open
    reads: list[Path] = []
    reads_lock = threading.Lock()

    def counted_open(path: os.PathLike[str] | str, flags: int, *args: Any) -> int:
        if Path(path) == source and flags == os.O_RDONLY:
            with reads_lock:
                reads.append(Path(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(provider_plane.os, "open", counted_open)
    errors: list[BaseException] = []

    def seed() -> None:
        try:
            provider_plane.seed_provider_credential(plane)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=seed) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    assert len(reads) == 1
    assert plane.credential_path is not None
    plane.credential_path.write_text('{"token":"refreshed"}', encoding="utf-8")
    source.write_text('{"token":"production-new"}', encoding="utf-8")
    provider_plane.seed_provider_credential(plane)
    assert len(reads) == 1
    assert json.loads(plane.credential_path.read_text(encoding="utf-8")) == {"token": "refreshed"}


def test_dead_initializer_partial_is_removed_and_reseeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = _plane(tmp_path)
    plane.home.mkdir(parents=True)
    destination = plane.credential_path
    assert destination is not None
    partial = plane.home / f".{destination.name}.killed.tmp"
    partial.write_text("partial", encoding="utf-8")
    lock = destination.with_name(f".{destination.name}.init.lock")
    lock.write_text(
        json.dumps({"pid": 99999999, "process_start_time": 1, "temp_name": partial.name}),
        encoding="utf-8",
    )
    provider_plane._record_attempt(plane.home, "killed", "initial_seed", "started")
    provider_plane.seed_provider_credential(plane, deadline_s=0)
    assert not partial.exists()
    assert json.loads(destination.read_text(encoding="utf-8")) == {"token": "seed"}
    records = [
        json.loads(line)
        for line in (plane.home / "seed-attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["reason"] == "dead_owner_recovery" for row in records)


def test_native_home_guard_and_every_roster_consumer_is_injected() -> None:
    allowed = {"sandbox_bootstrap.py", "utils/provider_plane.py"}
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert '"/.codex/sessions/"' not in text
        if 'Path.home() / ".codex"' not in text and 'Path.home() / ".claude"' not in text:
            continue
        assert path.relative_to(SOURCE).as_posix() in allowed

    roster = {
        "providers/codex.py",
        "providers/claude_code.py",
        "services/fork_context_service.py",
        "services/epoch_recovery_service.py",
        "services/message_trace_service.py",
        "api/main.py",
    }
    for relative in roster:
        assert "provider_home(" in (SOURCE / relative).read_text(encoding="utf-8")


def test_every_native_home_consumer_reads_the_injected_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.providers import codex
    from cli_agent_orchestrator.services import (
        epoch_recovery_service,
        fork_context_service,
        message_trace_service,
    )

    codex_plane = _plane(tmp_path / "codex-case")
    codex_plane.sessions.mkdir(parents=True)
    session_id = "11111111-1111-1111-1111-111111111111"
    rollout = codex_plane.sessions / f"rollout-{session_id}.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": "/work"}}) + "\n",
        encoding="utf-8",
    )
    for module in (codex, fork_context_service, epoch_recovery_service, message_trace_service):
        monkeypatch.setattr(module, "provider_home", lambda _provider, p=codex_plane: p)

    provider = codex.CodexProvider("tid", "session", "window")
    provider.validate_session_artifact(session_id, "/work")
    assert provider.auth_state_path() == codex_plane.home / "auth.json"
    assert (
        fork_context_service.validate_base_source(
            mode="compatibility", provider="codex", session_uuid=session_id, cwd="/work"
        )
        == {}
    )
    assert epoch_recovery_service._artifact_exists(
        {"provider": "codex", "session_uuid": session_id}
    )
    assert (
        message_trace_service.resolve_session_transcript(
            {"id": "tid", "provider": "codex", "provider_session_id": session_id}
        )
        == rollout
    )

    claude_plane = _plane(tmp_path / "claude-case", "claude_code")
    transcript = claude_plane.projects / "repo" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    monkeypatch.setattr(message_trace_service, "provider_home", lambda _provider: claude_plane)
    monkeypatch.setattr(main, "provider_home", lambda _provider: claude_plane)
    assert (
        message_trace_service.resolve_session_transcript(
            {
                "id": "tid",
                "provider": "claude_code",
                "provider_session_id": "session",
                "working_directory": "/repo",
            }
        )
        == transcript
    )
    monkeypatch.setattr(main, "get_terminal_metadata", lambda _terminal_id: {"id": "abcd1234"})
    monkeypatch.setattr(main, "create_transcript_binding", lambda *args: {"id": 1})
    response = TestClient(main.app, base_url="http://localhost").post(
        "/terminals/abcd1234/transcript-binding",
        json={
            "terminal_id": "abcd1234",
            "session_id": "session",
            "transcript_path": str(transcript),
            "source": "startup",
        },
    )
    assert response.status_code == 200, response.text


def test_version_pinned_auth_refresh_fixture_classifier() -> None:
    fixtures = REPO / "test" / "fixtures" / "g7b"
    claude = (fixtures / "claude-2.1.211-auth-refresh.txt").read_text(encoding="utf-8")
    codex = (fixtures / "codex-0.144.4-auth-refresh.txt").read_text(encoding="utf-8")
    for expected in (
        "interactive_login_required",
        "access_token_acquisition_failed",
        "credential_write_failed",
        "transient_network_failure",
    ):
        assert expected in {
            classify_auth_refresh_output("claude_code", line) for line in claude.splitlines()
        }
    assert classify_auth_refresh_output("codex", codex) == "auth_changed_skip"


def test_codex_seed_resume_uses_injected_artifact_identity() -> None:
    from cli_agent_orchestrator.models.terminal import ForkContext
    from cli_agent_orchestrator.providers.codex import CodexProvider

    context = ForkContext(
        mode="resume",
        session_uuid="seed-session",
        base_name="seed",
        provider="codex",
        initial_preamble="",
    )
    provider = CodexProvider("tid", "session", "window", fork_context=context)
    assert provider.resume_session_uuid() == "seed-session"


def test_claude_launch_preserves_injected_config_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.providers import claude_code

    provider = claude_code.ClaudeCodeProvider("tid", "session", "window", "missing")
    monkeypatch.setattr(provider, "_write_terminal_settings", lambda: Path("/tmp/settings"))
    command = provider._build_claude_command()
    assert "CLAUDE_CONFIG_DIR'" in command


def test_claude_isolated_home_gets_nonsecret_onboarding_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.providers import claude_code

    plane = _plane(tmp_path, "claude_code")
    plane.home.mkdir(parents=True)
    monkeypatch.setattr(claude_code, "provider_home", lambda _provider: plane)
    claude_code.ClaudeCodeProvider._ensure_sandbox_onboarding_state()
    state = json.loads((plane.home / ".claude.json").read_text(encoding="utf-8"))
    assert state == {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "bypassPermissionsModeAccepted": True,
    }
    assert stat.S_IMODE((plane.home / ".claude.json").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_claude_failed_refresh_maps_named_error_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.providers import claude_code

    provider = claude_code.ClaudeCodeProvider("tid", "session", "window", "developer")
    monkeypatch.setattr(
        claude_code, "wait_for_shell", lambda *a, **k: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(
        claude_code, "wait_until_status", lambda *a, **k: asyncio.sleep(0, result=False)
    )
    monkeypatch.setattr(provider, "_ensure_skip_bypass_prompt_setting", lambda: None)
    monkeypatch.setattr(provider, "_build_claude_command", lambda: "claude")
    monkeypatch.setattr(provider, "_handle_startup_prompts", lambda: None)

    class Backend:
        def send_keys(self, *args: Any) -> None:
            pass

        def get_history(self, *args: Any) -> str:
            return "Failed to save OAuth tokens"

    monkeypatch.setattr(claude_code, "get_backend", lambda: Backend())
    with pytest.raises(
        claude_code.ProviderError,
        match="provider_auth_refresh_failed:credential_write_failed",
    ):
        await provider.initialize()


def test_provider_pane_production_home_canary_mutation_is_fatal(tmp_path: Path) -> None:
    production_home = tmp_path / ".codex"
    production_home.mkdir()
    canary = production_home / "g7b-canary"
    canary.write_text("canary", encoding="utf-8")
    recorded_seed = production_home / "auth.json"
    script = """
import json
import os
from pathlib import Path

def hook(event, args):
    if event == "open" and args and str(args[0]) == os.environ["G7B_CANARY"]:
        print(json.dumps({"terminal": os.environ["CAO_TERMINAL_ID"], "path": str(args[0])}))
sys_audit = __import__("sys").addaudithook
sys_audit(hook)
Path(os.environ["G7B_CANARY"]).read_text(encoding="utf-8")
"""
    environment = {
        **os.environ,
        "CAO_TERMINAL_ID": "mutant-pane",
        "G7B_CANARY": str(canary),
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    accesses = [Path(json.loads(line)["path"]) for line in result.stdout.splitlines()]
    accesses.append(recorded_seed)
    allowed = {recorded_seed}
    violations = [
        path for path in accesses if path.is_relative_to(production_home) and path not in allowed
    ]
    assert violations == [canary]


def test_emitted_plane_env_and_cross_instance_affinity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.utils.sandbox_guard import bind_pane_identity

    codex = _activate_plane(monkeypatch, tmp_path, "codex")
    claude_home = tmp_path / "sandbox" / "claude_code"
    monkeypatch.setattr(
        provider_plane,
        "provider_plane_environment",
        lambda: {
            "CODEX_HOME": str(codex.home),
            "CLAUDE_CONFIG_DIR": str(claude_home),
        },
    )
    emitted = bind_pane_identity({}, "cafebabe")
    assert emitted == {
        "CAO_TERMINAL_ID": "cafebabe",
        "CAO_INSTANCE_ID": "deadbeef",
        "CAO_ENDPOINT": "http://127.0.0.1:19876",
        "CODEX_HOME": str(codex.home),
        "CLAUDE_CONFIG_DIR": str(claude_home),
    }
    assert (
        TestClient(app)
        .post(
            "/sessions",
            params={"agent_profile": "developer", "provider": "codex"},
            headers={"X-CAO-Instance": "foreign00"},
        )
        .status_code
        == 409
    )


def test_tmux_allows_only_manifest_pinned_blocked_plane_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setattr(
        provider_plane,
        "provider_plane_environment",
        lambda: {
            "CODEX_HOME": "/sandbox/codex",
            "CLAUDE_CONFIG_DIR": "/sandbox/claude",
        },
    )
    environment: dict[str, str] = {}
    TmuxClient._merge_extra_env(
        environment,
        {
            "CODEX_HOME": "/sandbox/codex",
            "CLAUDE_CONFIG_DIR": "/sandbox/claude",
        },
    )
    assert environment == {
        "CODEX_HOME": "/sandbox/codex",
        "CLAUDE_CONFIG_DIR": "/sandbox/claude",
    }

    TmuxClient._merge_extra_env(environment, {"CODEX_HOME": "/production/.codex"})
    assert environment["CODEX_HOME"] == "/sandbox/codex"
