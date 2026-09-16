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
    InvalidSurfaceMode,
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
    extra: list[str] = []
    if via_argv:
        extra = ["--mode", mode]
        env.pop("CAO_MCP_MODE", None)
    else:
        env["CAO_MCP_MODE"] = mode
    return _spawned_tool_names_with_env(env, extra)


def _spawned_tool_names_with_env(env: dict[str, str], extra_argv: list[str]) -> list[str]:
    argv = [_server_binary(), *extra_argv]
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
def test_a_spawned_server_with_no_mode_at_all_is_bare() -> None:
    """The ruling's first arm, end to end: absent -> 5 tools, in a real process."""
    env = dict(os.environ)
    env.pop("CAO_MCP_MODE", None)
    names = _spawned_tool_names_with_env(env, [])
    assert names == sorted(BARE_MODE_TOOLS)


@pytest.mark.slow
def test_the_mode_can_be_given_as_argv_too() -> None:
    """``cao launch --mode bare|skill`` is D21's stated surface; the env var is
    how it reaches a SPAWNED MCP process, which is where it is actually read."""
    assert _spawned_tool_names("bare", via_argv=True) == sorted(BARE_MODE_TOOLS)


@pytest.mark.slow
def test_a_spawned_server_refuses_an_unreadable_mode() -> None:
    """The refusal is not merely raisable — it stops the process.

    A server that logged the refusal and carried on would be the guess this
    ruling exists to forbid, dressed as a warning nobody reads.
    """
    env = dict(os.environ)
    env["CAO_MCP_MODE"] = "doctrine"
    process = subprocess.run(
        [_server_binary()],
        input="",
        env=env,
        capture_output=True,
        text=True,
        timeout=_HANDSHAKE_TIMEOUT_S,
    )
    assert process.returncode != 0
    assert "InvalidSurfaceMode" in process.stderr or "not a surface mode" in process.stderr


# --------------------------------------------------- the flag that sets it


def test_cao_launch_carries_the_mode_to_the_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """D21's flag, and the variable it sets.

    Asserted over the REQUEST BODY, because the forwarded-env channel is the one
    path that reaches the supervisor's process environment and every worker
    spawned later in the session — which is exactly the mode's scope.
    """
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands import launch as launch_cmd

    captured: dict[str, object] = {}

    class _Response:
        status_code = 500
        text = "stop here"

        def json(self) -> dict[str, object]:
            return {"detail": "stop here"}

    def _post(url: str, **kwargs: object) -> _Response:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(launch_cmd.cao_http, "post", _post)
    CliRunner().invoke(
        launch_cmd.launch,
        ["--agents", "developer", "--mode", "skill", "--headless", "--auto-approve"],
    )
    body = captured.get("json") or {}
    assert body.get("env_vars", {}).get("CAO_MCP_MODE") == "skill"


