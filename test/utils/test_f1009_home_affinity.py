"""F1009: the server, pane, and MCP child must use the same CAO store."""

import json
import os
import subprocess
import sys

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.utils.sandbox_guard import (
    bind_mcp_server_identity,
    bind_pane_identity,
)


@pytest.fixture
def server_home(tmp_path, monkeypatch):
    home = tmp_path / "server-store"
    store = home / "agent-store"
    store.mkdir(parents=True)
    (store / "routing.toml").write_text('provider = "claude_code"\n')
    monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    return home


@pytest.mark.parametrize("boundary", ["pane", "mcp"])
def test_home_is_pinned_and_conflicting_overrides_are_rejected(server_home, boundary):
    def bind(env):
        if boundary == "pane":
            return bind_pane_identity(env, "cafebabe")
        return bind_mcp_server_identity({"command": "cao-mcp-server", "env": env}, "cafebabe")[
            "env"
        ]

    assert bind({})["CAO_HOME_DIR"] == str(server_home)
    assert bind({"CAO_HOME_DIR": str(server_home)})["CAO_HOME_DIR"] == str(server_home)
    with pytest.raises(ValueError, match="may not override CAO_HOME_DIR"):
        bind({"CAO_HOME_DIR": str(server_home / "other")})


@pytest.mark.parametrize("carrier", ["tmux", "herdr", "mcp"])
def test_child_reads_server_routing_store(server_home, tmp_path, carrier):
    if carrier == "mcp":
        forwarded = bind_mcp_server_identity({"command": "cao-mcp-server"}, "cafebabe")["env"]
    else:
        pane_env = bind_pane_identity({}, "cafebabe")
        if carrier == "tmux":
            forwarded = {}
            TmuxClient._merge_extra_env(forwarded, pane_env)
        else:
            args = object.__new__(HerdrBackend)._build_env_args("cafebabe", "cao-test", pane_env)
            forwarded = dict(arg.split("=", 1) for arg in args[1::2])

    # Model a carrier with stale environment from a different CAO installation.
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("CAO_")}
    child_env["CAO_HOME_DIR"] = str(tmp_path / "stale-carrier-store")
    child_env.update(forwarded)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from cli_agent_orchestrator.constants import CAO_HOME_DIR, routing_toml_path; "
                "print(json.dumps([str(CAO_HOME_DIR), routing_toml_path().read_text()]))"
            ),
        ],
        env=child_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == [str(server_home), 'provider = "claude_code"\n']


def test_remote_mcp_config_is_unchanged(server_home):
    config = {"url": "https://example.test/mcp"}
    assert bind_mcp_server_identity(config, "cafebabe") == config


def test_relative_home_is_resolved_before_child_changes_directory(server_home, monkeypatch):
    monkeypatch.chdir(server_home.parent)
    monkeypatch.setenv("CAO_HOME_DIR", server_home.name)
    assert bind_pane_identity({}, "cafebabe")["CAO_HOME_DIR"] == str(server_home)


def test_default_home_is_explicit(monkeypatch):
    monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)
    monkeypatch.delenv("CAO_HOME_DIR", raising=False)
    expected = str(constants.local_agent_store_dir().parent.resolve())
    assert bind_pane_identity({}, "cafebabe")["CAO_HOME_DIR"] == expected
