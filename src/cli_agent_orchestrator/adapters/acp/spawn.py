"""D2 — spawn a headless ACP seat, and bind it.

D2 is the decision this module exists for: **the supervisor seat is a headless
ACP agent subprocess owned by CAO. No provider TUI, no pane, for a certified
seat.** Until this existed nothing in the tree ever started one, so D2 was a
paragraph rather than a code path and the whole plane was unreachable — the S1
review's B1.5.

What a seat spawn IS, in order: resolve the vendored adapter argv (never
``npx``, so the first boot after a cache wipe cannot hang), start the
subprocess in its own process group, ``initialize`` at protocol 1, open a
session with the D10 MCP entry carrying the terminal's identity, and bind the
client into the registry so a carrier can find it again.

What it is NOT: a pane, a backend window, or anything the tmux coordinates
describe. A seat spawned here produces a ``transport='acp'`` row whose
coordinates are NULL by design (D20).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cli_agent_orchestrator.adapters.acp.client import ACP_PROTOCOL_VERSION, AcpClient, AcpFrameLog
from cli_agent_orchestrator.adapters.acp.prefetch import AdapterUnavailable, resolve_adapter_argv
from cli_agent_orchestrator.adapters.acp.registry import AcpSessionRegistry, acp_sessions

logger = logging.getLogger(__name__)

__all__ = ["SeatSpawnFailed", "acp_adapter_for_provider", "frame_log_path", "spawn_acp_seat"]


class SeatSpawnFailed(RuntimeError):
    """The seat could not be brought up.

    Raised, unlike almost everything else in this plane, because a launch that
    cannot start its agent has produced nothing to report a typed reason ON. The
    caller unwinds the terminal row; there is no half-seat to keep.
    """


#: Which vendored adapter serves which CAO provider (D8: one client, a launch
#: spec, a certification row — no per-CLI driver class).
_ADAPTER_BY_PROVIDER = {
    "claude_code": "claude-acp",
    "codex": "codex-acp",
    "pi_cli": "pi-acp",
}


def acp_adapter_for_provider(provider: str) -> str | None:
    """The adapter name for a provider, or ``None`` when it has no ACP seat.

    ``None`` is a real answer: S1 certifies two seat drivers and S3 adds the
    workers, so a provider absent from this table is not a failure, it is one
    that has not been certified yet and must take the native path.
    """
    return _ADAPTER_BY_PROVIDER.get(provider)


def frame_log_path(terminal_id: str, *, environ: dict[str, str] | None = None) -> Path:
    """Where this seat's frames land.

    Under the terminal's own id because AC-S1.2, AC-S1.19 and AC-S1.21 are all
    asserted FROM the frame log, and a log shared between seats — or between
    runs — makes a per-session count unreadable. That is not hypothetical: a
    shared append-mode log is exactly what made one of this lane's own arms read
    four prompts where there were two.
    """
    env = environ if environ is not None else dict(os.environ)
    home = env.get("CAO_HOME_DIR") or str(Path.home() / ".aws" / "cli-agent-orchestrator")
    return Path(home) / "acp-frames" / f"{terminal_id}.jsonl"


def _rotate_frame_log(path: Path) -> None:
    """Move a previous session's frames aside before a new seat writes.

    ``AcpFrameLog`` appends, which is right WITHIN a session — the log is the
    evidence AC-S1.2, AC-S1.19 and AC-S1.21 are asserted from, and a truncating
    writer would lose frames on a crash. It is wrong ACROSS sessions: a respawn
    is a new subprocess and a new session id, and appending makes a per-session
    count unreadable.

    That is not a hypothetical. It has now cost this WP twice: once when two runs
    shared an output directory and a count read four prompts where there were
    two, and again when a re-run of the same terminal id read forty where there
    were twenty. Both times the transport was fine and the evidence was not.

    Rotated rather than deleted. The previous session's frames are exactly what
    someone debugging a respawn wants, and the numbered suffix keeps them without
    letting them contaminate the next count.
    """
    if not path.exists():
        return
    index = 1
    while True:
        rotated = path.with_suffix(f".{index}{path.suffix}")
        if not rotated.exists():
            path.rename(rotated)
            return
        index += 1


def spawn_acp_seat(
    *,
    terminal_id: str,
    provider: str,
    cwd: str,
    auth_token: str | None = None,
    mcp_servers: list[dict[str, object]] | None = None,
    registry: AcpSessionRegistry | None = None,
    environ: dict[str, str] | None = None,
    timeout_s: float = 120.0,
) -> AcpClient:
    """Start a headless ACP seat for ``terminal_id`` and bind it.  Never a pane.

    ``auth_token`` is D22's CAO-issued token. It rides the MCP entry's ``env``
    (D10) rather than the prompt, because identity is something CAO issues and
    binds to the session id — not something an agent tells us about itself.
    """
    adapter = acp_adapter_for_provider(provider)
    if adapter is None:
        raise SeatSpawnFailed(
            f"provider {provider!r} has no certified ACP adapter; it must take the "
            "native path until its row is certified"
        )
    env = dict(environ if environ is not None else os.environ)
    try:
        argv = resolve_adapter_argv(adapter, environ=env)
    except AdapterUnavailable as exc:
        raise SeatSpawnFailed(str(exc)) from exc

    if auth_token:
        env["CAO_TERMINAL_ID"] = terminal_id
        env["CAO_TERMINAL_TOKEN"] = auth_token

    log_path = frame_log_path(terminal_id, environ=env)
    _rotate_frame_log(log_path)
    client = AcpClient(
        argv,
        frame_log=AcpFrameLog(log_path),
        cwd=cwd,
        env=env,
        stderr_path=log_path.with_suffix(".stderr"),
    )
    client.start()

    reply = client.initialize(client_name="cao", timeout=timeout_s)
    if "result" not in reply:
        client.terminate_process_group(grace_s=3)
        raise SeatSpawnFailed(f"initialize failed for {adapter}: {reply}")
    negotiated = (reply.get("result") or {}).get("protocolVersion")
    if negotiated not in (None, ACP_PROTOCOL_VERSION):
        # D4 froze v1. A v2 seat is not a seat we know how to drive, and starting
        # one anyway would certify a row against a protocol nobody measured.
        client.terminate_process_group(grace_s=3)
        raise SeatSpawnFailed(
            f"{adapter} negotiated protocolVersion {negotiated}; this build speaks "
            f"{ACP_PROTOCOL_VERSION} only (D4)"
        )

    session = client.session_new(cwd=cwd, mcp_servers=mcp_servers or [], timeout=timeout_s)
    if not (session.get("result") or {}).get("sessionId"):
        client.terminate_process_group(grace_s=3)
        raise SeatSpawnFailed(f"session/new failed for {adapter}: {session}")

    (registry or acp_sessions).bind(terminal_id, client)
    logger.info(
        "acp seat up: terminal=%s adapter=%s session=%s pid=%s",
        terminal_id,
        adapter,
        client.session_state().session_id,
        client.pid,
    )
    return client
