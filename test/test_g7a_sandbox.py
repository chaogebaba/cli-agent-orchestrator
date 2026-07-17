"""G7a closed-world namespace, lifecycle, and regression guards."""

from __future__ import annotations

import argparse
import ast
import copy
import importlib
import io
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator import sandbox_bootstrap as bootstrap
from cli_agent_orchestrator.utils.http import (
    CAOHttpClient,
    EndpointConfigurationError,
    resolve_endpoint,
)
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "cli_agent_orchestrator"
PYTHON = REPO / ".venv" / "bin" / "python"


def _python_files() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def _raw_tmux_calls(tree: ast.AST) -> list[ast.Call]:
    violations: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
            and function.attr in {"run", "Popen", "call", "check_call", "check_output"}
        ):
            continue
        argument = node.args[0]
        if (
            isinstance(argument, (ast.List, ast.Tuple))
            and argument.elts
            and isinstance(argument.elts[0], ast.Constant)
            and argument.elts[0].value == "tmux"
        ):
            violations.append(node)
    return violations


def test_endpoint_ast_guard_is_closed() -> None:
    request_methods = {"get", "post", "put", "patch", "delete", "request"}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(REPO).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(alias.name == "API_BASE_URL" for alias in node.names), relative
            if isinstance(node, ast.Name):
                if node.id == "API_BASE_URL":
                    assert relative.endswith("constants.py") and isinstance(
                        node.ctx, ast.Store
                    ), relative
            if isinstance(node, ast.Constant) and node.value == 9889:
                assert relative.endswith("utils/http.py"), relative
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "requests":
                continue
            if node.func.attr not in request_methods or relative.endswith("utils/http.py"):
                continue
            # The installer fetches a user-selected remote profile, not the CAO API.
            assert relative.endswith("services/install_service.py"), relative


def test_tmux_ast_guard_is_closed() -> None:
    allowed = {"utils/tmux_command.py", "sandbox_bootstrap.py"}
    for path in _python_files():
        relative = path.relative_to(SOURCE).as_posix()
        violations = _raw_tmux_calls(ast.parse(path.read_text(encoding="utf-8")))
        if relative in allowed:
            continue
        assert not violations, f"raw tmux execution in {relative}"


@pytest.mark.parametrize(
    ("relative", "command"),
    [
        ("clients/tmux.py", "load-buffer"),
        ("clients/tmux.py", "paste-buffer"),
        ("clients/tmux.py", "send-keys"),
        ("clients/tmux.py", "delete-buffer"),
        ("backends/tmux_backend.py", "list-windows"),
        ("backends/tmux_backend.py", "attach-session"),
        ("services/fork_context_service.py", "display-message"),
        ("cli/commands/info.py", "display-message"),
        ("api/main.py", "-u"),
    ],
)
def test_each_legacy_tmux_site_mutation_is_killed(relative: str, command: str) -> None:
    path = SOURCE / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    class RawTmuxMutation(ast.NodeTransformer):
        mutations = 0

        def visit_Call(self, node: ast.Call) -> ast.AST:
            node = self.generic_visit(node)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "tmux_argv"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == command
            ):
                self.mutations += 1
                return ast.copy_location(
                    ast.List(elts=[ast.Constant("tmux"), *node.args], ctx=ast.Load()), node
                )
            return node

    mutation = RawTmuxMutation()
    mutated = mutation.visit(tree)
    assert mutation.mutations == 1, f"site not uniquely found: {relative}:{command}"
    assert len(_raw_tmux_calls(mutated)) == 1, f"guard missed {relative}:{command}"


def test_bootstrap_top_level_imports_are_stdlib_only(tmp_path: Path) -> None:
    path = SOURCE / "sandbox_bootstrap.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules = [alias.name.partition(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [(node.module or "").partition(".")[0]]
        else:
            continue
        assert all(
            module == "__future__" or module in sys.stdlib_module_names for module in modules
        )

    home = tmp_path / "empty-home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [str(PYTHON), "-B", "-c", "import cli_agent_orchestrator.sandbox_bootstrap"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not list(home.iterdir())


def test_sandbox_endpoint_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.delenv("CAO_ENDPOINT", raising=False)
    with pytest.raises(EndpointConfigurationError):
        resolve_endpoint()


