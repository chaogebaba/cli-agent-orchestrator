"""AC-S1.11 and AC-S1.16 — D21's mode-scoped tool surface, and `effort`.

AC-S1.11 is explicit that the count must be taken from a SPAWNED PROCESS, and
gives the reason: a test that asserted over the imported module would pass
against a per-call check while the import-time gate did nothing.  So these arms
launch the real ``cao-mcp-server`` console script over stdio, perform a real MCP
handshake, and read ``tools/list`` off the wire.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.mcp_server.server import (
    BARE_MODE_TOOLS,
    _resolve_surface_mode,
)

#: Measured on this tree, 2026-09-16.  M14's figure was 49; this build reads 52,
#: which is 51 pre-existing plus D21's new ``list``.  AC-S1.11 says to pin the
#: number and treat the drift as the finding rather than to chase it — the
#: tripwire is what matters, and an unpinned count is not one.
SKILL_TOOL_COUNT = 52

_HANDSHAKE_TIMEOUT_S = 90.0


def _server_binary() -> str:
    candidate = Path(sys.executable).parent / "cao-mcp-server"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("cao-mcp-server")
    if found:
        return found
    pytest.skip("cao-mcp-server console script is not installed in this environment")


def _spawned_tool_names(mode: str, *, via_argv: bool = False) -> list[str]:
    """Launch the real server in ``mode`` and read ``tools/list`` over stdio."""
    env = dict(os.environ)
    argv = [_server_binary()]
    if via_argv:
        argv += ["--mode", mode]
        env.pop("CAO_MCP_MODE", None)
    else:
        env["CAO_MCP_MODE"] = mode
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None

    def send(frame: dict[str, object]) -> None:
        process.stdin.write(json.dumps(frame) + "\n")
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ac-s1-11", "version": "1"},
                },
            }
        )
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            frame = json.loads(line)
            if frame.get("id") == 1:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            elif frame.get("id") == 2:
                return sorted(tool["name"] for tool in frame["result"]["tools"])
        raise AssertionError(f"no tools/list reply from a {mode!r} server within the deadline")
    finally:
        process.kill()
        process.wait(timeout=10)


# ------------------------------------------------------------ AC-S1.11


@pytest.mark.slow
def test_bare_mode_exposes_exactly_the_five_tools() -> None:
    """BARE = 5: three to dispatch and route, one to see what is outstanding,
    one to pull a skill body as TEXT."""
    names = _spawned_tool_names("bare")
    assert names == sorted(BARE_MODE_TOOLS)
    assert len(names) == 5


@pytest.mark.slow
def test_a_bare_session_cannot_reach_peek_terminal() -> None:
    """The AC's named negative.  Not merely refused — ABSENT, so a client cannot
    form the call at all."""
    assert "peek_terminal" not in _spawned_tool_names("bare")


@pytest.mark.slow
def test_skill_mode_pins_its_count_by_name() -> None:
    """The tripwire D21's collision row asks for: "every new tool must declare
    its mode; the count test fails an undeclared one"."""
    names = _spawned_tool_names("skill")
    assert len(names) == SKILL_TOOL_COUNT, (
        f"the SKILL surface moved to {len(names)}; pin the new number and record "
        "the drift, per AC-S1.11"
    )
    assert set(BARE_MODE_TOOLS) <= set(names), "every BARE tool is also a SKILL tool"


@pytest.mark.slow
def test_the_mode_can_be_given_as_argv_too() -> None:
    """``cao launch --mode bare|skill`` is D21's stated surface; the env var is
    how it reaches a SPAWNED MCP process, which is where it is actually read."""
    assert _spawned_tool_names("bare", via_argv=True) == sorted(BARE_MODE_TOOLS)


def test_bare_is_the_default() -> None:
    """D0: the plane is infrastructure first, and BARE is the mode that must
    always work.  A deployment that wants the doctrine surface asks for it."""
    assert _resolve_surface_mode([], {}) == "bare"


@pytest.mark.parametrize("value", ["", "  ", "SKILLED", "doctrine", "1", "true"])
def test_an_unrecognised_mode_resolves_to_bare(value: str) -> None:
    """Fails toward the SMALLER surface.

    This runs at import in a server whose boot must not be failed by a
    configuration typo, and the directions are not symmetric: a missing tool is
    visible the moment something tries to use it, an unexpectedly exposed one is
    not visible at all.
    """
    assert _resolve_surface_mode([], {"CAO_MCP_MODE": value}) == "bare"


