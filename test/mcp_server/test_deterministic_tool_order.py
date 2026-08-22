"""Regression test: tools/list returns tools in deterministic alphabetical order.

MCP 2026-07-28: "Servers SHOULD return tools/list in deterministic order."
AC5 of wp-mcp-2026 blueprint — sorted by name for prompt-cache stability.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_SESSION_NAME", "test")
    monkeypatch.setenv("CAO_TERMINAL_ID", "test1234")


@pytest.mark.usefixtures("_env")
def test_tools_list_is_sorted_alphabetically() -> None:
    """The server's tools/list must return tools sorted by name."""
    from cli_agent_orchestrator.mcp_server.server import mcp

    async def _list() -> list[str]:
        tools = await mcp.list_tools()
        return [t.name for t in tools]

    names = asyncio.run(_list())
    assert names == sorted(names), (
        f"tools/list is not sorted alphabetically; first out-of-order: "
        f"{next((a, b) for a, b in zip(names, sorted(names)) if a != b)}"
    )
    # Sanity: we have at least some tools registered
    assert len(names) > 10