def test_mcp_identity_forced_and_overrides_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")
    resolved = resolve_mcp_server_config(
        {"command": "cao-mcp-server", "env": {}}, terminal_id="cafebabe"
    )
    assert resolved["command"] == sys.executable
    assert resolved["args"][:2] == ["-m", "cli_agent_orchestrator.mcp_server.server"]
    assert resolved["env"] == {
        "CAO_TERMINAL_ID": "cafebabe",
        "CAO_INSTANCE_ID": "deadbeef",
        "CAO_ENDPOINT": "http://127.0.0.1:19876",
    }
    for key in ("CAO_TERMINAL_ID", "CAO_INSTANCE_ID", "CAO_ENDPOINT"):
        with pytest.raises(ValueError):
            resolve_mcp_server_config(
                {"command": "cao-mcp-server", "env": {key: "wrong"}},
                terminal_id="cafebabe",
            )


@pytest.mark.asyncio
async def test_all_ten_providers_fail_through_public_create_and_api_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.services import session_service, terminal_service

    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")

    def bomb(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("pre-admission side effect reached")

    monkeypatch.setattr(session_service, "create_terminal", bomb)
    monkeypatch.setattr(terminal_service, "seed_resume_bootstrap", bomb)
    monkeypatch.setattr(terminal_service, "get_provider_class", bomb)
    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    for provider in (
        name for name in bootstrap.PROVIDERS if name not in bootstrap.SHARED_AUTH_PROVIDERS
    ):
        with pytest.raises(SandboxProviderUnsafe, match=f"sandbox_provider_unsafe:{provider}"):
            await terminal_service.create_terminal(provider, "developer")
        with pytest.raises(SandboxProviderUnsafe, match=f"sandbox_provider_unsafe:{provider}"):
            await session_service.create_session(provider, "developer")
        with pytest.raises(SandboxProviderUnsafe, match=f"sandbox_provider_unsafe:{provider}"):
            await session_service.start_session(provider=provider, agent_profile="developer")
        response = client.post(
            "/sessions",
            params={"agent_profile": "developer", "provider": provider},
            headers={"X-CAO-Instance": "deadbeef"},
        )
        assert response.status_code == 500
        assert f"sandbox_provider_unsafe:{provider}" in response.json()["detail"]
        assert (
            client.post(
                "/sessions/start",
                params={"agent_profile": "developer", "provider": provider},
                headers={"X-CAO-Instance": "deadbeef"},
            ).status_code
            == 500
        )
        terminal_response = client.post(
            "/sessions/cao-test/terminals",
            params={"agent_profile": "developer", "provider": provider},
            headers={"X-CAO-Instance": "deadbeef"},
        )
        assert terminal_response.status_code == 500
        assert f"sandbox_provider_unsafe:{provider}" in terminal_response.json()["detail"]


class _CaptureResponse:
    status_code = 200

    def __init__(self, payload: Any = None) -> None:
        self._payload = {} if payload is None else payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _CaptureTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def _capture(self, url: str) -> _CaptureResponse:
        self.urls.append(url)
        if url.endswith("/terminals/cafebabe"):
            return _CaptureResponse({"session_name": "cao-test"})
        if url.endswith("/sessions"):
            return _CaptureResponse([])
        return _CaptureResponse({})

    def get(self, url: str, **kwargs: Any) -> _CaptureResponse:
        return self._capture(url)

    def post(self, url: str, **kwargs: Any) -> _CaptureResponse:
        return self._capture(url)

    def request(self, method: str, url: str, **kwargs: Any) -> _CaptureResponse:
        return self._capture(url)


@pytest.mark.parametrize(
    ("module_name", "event"),
    [
        (
            "cli_agent_orchestrator.hooks.transcript_binding",
            {"session_id": "session", "transcript_path": "/tmp/transcript", "cwd": "/tmp"},
        ),
        ("cli_agent_orchestrator.hooks.session_brief", {"source": "compact"}),
    ],
)
def test_both_hook_endpoint_canary_mutations(
    module_name: str,
    event: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module(module_name)
    transport = _CaptureTransport()
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9889")
    monkeypatch.setenv("CAO_TERMINAL_ID", "cafebabe")
    monkeypatch.setattr(module, "cao_http", CAOHttpClient(lambda: transport))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert module.main() == 0
    assert transport.urls
    assert all(url.startswith("http://127.0.0.1:19876/") for url in transport.urls)

    transport.urls.clear()
    monkeypatch.delenv("CAO_ENDPOINT")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert module.main() == 0
    assert transport.urls == []


def test_endpoint_canaries_for_overlooked_family_csp_and_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.cli.commands import session as session_command
    from cli_agent_orchestrator.ext_apps import apps
    from cli_agent_orchestrator.utils import terminal

    transport = _CaptureTransport()
    client = CAOHttpClient(lambda: transport)
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9889")
    monkeypatch.setattr(terminal, "cao_http", client)
    monkeypatch.setattr(session_command, "cao_http", client)

    terminal.sync_backend_from_server()
    assert session_command._get_sessions() == []
    assert apps.default_csp()["connectDomains"] == ["http://127.0.0.1:19876"]
    assert transport.urls == [
        "http://127.0.0.1:19876/health",
        "http://127.0.0.1:19876/sessions",
    ]


def test_http_client_rejects_sandbox_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CaptureTransport()
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")
    with pytest.raises(EndpointConfigurationError, match="must match"):
        CAOHttpClient(lambda: transport).get("/health", base_url="http://127.0.0.1:19877")
    assert transport.urls == []


def _manifest(tmp_path: Path) -> tuple[dict, Path]:
    root = tmp_path / "sandbox"
    manifest = bootstrap._build_manifest(root, 19876)
    return manifest, root / bootstrap.MANIFEST_NAME


def test_foreign_tmux_socket_collision_is_neither_adopted_nor_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_id = uuid.uuid4().hex[:8]
    socket_name = f"cao-sbx-{instance_id}"
    monkeypatch.setattr(
        bootstrap.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=f"{instance_id}{'0' * 24}"),
    )
    subprocess.run(
        ["tmux", "-L", socket_name, "new-session", "-d", "-s", "foreign"],
        check=True,
        capture_output=True,
        text=True,
    )
    root = tmp_path / "collision"
    try:
        with pytest.raises(bootstrap.SandboxError, match="already live"):
            bootstrap.command_up(argparse.Namespace(root=str(root), port=_free_port()))
        sessions = subprocess.run(
            ["tmux", "-L", socket_name, "list-sessions", "-F", "#{session_name}"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert sessions.stdout.splitlines() == ["foreign"]
        assert not root.exists()
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_path", str(bootstrap.PRODUCTION_ROOT / "db" / "cli-agent-orchestrator.db")),
        ("endpoint", "http://127.0.0.1:9889"),
        ("tmux_socket", "default"),
        ("settings_path", "/tmp/outside-settings.json"),
        ("providers_path", str(Path.home() / ".codex" / "providers.toml")),
    ],
)
def test_manifest_tamper_matrix(tmp_path: Path, field: str, value: str) -> None:
    manifest, manifest_path = _manifest(tmp_path)
    tampered = copy.deepcopy(manifest)
    tampered[field] = value
    with pytest.raises(bootstrap.SandboxError):
        bootstrap.validate_manifest(tampered, manifest_path)


def test_manifest_rejects_hardlink_symlink_and_inode_swap(tmp_path: Path) -> None:
    manifest, manifest_path = _manifest(tmp_path)
    settings = Path(manifest["settings_path"])
    settings.write_text("{}", encoding="utf-8")
    os.link(settings, settings.with_name("settings-alias.json"))
    with pytest.raises(bootstrap.SandboxError, match="hard-linked"):
        bootstrap.validate_manifest(manifest, manifest_path)

    settings.with_name("settings-alias.json").unlink()
    settings.unlink()
    root = Path(manifest["root"])
    moved = root.with_name("moved")
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(bootstrap.SandboxError):
        bootstrap.validate_manifest(manifest, manifest_path)

    root.unlink()
    root.mkdir()
    with pytest.raises(bootstrap.SandboxError, match="inode changed"):
        bootstrap.validate_manifest(manifest, root / bootstrap.MANIFEST_NAME)


def test_child_fence_reopens_manifest_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, manifest_path = _manifest(tmp_path)
    bootstrap._write_once(manifest_path, bootstrap.render_manifest(manifest), 0o400)
    for key, value in bootstrap._manifest_env(manifest, manifest_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19877")
    reached: list[str] = []

    def bomb(name: str):
        def fail(*args: Any, **kwargs: Any) -> Any:
            reached.append(name)
            pytest.fail(f"child sink reached before revalidation: {name}")

        return fail

    monkeypatch.setattr(bootstrap, "assert_sandbox_db_fence", bomb("database"))
    monkeypatch.setattr(bootstrap, "source_identity", bomb("source subprocess"))
    with pytest.raises(bootstrap.SandboxError, match="environment mismatch"):
        bootstrap.command_serve(argparse.Namespace(manifest=str(manifest_path)))
    assert reached == []


def _cache_snapshot() -> dict[str, tuple[int, int, int]]:
    result: dict[str, tuple[int, int, int]] = {}
    for base in (REPO / "src", REPO / ".venv"):
        for path in base.rglob("*"):
            if path.is_file() and (path.suffix == ".pyc" or "__pycache__" in path.parts):
                stat = path.stat()
                result[str(path)] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return result


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_empty_cache_invalid_and_valid_lifecycle_audit(tmp_path: Path) -> None:
    before = _cache_snapshot()
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "CAO_HOME",
            "CAO_ENDPOINT",
            "CAO_INSTANCE_ID",
            "CAO_TMUX_SOCKET",
            "CAO_TMP_DIR",
            "CAO_GRAPH_EXPORT_ROOT",
            "CAO_SANDBOX_MANIFEST",
        }
    }
    invalid_root = tmp_path / "already-there"
    invalid_root.mkdir()
    invalid = subprocess.run(
        [
            str(PYTHON),
            "-B",
            "-m",
            "cli_agent_orchestrator.sandbox_bootstrap",
            "up",
            "--root",
            str(invalid_root),
            "--port",
            str(_free_port()),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 2

    root = tmp_path / "valid"
    port = _free_port()
    up = subprocess.run(
        [
            str(PYTHON),
            "-B",
            "-m",
            "cli_agent_orchestrator.sandbox_bootstrap",
            "up",
            "--root",
            str(root),
            "--port",
            str(port),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert up.returncode == 0, up.stderr
    down = subprocess.run(
        [
            str(PYTHON),
            "-B",
            "-m",
            "cli_agent_orchestrator.sandbox_bootstrap",
            "down",
            "--root",
            str(root),
            "--purge",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert down.returncode == 0, down.stderr
    assert _cache_snapshot() == before


def test_real_down_purge_rejects_symlink_swapped_root(tmp_path: Path) -> None:
    root = tmp_path / "purge"
    moved = tmp_path / "purge-moved"
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "CAO_ENDPOINT",
            "CAO_INSTANCE_ID",
            "CAO_TMUX_SOCKET",
            "CAO_TMP_DIR",
            "CAO_GRAPH_EXPORT_ROOT",
            "CAO_SANDBOX_MANIFEST",
        }
    }
    up = subprocess.run(
        [
            str(PYTHON),
            "-B",
            "-m",
            "cli_agent_orchestrator.sandbox_bootstrap",
            "up",
            "--root",
            str(root),
            "--port",
            str(_free_port()),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert up.returncode == 0, up.stderr
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    try:
        rejected = subprocess.run(
            [
                str(PYTHON),
                "-B",
                "-m",
                "cli_agent_orchestrator.sandbox_bootstrap",
                "down",
                "--root",
                str(root),
                "--purge",
            ],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert rejected.returncode == 2
        assert "symlink component forbidden" in rejected.stderr
    finally:
        root.unlink(missing_ok=True)
        if moved.exists():
            moved.rename(root)
    down = subprocess.run(
        [
            str(PYTHON),
            "-B",
            "-m",
            "cli_agent_orchestrator.sandbox_bootstrap",
            "down",
            "--root",
            str(root),
            "--purge",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert down.returncode == 0, down.stderr
    assert not root.exists()


def test_production_home_default_is_byte_identical() -> None:
    env = dict(os.environ)
    env.pop("CAO_HOME", None)
    result = subprocess.run(
        [
            str(PYTHON),
            "-B",
            "-c",
            "from cli_agent_orchestrator.constants import CAO_HOME_DIR; print(CAO_HOME_DIR)",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(Path.home() / ".aws" / "cli-agent-orchestrator")


@pytest.mark.asyncio
async def test_wrong_venv_same_base_identity_fails_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.api import main as api_main

    manifest, _ = _manifest(tmp_path)
    wrong_source = copy.deepcopy(manifest["source"])
    wrong_source["interpreter_identity"]["venv_prefix"] = str(REPO / "wrong-venv")
    monkeypatch.setenv("CAO_INSTANCE_ID", str(manifest["instance_id"]))
    monkeypatch.setattr(bootstrap, "validate_active_sandbox", lambda: manifest)
    monkeypatch.setattr(bootstrap, "source_identity", lambda fork_root: wrong_source)
    monkeypatch.setattr(api_main, "get_backend", lambda: object())
    payload = await api_main.health_check()
    assert (
        payload["source"]["interpreter_identity"]["base_interpreter_realpath"]
        == manifest["source"]["interpreter_identity"]["base_interpreter_realpath"]
    )
    assert payload["source"]["interpreter_match"] is False


def test_cao_home_namespaces_all_core_paths(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    env = {**os.environ, "CAO_HOME": str(root), "CAO_GRAPH_EXPORT_ROOT": str(root / "graph")}
    code = """
from cli_agent_orchestrator import constants as c
paths = [c.CAO_ENV_FILE, c.DB_DIR, c.LOG_DIR, c.TERMINAL_LOG_DIR, c.DRAFT_LOG_DIR,
         c.FIFO_DIR, c.AGENT_CONTEXT_DIR, c.LOCAL_AGENT_STORE_DIR, c.SKILLS_DIR,
         c.DATABASE_FILE, c.MEMORY_BASE_DIR, c.WORKFLOW_SPEC_DIR,
         c.WORKFLOW_SCRIPT_SCRATCH_DIR, c.DEFERRED_DEADLETTER_DIR,
         c.graph_export_root()]
print(chr(10).join(map(str, paths)))
"""
    result = subprocess.run(
        [str(PYTHON), "-B", "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()
    assert all(Path(value).is_relative_to(root) for value in result.stdout.splitlines())


def test_each_local_mutation_command_is_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19876")
    monkeypatch.setenv("CAO_HOME", str(tmp_path / "home"))

    redeploy_module = importlib.import_module("cli_agent_orchestrator.cli.commands.redeploy")
    install_module = importlib.import_module("cli_agent_orchestrator.cli.commands.install")
    config_module = importlib.import_module("cli_agent_orchestrator.cli.commands.config")
    env_module = importlib.import_module("cli_agent_orchestrator.cli.commands.env")
    skills_module = importlib.import_module("cli_agent_orchestrator.cli.commands.skills")

    def bomb(*args, **kwargs):
        pytest.fail("mutation sink reached")

    monkeypatch.setattr(redeploy_module, "_install_redeploy", bomb)
    monkeypatch.setattr(redeploy_module, "_restart_server", bomb)
    monkeypatch.setattr(install_module, "_copy_local_profile_to_store", bomb)
    monkeypatch.setattr(install_module, "install_agent", bomb)
    monkeypatch.setattr(config_module.ConfigService, "set", bomb)
    monkeypatch.setattr(env_module, "set_env_var", bomb)
    monkeypatch.setattr(env_module, "unset_env_var", bomb)
    monkeypatch.setattr(skills_module, "_install_skill_folder", bomb)

    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    skill = tmp_path / "skill"
    skill.mkdir()
    from cli_agent_orchestrator.cli.main import cli

    commands = [
        ["redeploy", "--yes"],
        ["install", "developer"],
        ["config", "set", "memory.enabled", "true"],
        ["env", "set", "SAFE_KEY", "value"],
        ["env", "unset", "SAFE_KEY"],
        ["profile", "create", "-t", "x", "-c", str(config), "-o", str(tmp_path)],
        ["profile", "remove", "x", "--yes"],
        ["skills", "add", str(skill)],
        ["skills", "remove", "x"],
    ]
    runner = CliRunner()
    for command in commands:
        result = runner.invoke(cli, command)
        assert result.exit_code != 0, command
        assert "sandbox mutation forbidden" in str(result.exception), command
    assert not (tmp_path / "x.md").exists()


def test_server_affinity_and_setter_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    monkeypatch.setenv("CAO_INSTANCE_ID", "deadbeef")
    client = TestClient(app)
    assert client.post("/settings/agent-dirs", json={}).status_code == 409
    assert (
        client.post(
            "/settings/agent-dirs",
            json={},
            headers={"X-CAO-Instance": "deadbeef"},
        ).status_code
        == 403
    )
    monkeypatch.delenv("CAO_INSTANCE_ID")
    assert (
        client.post(
            "/settings/agent-dirs",
            json={},
            headers={"X-CAO-Instance": "deadbeef"},
        ).status_code
        == 409
    )
