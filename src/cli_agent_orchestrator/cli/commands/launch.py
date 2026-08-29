"""Launch command for CLI Agent Orchestrator CLI."""

import os
import time
from pathlib import Path

import click
import requests

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import (
    DEFAULT_PROVIDER,
    PROVIDERS,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.http import CAOHttpClient

cao_http = CAOHttpClient(lambda: requests)
from cli_agent_orchestrator.utils.terminal import (
    poll_until_done,
    sync_backend_from_server,
    wait_until_terminal_status,
)

# Providers that require workspace folder access
PROVIDERS_REQUIRING_WORKSPACE_ACCESS = {
    "antigravity_cli",
    "claude_code",
    "codex",
    "copilot_cli",
    "cursor_cli",
    "grok_cli",
    "hermes",
    "kimi_cli",
    "kiro_cli",
    "mcode",
    "opencode_cli",
    "omp",
}

# Validation constraints for ``--env`` forwarded vars (mirrored server-side
# in ``TmuxClient._merge_extra_env``). See issue #248.
_FORWARDED_ENV_BLOCKED_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")
_FORWARDED_ENV_PREFIX_ALLOWLIST = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    }
)
_FORWARDED_ENV_MAX_VALUE_BYTES = 2048

# F541 (#397): POST /sessions/start latency is bounded by provider init
# (cold Claude Code + MCP startup after a reboot), NOT by an MCP request, so
# the 30s ``mcp_request_timeout`` is the wrong bound — it fired mid-launch and
# a successful-but-slow cold launch was reported as a connection failure. Use
# a launch-specific read timeout, and on a read timeout poll the session
# instead of failing (the seat is usually already up).
#
# The bound must exceed the SERVER's init ceiling + margin, or the CLI gives up
# before the server does. claude_code's server-side init cap is now
# ``claude_code_init_timeout`` (default 180s; the longest of any provider — see
# settings_service._SERVER_DEFAULTS), so both the read timeout and the poll
# window are set to init_ceiling(180) + 60s margin = 240s. Keep them a flat
# constant (not read from server settings) so the CLI has no extra round trip
# before it can even talk to the server; 240s comfortably covers the 180s
# default plus any modestly-raised override.
SESSION_START_TIMEOUT_S = 240
# Bounded confirm-then-attach poll after a read timeout: how long to keep
# asking GET /sessions/<name> whether the supervisor terminal came up, and how
# often. ~240s total at a 2s cadence — >= server init ceiling (180s) + 60s.
SESSION_START_POLL_TIMEOUT_S = 240
SESSION_START_POLL_INTERVAL_S = 2


def _parse_env_pairs(pairs):
    """Parse repeated ``KEY=VALUE`` entries into a dict, validating each.

    Mirrors the constraints applied to inherited env in TmuxClient so a
    forwarded var that would be silently dropped server-side is rejected at
    the CLI boundary with a clear error message instead.
    """
    result: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise click.ClickException(
                f"--env expects KEY=VALUE (got {raw!r}); did you forget the '='?"
            )
        key, value = raw.split("=", 1)
        # POSIX env names: leading letter/underscore, then alnum/underscore.
        # Stricter than ``str.isidentifier`` only in that it forbids non-ASCII.
        if (
            not key
            or not (key[0].isalpha() or key[0] == "_")
            or not all(c.isalnum() or c == "_" for c in key)
            or not key.isascii()
        ):
            raise click.ClickException(f"--env key must match [A-Za-z_][A-Za-z0-9_]* (got {key!r})")
        if key not in _FORWARDED_ENV_PREFIX_ALLOWLIST and any(
            key.startswith(p) for p in _FORWARDED_ENV_BLOCKED_PREFIXES
        ):
            raise click.ClickException(
                f"--env key {key!r} uses a blocked prefix "
                f"({', '.join(_FORWARDED_ENV_BLOCKED_PREFIXES)}) reserved for provider env"
            )
        if len(value.encode("utf-8")) >= _FORWARDED_ENV_MAX_VALUE_BYTES:
            raise click.ClickException(
                f"--env value for {key!r} exceeds {_FORWARDED_ENV_MAX_VALUE_BYTES} bytes "
                "(tmux argv limit, PR #246)"
            )
        if key == "CAO_ARTIFACTS_DIR" and (not value or not Path(value).is_absolute()):
            raise click.ClickException(
                "--env CAO_ARTIFACTS_DIR must be an absolute path " "(artifacts_dir_not_absolute)"
            )
        result[key] = value
    return result