def test_cao_launch_sends_nothing_when_the_flag_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Default bare" is a property of the READER, not of the wire.

    The flag's default and the server's default are the same value, so
    transmitting ``bare`` for a launch that said nothing would change no
    behaviour while breaking the standing contract that a launch with no
    ``--env`` sends no request body at all.
    """
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands import launch as launch_cmd

    captured: dict[str, object] = {}

    class _Response:
        status_code = 500
        text = "stop here"

        def json(self) -> dict[str, object]:
            return {"detail": "stop here"}

    monkeypatch.setattr(
        launch_cmd.cao_http, "post", lambda url, **kw: (captured.update(kw), _Response())[1]
    )
    CliRunner().invoke(launch_cmd.launch, ["--agents", "developer", "--headless", "--auto-approve"])
    body = captured.get("json") or {}
    assert "CAO_MCP_MODE" not in body.get("env_vars", {})
    # ...and the seat that receives no variable resolves BARE, which is the arm
    # that makes the silence safe.
    assert _resolve_surface_mode([], {}) == "bare"


@pytest.mark.parametrize("command", ["launch", "session-start"])
def test_the_cli_refuses_an_unreadable_mode_before_anything_is_launched(command: str) -> None:
    """The ruling's "refused at launch": ``click.Choice`` rejects it at the CLI
    boundary, so the server is never asked to interpret it."""
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.launch import launch as launch_cmd
    from cli_agent_orchestrator.cli.commands.session import session as session_cmd

    if command == "launch":
        result = CliRunner().invoke(launch_cmd, ["--agents", "developer", "--mode", "doctrine"])
    else:
        result = CliRunner().invoke(
            session_cmd, ["start", "--agents", "developer", "--mode", "doctrine"]
        )
    assert result.exit_code == 2
    assert "'bare'" in result.output and "'skill'" in result.output


def test_the_flag_and_the_reader_share_one_variable_name() -> None:
    """A second literal is how a flag comes to set a variable nothing reads."""
    from cli_agent_orchestrator.cli.commands.launch import _MCP_MODE_ENV_VAR
    from cli_agent_orchestrator.mcp_server.server import _MODE_ENV_VAR

    assert _MCP_MODE_ENV_VAR == _MODE_ENV_VAR == "CAO_MCP_MODE"


def test_bare_is_the_default() -> None:
    """D21: the plane is infrastructure first, and BARE is the mode that must
    always work.  A deployment that wants the doctrine surface asks for it."""
    assert _resolve_surface_mode([], {}) == "bare"


@pytest.mark.parametrize("value", ["", "  "])
def test_a_cleared_variable_is_a_withdrawn_request_not_an_unreadable_one(value: str) -> None:
    """Clearing a variable is how an operator withdraws a request, so it takes
    the default rather than the refusal."""
    assert _resolve_surface_mode([], {"CAO_MCP_MODE": value}) == "bare"


@pytest.mark.parametrize("value", ["SKILLED", "doctrine", "1", "true", "BARE-ISH"])
def test_an_explicit_unparseable_mode_is_REFUSED(value: str) -> None:
    """The operator ASKED for something and the server cannot tell what.

    Guessing is the one outcome nobody could debug from the outside: a seat
    serving 5 tools when its operator typed something meaning 52 looks exactly
    like a seat that was launched bare on purpose.  Deliberately unlike
    ``core/switches.py``, whose refusals are values — there a caller can decline
    ONE subsystem and keep booting, and here the subsystem IS the process.
    """
    with pytest.raises(InvalidSurfaceMode) as raised:
        _resolve_surface_mode([], {"CAO_MCP_MODE": value})
    message = str(raised.value)
    assert "'bare'" in message and "'skill'" in message, "the refusal names what is accepted"
    assert "cao launch --mode" in message, "and carries the literal line to type"


def test_an_unparseable_argv_mode_is_refused_too() -> None:
    """Both spellings, because both reach the same import-time resolution."""
    with pytest.raises(InvalidSurfaceMode):
        _resolve_surface_mode(["--mode", "doctrine"], {})
    with pytest.raises(InvalidSurfaceMode):
        _resolve_surface_mode(["--mode=doctrine"], {})


@pytest.mark.parametrize("value", ["BARE", "Skill", " skill "])
def test_a_recognised_mode_is_case_and_whitespace_tolerant(value: str) -> None:
    """Tolerant about SPELLING, strict about MEANING — the refusal above is for
    values that mean nothing, not for a capital letter."""
    assert _resolve_surface_mode([], {"CAO_MCP_MODE": value}) == value.strip().lower()


def test_argv_beats_the_environment() -> None:
    """``--mode`` is the more specific statement; an inherited ``CAO_MCP_MODE``
    from a parent process is the less specific one."""
    assert _resolve_surface_mode(["--mode", "skill"], {"CAO_MCP_MODE": "bare"}) == "skill"
    assert _resolve_surface_mode(["--mode=skill"], {"CAO_MCP_MODE": "bare"}) == "skill"


def test_the_bare_set_is_exactly_d21s_five() -> None:
    assert BARE_MODE_TOOLS == frozenset({"assign", "send_message", "handoff", "list", "load_skill"})


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
        resolve_reasoning_effort("codex", profile_defaults, provider_defaults, None) == "low"
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


# ------------------------------------------------------------ S4: the schema


@pytest.mark.slow
def test_bare_assign_exposes_only_d21s_shape() -> None:
    """S4: pruning the tool LIST while leaving the schema is half the mode.

    D21 specifies ``assign(task, position? | provider?, model?, effort?, cwd?)``.
    The SKILL-era function carries twenty parameters, so a BARE seat was reading
    a surface full of fork bases, barriers, worktrees and authority pins —
    vocabulary the mode exists to keep away from it. AC-S1.11 pins the count
    only, which is why it could not see this.

    Read off a SPAWNED server's ``tools/list``, because the schema a client
    actually receives is the thing under test.
    """
    schema = _spawned_assign_schema("bare")
    assert set(schema["properties"]) == {
        "agent_profile",
        "message",
        "provider",
        "model",
        "effort",
        "working_directory",
    }
    assert set(schema.get("required", [])) == {"agent_profile", "message"}


@pytest.mark.slow
def test_skill_assign_keeps_its_full_shape() -> None:
    """The control. Narrowing BARE must not narrow the doctrine surface, where
    fork bases and barriers are the vocabulary."""
    schema = _spawned_assign_schema("skill")
    assert "fork_from" in schema["properties"]
    assert "barrier" in schema["properties"]
    assert len(schema["properties"]) >= 20


def test_the_narrowing_drops_no_parameter_the_function_requires() -> None:
    """A schema that hid a REQUIRED parameter would make every BARE assign fail.

    So the kept set is checked against the function's own signature: everything
    without a default must survive the prune.
    """
    import inspect

    from cli_agent_orchestrator.mcp_server.server import BARE_ASSIGN_PARAMETERS, assign

    for name, parameter in inspect.signature(assign).parameters.items():
        if parameter.default is inspect.Parameter.empty:
            assert name in BARE_ASSIGN_PARAMETERS, f"{name} is required and was pruned"


def _spawned_assign_schema(mode: str) -> dict:
    """``assign``'s inputSchema from a real ``tools/list``."""
    env = dict(os.environ)
    env["CAO_MCP_MODE"] = mode
    process = subprocess.Popen(
        [_server_binary()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None

    def send(frame: dict) -> None:
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
                    "clientInfo": {"name": "s4", "version": "1"},
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
                for tool in frame["result"]["tools"]:
                    if tool["name"] == "assign":
                        return tool["inputSchema"]
                raise AssertionError(f"no assign tool in {mode} mode")
        raise AssertionError(f"no tools/list reply from a {mode!r} server")
    finally:
        process.kill()
        process.wait(timeout=10)
