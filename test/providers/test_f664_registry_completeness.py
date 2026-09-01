"""F664 (#519): omp and mcode are registered, so the five fatal call sites resolve.

``ProviderType`` advertised 14 providers while ``PROVIDER_CLASSES`` held 12; the
spawn path resolves through ``get_provider_class``, so ``omp``/``mcode`` died with
``ValueError: Unknown provider type`` and their ``construct_provider`` branches
were unreachable. Each arm below drives the expression a real call site evaluates,
so removing a registry row turns that arm red rather than a text assertion.
"""

import json
import os

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.manager import ProviderManager, get_provider_class
from cli_agent_orchestrator.providers.minimax_code import MiniMaxCodeProvider
from cli_agent_orchestrator.providers.omp import OmpProvider

NEWLY_REGISTERED = [
    pytest.param(ProviderType.OMP.value, OmpProvider, id="omp"),
    pytest.param(ProviderType.MINIMAX_CODE.value, MiniMaxCodeProvider, id="mcode"),
]


@pytest.mark.parametrize("provider_type", [p.value for p in ProviderType], ids=lambda v: v)
def test_registry_covers_every_provider_type(provider_type):
    """Registry ⊇ enum: nothing dispatchable may be missing from PROVIDER_CLASSES."""
    resolved = get_provider_class(provider_type)
    assert issubclass(resolved, BaseProvider)


@pytest.mark.parametrize("provider_type,expected_cls", NEWLY_REGISTERED)
def test_registry_class_matches_construct_provider_branch(provider_type, expected_cls):
    """The registry row and construct_provider's if/elif branch build the same class."""
    assert get_provider_class(provider_type) is expected_cls
    constructed = ProviderManager().construct_provider(
        provider_type,
        terminal_id="f664-t1",
        tmux_session="f664-s",
        tmux_window="f664-w",
        agent_profile="developer",
        persona_plan=None,
    )
    assert isinstance(constructed, expected_cls)


@pytest.mark.parametrize("provider_type,expected_cls", NEWLY_REGISTERED)
def test_site1_start_session_seed_probe_resolves(provider_type, expected_cls):
    """session_service.py:355 — ``get_provider_class(resolved).supports_seed_resume_identity``."""
    assert get_provider_class(provider_type).supports_seed_resume_identity is False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type,expected_cls", NEWLY_REGISTERED)
async def test_site2_seed_resume_bootstrap_returns_none(provider_type, expected_cls, tmp_path):
    """terminal_service.py:546 — seed_resume_bootstrap raised ValueError before F664."""
    from cli_agent_orchestrator.services.terminal_service import seed_resume_bootstrap

    assert await seed_resume_bootstrap("developer", provider_type, str(tmp_path)) is None


@pytest.mark.parametrize("provider_type,expected_cls", NEWLY_REGISTERED)
def test_site3_create_terminal_seed_required_guard(provider_type, expected_cls):
    """terminal_service.py:1845 — the ``seed_required`` guard reads the same flag."""
    provider_class = get_provider_class(provider_type)
    assert (provider_class.supports_seed_resume_identity is True) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type,expected_cls", NEWLY_REGISTERED)
async def test_site4_f295_preflight_hook_awaits_clean(provider_type, expected_cls):
    """terminal_service.py:2079 — the F295 preflight resolves the class and awaits its hook."""
    hook = getattr(get_provider_class(provider_type), "preflight_launch", None)
    assert hook is not None
    assert await hook(agent_profile="developer", model=None) is None


@pytest.mark.parametrize("provider_type,expected_cls", NEWLY_REGISTERED)
def test_site5_interrupt_keys_resolve(provider_type, expected_cls):
    """mcp_server/server.py:1470 — interrupt_terminal reads ``interrupt_keys`` off the class."""
    assert get_provider_class(provider_type).interrupt_keys == ["C-c"]


def test_minimax_plugin_forwards_terminal_token(tmp_path, monkeypatch):
    """F332: a registered mcode must ship CAO_TERMINAL_TOKEN with CAO_TERMINAL_ID."""
    monkeypatch.setenv("CAO_TERMINAL_TOKEN", "f664-token")
    provider = MiniMaxCodeProvider("f664-t1", "f664-s", "f664-w", agent_profile="developer")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    provider._write_plugin(data_dir, {"cao": {"command": "cao-mcp-server", "args": []}})

    manifest = json.loads(
        (data_dir / "plugins" / "cao-orchestrator" / "servers.mcp.json").read_text()
    )
    env = manifest["mcpServers"]["cao"]["env"]
    assert env["CAO_TERMINAL_ID"] == "f664-t1"
    assert env["CAO_TERMINAL_TOKEN"] == "f664-token"


def test_minimax_plugin_omits_token_when_unset(tmp_path, monkeypatch):
    """Negative control: no token in the environment means no empty token key."""
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    provider = MiniMaxCodeProvider("f664-t2", "f664-s", "f664-w", agent_profile="developer")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    provider._write_plugin(data_dir, {"cao": {"command": "cao-mcp-server", "args": []}})

    manifest = json.loads(
        (data_dir / "plugins" / "cao-orchestrator" / "servers.mcp.json").read_text()
    )
    env = manifest["mcpServers"]["cao"]["env"]
    assert env["CAO_TERMINAL_ID"] == "f664-t2"
    assert "CAO_TERMINAL_TOKEN" not in env
    assert os.environ.get("CAO_TERMINAL_TOKEN") is None