def _poll_session_supervisor_after_timeout(session_name):
    """F541 (#397): after a read timeout on POST /sessions/start, confirm the
    launch actually succeeded by polling ``GET /sessions/<session_name>``.

    The read timeout does not mean the launch failed — a cold post-reboot
    provider init routinely takes longer than the request read timeout while
    the server goes on to create the supervisor terminal and bring the seat up
    healthy (issue #397 incident). So instead of reporting a failure, poll the
    session for up to ``SESSION_START_POLL_TIMEOUT_S`` and, the moment its
    supervisor terminal exists, return a terminal dict shaped like the one the
    POST would have returned so the caller can continue the normal path.

    Returns the supervisor terminal dict on success, or ``None`` if the
    supervisor never appeared within the bound (launch still initializing or
    genuinely failed). A 404 while polling is not fatal — the session row may
    not be visible yet — so it is treated the same as "not ready yet". A
    connection error while polling IS surfaced (the server is unreachable, a
    different failure class from a slow launch).

    Raises ``requests.exceptions.RequestException`` if the server becomes
    unreachable during polling.
    """
    if not session_name:
        # Without a caller-supplied session name we cannot address the poll;
        # the server would have minted one, but it is only returned in the POST
        # response we never received. Signal "cannot confirm" to the caller.
        return None

    deadline = time.monotonic() + SESSION_START_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            resp = cao_http.get(
                f"/sessions/{session_name}",
                timeout=get_server_settings()["mcp_request_timeout"],
            )
        except requests.exceptions.RequestException:
            # Server unreachable — a genuinely different failure from a slow
            # launch. Propagate so the caller reports "server unreachable".
            raise
        if resp.status_code == 404:
            # Session row not visible yet; keep waiting.
            time.sleep(SESSION_START_POLL_INTERVAL_S)
            continue
        if resp.status_code >= 400:
            time.sleep(SESSION_START_POLL_INTERVAL_S)
            continue
        try:
            payload = resp.json()
        except ValueError:
            time.sleep(SESSION_START_POLL_INTERVAL_S)
            continue
        terminals = payload.get("terminals") or []
        if terminals:
            # The supervisor terminal exists — the launch succeeded. Return the
            # first terminal (the supervisor) with fields the caller needs.
            supervisor = terminals[0]
            session_info = payload.get("session") or {}
            resolved_session = (
                supervisor.get("session_name") or session_info.get("id") or session_name
            )
            return {
                "id": supervisor["id"],
                "name": supervisor.get("name", supervisor["id"]),
                "session_name": resolved_session,
            }
        time.sleep(SESSION_START_POLL_INTERVAL_S)
    return None