def test_argv_beats_the_environment() -> None:
    """``--mode`` is the more specific statement; an inherited ``CAO_MCP_MODE``
    from a parent process is the less specific one."""
    assert _resolve_surface_mode(["--mode", "skill"], {"CAO_MCP_MODE": "bare"}) == "skill"
    assert _resolve_surface_mode(["--mode=skill"], {"CAO_MCP_MODE": "bare"}) == "skill"


def test_the_bare_set_is_exactly_d21s_five() -> None:
    assert BARE_MODE_TOOLS == frozenset(
        {"assign", "send_message", "handoff", "list", "load_skill"}
    )


def test_load_skill_is_in_bare_and_returns_text_not_tools() -> None:
    """D21: "the U-K loader returns doctrine TEXT, never tools".

    A loader that could register tools would make BARE a mode a caller could
    escape from, and the whole point of the per-process gate is that it cannot.
    """
    from cli_agent_orchestrator.mcp_server import server

    assert "load_skill" in BARE_MODE_TOOLS
    source = server.LOAD_SKILL_TOOL_DESCRIPTION
    assert "Markdown" in source or "body" in source


# ------------------------------------------------------------ AC-S1.16


def test_effort_is_a_parameter_of_assign() -> None:
    """The fails-if is a parameter "accepted and silently dropped" — the failure
    shape a new pass-through always has.  So the chain is asserted link by link."""
    import inspect

    from cli_agent_orchestrator.mcp_server import server

    assert "effort" in inspect.signature(server.assign).parameters
    assert "effort" in inspect.signature(server._assign_impl).parameters
    assert "effort" in inspect.signature(server._create_terminal).parameters


def test_effort_reaches_the_terminal_service_and_the_api() -> None:
    import inspect

    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.services import terminal_service

    assert "effort" in inspect.signature(terminal_service.create_terminal).parameters
    assert "effort" in inspect.signature(api_main.create_terminal_in_session).parameters
    assert "effort" in inspect.signature(api_main.create_session).parameters


def test_an_empty_effort_is_a_request_not_an_omission() -> None:
    """``effort=""`` CLEARS the flag, exactly as an empty providers.toml value
    does.  A truthiness test on the way through would silently drop the one
    value a caller uses to say "no effort flag at all"."""
    from cli_agent_orchestrator.services.settings_service import resolve_reasoning_effort

    assert resolve_reasoning_effort("kiro_cli", {}, {}, None, requested="") is None
    assert resolve_reasoning_effort("kiro_cli", {}, {}, None, requested="high") == "high"


def test_the_request_outranks_every_providers_toml_layer() -> None:
    """Highest precedence, like ``model``: an argument naming ONE worker is more
    specific than configuration naming a whole profile."""
    from cli_agent_orchestrator.services.settings_service import resolve_reasoning_effort

    profile_defaults = {"reasoning_effort": "low"}
    provider_defaults = {"reasoning_effort": "medium"}
    assert (
        resolve_reasoning_effort("codex", profile_defaults, provider_defaults, None)
        == "low"
    ), "the control: without a request, the toml chain decides"
    assert (
        resolve_reasoning_effort(
            "codex", profile_defaults, provider_defaults, None, requested="xhigh"
        )
        == "xhigh"
    )


def test_omitting_effort_changes_nothing() -> None:
    """AC-S1.16's own control arm."""
    from cli_agent_orchestrator.services.settings_service import resolve_reasoning_effort

    for provider in ("codex", "grok_cli", "claude_code", "cline_cli", "pi_cli", "kiro_cli"):
        without = resolve_reasoning_effort(provider, {}, {}, None)
        with_none = resolve_reasoning_effort(provider, {}, {}, None, requested=None)
        assert without == with_none


def test_a_provider_with_no_effort_knob_ignores_the_request() -> None:
    """Routing may send the same assign to a different provider, so a hard error
    would make the argument provider-specific."""
    from cli_agent_orchestrator.services.settings_service import resolve_reasoning_effort

    assert resolve_reasoning_effort("no_such_provider", {}, {}, None, requested="high") is None


def test_every_provider_that_has_an_effort_knob_reads_the_request() -> None:
    """The six resolution sites, checked as source.

    A per-provider constructor parameter would have had six shapes, and a shape
    per provider is how a pass-through argument comes to be honoured by four of
    them.  This asserts all six read the one shared request.
    """
    import inspect

    from cli_agent_orchestrator.providers import (
        claude_code,
        cline_cli,
        codex,
        grok_cli,
        kiro_cli,
        pi_cli,
    )

    for module in (claude_code, cline_cli, codex, grok_cli, kiro_cli, pi_cli):
        source = inspect.getsource(module)
        assert "requested_effort_for_terminal" in source, module.__name__
