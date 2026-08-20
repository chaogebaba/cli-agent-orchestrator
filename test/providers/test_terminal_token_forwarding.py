"""F332 AC11: Every provider that forwards CAO_TERMINAL_ID also forwards CAO_TERMINAL_TOKEN.

Enforced by iteration over PROVIDER_CLASSES, not a hand-written list.
"""

import importlib
import inspect
import re
from pathlib import Path

import pytest


def _provider_modules():
    """Yield (module_name, source_path) for every provider in PROVIDER_CLASSES."""
    from cli_agent_orchestrator.providers.manager import PROVIDER_CLASSES

    providers_dir = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator" / "providers"
    for provider_key, cls in PROVIDER_CLASSES.items():
        module = inspect.getmodule(cls)
        if module is None:
            continue
        source_file = inspect.getfile(cls)
        yield provider_key, Path(source_file)


@pytest.mark.parametrize(
    "provider_key,source_path",
    list(_provider_modules()),
    ids=[k for k, _ in _provider_modules()],
)
def test_provider_forwards_terminal_token_if_it_forwards_terminal_id(provider_key, source_path):
    """If a provider's source references CAO_TERMINAL_ID, it must also reference CAO_TERMINAL_TOKEN."""
    source = source_path.read_text(encoding="utf-8")

    has_terminal_id = "CAO_TERMINAL_ID" in source
    has_terminal_token = "CAO_TERMINAL_TOKEN" in source

    # Providers using bind_mcp_server_identity (which handles token forwarding
    # in sandbox_guard.py) are covered by that shared utility.
    uses_bind_identity = "bind_mcp_server_identity" in source

    if has_terminal_id:
        assert has_terminal_token or uses_bind_identity, (
            f"Provider '{provider_key}' ({source_path.name}) references CAO_TERMINAL_ID "
            f"but does NOT reference CAO_TERMINAL_TOKEN and does not use "
            f"bind_mcp_server_identity. F332 requires token forwarding."
        )