def _finish_launch_after_start(terminal, *, headless, message, is_async):
    """Attach to the launched session (or send the message in headless mode).

    Extracted so both the normal POST path and the F541 (#397) read-timeout
    confirm-then-attach path share exactly one post-start behavior. ``terminal``
    must carry ``id``, ``name``, and ``session_name``.

    Attach to tmux session unless headless. Wait for the provider to finish
    initializing first — otherwise tmux attach races with the TUI's input
    handler wiring, resizes the pty mid-init, and the TUI silently drops
    keystrokes (issue #220). The wait is advisory: if it times out we still
    attach so the user can inspect the half-initialized session rather than
    orphan it in tmux.
    """
    if not headless:
        # Align the CLI's backend singleton with the running server. Without
        # this, ``cao-server --terminal herdr`` + no config.json entry causes
        # the CLI to default to tmux. See issue #308.
        sync_backend_from_server()
        ready = wait_until_terminal_status(
            terminal["id"],
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120,
        )
        if not ready:
            click.echo(
                click.style(
                    f"  Warning: {terminal['id']} did not reach idle within 120s — "
                    "attaching anyway; input may be unreliable until init completes.",
                    fg="yellow",
                )
            )
        get_backend().attach_session(terminal["session_name"])
    elif message:
        ready = wait_until_terminal_status(
            terminal["id"],
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120,
        )
        if not ready:
            raise click.ClickException(
                f"Conductor {terminal['id']} did not become ready within 120s"
            )
        request_timeout = get_server_settings()["mcp_request_timeout"]
        response = cao_http.post(
            f"/terminals/{terminal['id']}/input",
            params={"message": message},
            timeout=request_timeout,
        )
        response.raise_for_status()
        time.sleep(3)
        if is_async:
            click.echo(f"Message sent to {terminal['name']}. Running in background.")
            return
        poll_until_done(terminal["id"], timeout=300)
        request_timeout = get_server_settings()["mcp_request_timeout"]
        output_resp = cao_http.get(
            f"/terminals/{terminal['id']}/output",
            params={"mode": "last"},
            timeout=request_timeout,
        )
        output_resp.raise_for_status()
        output = output_resp.json().get("output", "")
        if output:
            click.echo(output)


@click.command()
@click.argument("message", required=False, default=None)
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider",
    default=None,
    help=f"Provider to use (default: profile provider or {DEFAULT_PROVIDER})",
)
@click.option(
    "--engine",
    "engine",
    type=click.Choice(["v2", "kas"], case_sensitive=True),
    default=None,
    help="Explicit Kiro engine (default: profile engine or v2).",
)
@click.option(
    "--allowed-tools",
    multiple=True,
    help="Override allowedTools (CAO format: execute_bash, fs_read, @cao-mcp-server). Repeatable.",
)
@click.option(
    "--async",
    "is_async",
    is_flag=True,
    help="Send message and return immediately without waiting for completion",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Skip confirmation prompt (restrictions still enforced).",
)
@click.option(
    "--yolo",
    is_flag=True,
    help="[DANGEROUS] Unrestricted tool access AND skip confirmation prompts. "
    "Agent can execute ANY command including aws, rm, curl.",
)
@click.option(
    "--working-directory",
    default=None,
    help="Working directory for the session (default: current directory)",
)
@click.option(
    "--memory",
    "memory",
    is_flag=True,
    help="Also launch a context-manager (memory_manager) terminal for curated memory injection.",
)
@click.option(
    "--env",
    "env_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Forward an env var to the supervisor AND every worker spawned later "
    "in the same session. Repeatable. Values travel in the request body, not "
    "the URL. Blocked prefixes (CLAUDE/CODEX_/__MISE_) and >=2048-byte values "
    "are rejected. See issue #248.",
)
@click.option(
    "--allow-incomplete-brief",
    is_flag=True,
    help="Allow a required session brief to degrade loudly instead of aborting startup.",
)
@click.option(
    "--resume-session-id",
    "resume_session_id",
    default=None,
    metavar="SESSION_ID",
    help="Resume a prior Claude Code conversation in the launched supervisor "
    "(claude --resume <id>). claude_code provider only.",
)
def launch(
    message,
    agents,
    session_name,
    headless,
    is_async,
    provider,
    engine,
    allowed_tools,
    auto_approve,
    yolo,
    working_directory,
    memory,
    env_pairs,
    allow_incomplete_brief,
    resume_session_id,
):
    """Launch cao session with specified agent profile."""
    try:
        click.echo(
            "WARNING: cao launch is deprecated; use cao session start; "
            "cao launch will be removed in the next major release",
            err=True,
        )
        display_dir = working_directory or os.path.realpath(os.getcwd())
        explicit_provider = provider is not None  # True only when --provider was passed
        forwarded_env = _parse_env_pairs(env_pairs) if env_pairs else {}

        # Resolve allowedTools: --yolo > --allowed-tools CLI > profile/role defaults
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
        from cli_agent_orchestrator.utils.tool_mapping import (
            format_tool_summary,
            get_disallowed_tools,
            resolve_allowed_tools,
        )

        resolved_allowed_tools = None
        no_role_set = False
        if yolo:
            resolved_allowed_tools = ["*"]
        elif allowed_tools:
            resolved_allowed_tools = list(allowed_tools)
        else:
            # Load profile to get role-based defaults
            try:
                profile = load_agent_profile(agents)
                mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
                no_role_set = not profile.role and not profile.allowedTools
                resolved_allowed_tools = resolve_allowed_tools(
                    profile.allowedTools, profile.role, mcp_server_names
                )
            except (FileNotFoundError, RuntimeError):
                # Profile not found — use developer defaults (backward compatible)
                no_role_set = True
                resolved_allowed_tools = resolve_allowed_tools(None, None, None)

        # Honour profile.provider whenever the user did not pass --provider
        # explicitly. This runs regardless of which permission-resolution
        # branch above fired — provider selection ("which CLI runs this
        # agent?") is orthogonal to tool restrictions ("what is the agent
        # allowed to do?"). Previously this lookup lived inside the ``else``
        # branch and ``--yolo`` / ``--allowed-tools`` silently bypassed the
        # profile's ``provider:`` field, breaking heterogeneous-panel
        # workflows. See issue #239. ``resolve_provider`` falls back to
        # ``DEFAULT_PROVIDER`` when the profile is missing or has no
        # ``provider`` key, so the trailing fallback is no longer needed.
        if provider is None:
            from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

            provider = resolve_provider(agents, DEFAULT_PROVIDER)

        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}"
            )
        # Confirmation / warning prompts
        if provider in PROVIDERS_REQUIRING_WORKSPACE_ACCESS:
            if yolo:
                # --yolo: warn but don't block
                click.echo(click.style("\n[WARNING] --yolo mode enabled", fg="yellow", bold=True))
                click.echo(
                    f"  Agent '{agents}' launching UNRESTRICTED on {provider}.\n"
                    f"  Agent can execute ANY command (aws, rm, curl, read credentials).\n"
                    f"  Directory: {display_dir}\n"
                )
                if provider == "kiro_cli":
                    # The kiro-cli TUI blocks on an interactive "Yes, I accept"
                    # consent dialog when --trust-all-tools is set. CAO answers
                    # it automatically after launch (the provider verifies the
                    # dialog first), so no --legacy-ui suppression is needed —
                    # and --legacy-ui must not be used, because it selects the
                    # v1 engine, which serves the agent no MCP tools.
                    click.echo(
                        "  Note: kiro_cli's --trust-all-tools consent dialog will be "
                        "auto-answered at startup.\n"
                    )
                elif provider == "opencode_cli":
                    # opencode's TUI has no runtime skip-permissions flag
                    # (tracked upstream in sst/opencode#8463). Permissions are
                    # install-time only, so --yolo cannot loosen them here.
                    click.echo(
                        click.style(
                            "  Note: --yolo has no runtime effect on opencode_cli.\n"
                            "  Permissions are set at cao install time. To get unrestricted\n"
                            "  access, set 'allowedTools: [\"*\"]' in the profile and re-run\n"
                            "  'cao install'. See docs/opencode-cli.md for details.\n",
                            fg="yellow",
                        )
                    )
            else:
                # Normal launch: show tool summary and confirm
                tool_summary = format_tool_summary(resolved_allowed_tools)
                blocked = get_disallowed_tools(provider, resolved_allowed_tools)
                blocked_summary = ", ".join(blocked) if blocked else "(none)"

                click.echo(
                    f"\nAgent '{agents}' launching on {provider}:\n"
                    f"  Allowed:  {tool_summary}\n"
                    f"  Blocked:  {blocked_summary}\n"
                    f"  Directory: {display_dir}\n"
                )
                if no_role_set:
                    click.echo(
                        "  Note: No role or allowedTools set — defaulting to 'developer'.\n"
                        "  Add 'role' or 'allowedTools' to your agent profile to control tool access.\n"
                        "  Docs: https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/tool-restrictions.md\n"
                    )
                click.echo(
                    "  To skip this prompt next time, relaunch with --auto-approve\n"
                    "  To remove all restrictions, relaunch with --yolo\n"
                )
                if not auto_approve and not click.confirm("Proceed?", default=True):
                    raise click.ClickException("Launch cancelled by user")

        # Call API to create session — pass working_directory only if explicitly
        # provided. When omitted, the server defaults to its own CWD.
        url = "/sessions/start"
        params = {
            "agent_profile": agents,
            "working_directory": working_directory or os.getcwd(),
        }
        if explicit_provider:
            params["provider"] = provider
        if engine is not None:
            params["engine"] = engine
        if session_name:
            params["session_name"] = session_name
        if resolved_allowed_tools:
            # Pass as comma-separated string for query param
            params["allowed_tools"] = ",".join(resolved_allowed_tools)
        if memory:
            params["memory"] = "true"
        if allow_incomplete_brief:
            params["allow_incomplete_brief"] = "true"
        if resume_session_id:
            params["resume_session_id"] = resume_session_id

        # Forwarded env vars travel in the JSON body so values (which may
        # contain secrets) don't end up in cao-server's HTTP access log.
        # See issue #248.
        #
        # F541 (#397): the read timeout here is launch-specific (~120s), NOT
        # ``mcp_request_timeout`` (30s) — POST /sessions/start latency is
        # bounded by provider init, not by an MCP request. On a read timeout we
        # confirm-then-attach: poll the session and, if the supervisor terminal
        # came up, continue the normal success path rather than reporting a
        # failure for a launch that actually succeeded.
        post_kwargs: dict = {"params": params, "timeout": SESSION_START_TIMEOUT_S}
        if forwarded_env:
            post_kwargs["json"] = {"env_vars": forwarded_env}

        from cli_agent_orchestrator.cli.http import format_domain_detail, response_detail

        try:
            response = cao_http.post(url, **post_kwargs)
        except requests.exceptions.Timeout:
            # The launch did not fail — provider init simply outran the read
            # timeout. Poll the session to confirm the supervisor came up.
            click.echo(
                "Launch is still initializing (server did not respond within "
                f"{SESSION_START_TIMEOUT_S}s); confirming the session came up...",
                err=True,
            )
            try:
                confirmed = _poll_session_supervisor_after_timeout(session_name)
            except requests.exceptions.RequestException as e:
                raise click.ClickException(
                    f"cao-server became unreachable while confirming the launch: {e}"
                )
            if confirmed is None:
                target = f" '{session_name}'" if session_name else ""
                raise click.ClickException(
                    f"Launch of session{target} is still initializing after "
                    f"{SESSION_START_POLL_TIMEOUT_S}s and could not be confirmed. "
                    "The server is reachable but the supervisor terminal has not "
                    "come up yet — check `cao status` shortly, or attach manually."
                )
            terminal = confirmed
            click.echo(f"Session created: {terminal['session_name']}")
            click.echo(f"Terminal created: {terminal['name']}")
            _finish_launch_after_start(
                terminal,
                headless=headless,
                message=message,
                is_async=is_async,
            )
            return

        start_error = None
        try:
            start_error = response.json()
        except ValueError:
            pass
        if (
            response.status_code == 422
            and isinstance(start_error, dict)
            and start_error.get("bootstrap", {}).get("status") == "seed_failed"
        ):
            click.echo(f"bootstrap failed [{start_error['bootstrap']['error_code']}]", err=True)
            raise click.exceptions.Exit(2)
        detail = response_detail(response)
        if (
            response.status_code in {409, 500}
            and detail
            and detail.get("code")
            in {"mailbox_conflict", "mailbox_authority_timeout", "publication_cleanup_failed"}
        ):
            click.echo(format_domain_detail(detail), err=True)
            raise click.exceptions.Exit(1)
        response.raise_for_status()

        start_payload = response.json()
        # Preserve wrapper behavior for legacy-compatible/mocked servers while
        # the real create call delegates to /sessions/start.
        terminal = start_payload.get("supervisor_terminal", start_payload)

        click.echo(f"Session created: {terminal['session_name']}")
        click.echo(f"Terminal created: {terminal['name']}")

        _finish_launch_after_start(
            terminal,
            headless=headless,
            message=message,
            is_async=is_async,
        )

    except click.exceptions.Exit:
        raise
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {str(e)}")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))
