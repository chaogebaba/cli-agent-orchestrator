"""Session service for session-level operations.

This module provides session management functionality for CAO, where a "session"
corresponds to a tmux session that may contain multiple terminal windows (agents).

Session Hierarchy:
- Session: A tmux session (e.g., "cao-my-project")
  - Terminal: A tmux window within the session (e.g., "developer-abc123")
    - Provider: The CLI agent running in the terminal (e.g., KiroCliProvider)

Key Operations:
- list_sessions(): Get all CAO-managed sessions (filtered by SESSION_PREFIX)
- get_session(): Get session details including all terminal metadata
- delete_session(): Clean up session, providers, database records, and tmux session

Session Lifecycle:
1. create_terminal() with new_session=True creates a new tmux session
2. Additional terminals are added via create_terminal() with new_session=False
3. delete_session() removes the entire session and all contained terminals
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateSessionEvent,
    PostKillSessionEvent,
)
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import clear_session_env
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile, resolve_provider
from cli_agent_orchestrator.utils.sandbox_guard import require_provider_admitted
from cli_agent_orchestrator.utils.terminal import generate_session_name

logger = logging.getLogger(__name__)

SESSION_TEARDOWN_VERIFY_ATTEMPTS = 5
SESSION_TEARDOWN_VERIFY_DELAY_SECONDS = 0.2
ARTIFACTS_DIR_ENV = "CAO_ARTIFACTS_DIR"


def canonical_session_env(
    working_directory: str | None,
    env_vars: dict[str, str] | None,
) -> dict[str, str]:
    """Return the session floor with one absolute, immutable artifact root."""
    result = dict(env_vars or {})
    override = result.get(ARTIFACTS_DIR_ENV)
    if override is not None:
        if not override or not Path(override).is_absolute():
            raise ValueError(
                "artifacts_dir_not_absolute: CAO_ARTIFACTS_DIR must be an absolute path"
            )
        artifact_root = Path(override).resolve()
    else:
        artifact_root = Path(working_directory or os.getcwd()).resolve() / "tmp" / "orch"
    result[ARTIFACTS_DIR_ENV] = str(artifact_root)
    return result


def finalize_session(
    session_name: str, registry: PluginRegistry | None = None, backend=None
) -> None:
    """Kill/verify a backend session and settle shared session-level side effects."""
    backend = backend or get_backend()
    if backend.session_exists(session_name):
        backend.kill_session(session_name)
    for attempt in range(SESSION_TEARDOWN_VERIFY_ATTEMPTS):
        if not backend.session_exists(session_name):
            break
        backend.kill_session(session_name)
        if attempt < SESSION_TEARDOWN_VERIFY_ATTEMPTS - 1:
            time.sleep(SESSION_TEARDOWN_VERIFY_DELAY_SECONDS)
    if backend.session_exists(session_name):
        raise RuntimeError(f"Session '{session_name}' still exists after teardown")
    clear_session_env(session_name)
    dispatch_plugin_event(
        registry,
        "post_kill_session",
        PostKillSessionEvent(session_id=session_name, session_name=session_name),
    )


async def create_session(
    provider: str | None,
    agent_profile: str,
    session_name: str | None = None,
    working_directory: str | None = None,
    allowed_tools: list[str] | None = None,
    registry: PluginRegistry | None = None,
    env_vars: dict[str, str] | None = None,
    allow_incomplete_brief: bool = False,
) -> Terminal:
    """Create a new session by creating its initial terminal.

    ``env_vars`` are operator-forwarded env vars from ``cao launch --env``.
    They are persisted on the session record so every worker spawned later
    in the same session inherits them. See issue #248.
    """
    if provider is None:
        resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
    else:
        resolved_provider = provider
    require_provider_admitted(resolved_provider)

    session_env = canonical_session_env(working_directory, env_vars)

    from cli_agent_orchestrator.constants import SESSION_PREFIX

    effective_session_name = session_name or generate_session_name()
    if not effective_session_name.startswith(SESSION_PREFIX):
        effective_session_name = f"{SESSION_PREFIX}{effective_session_name}"
    try:
        profile = load_agent_profile(agent_profile)
    except FileNotFoundError:
        profile = None
    mailbox_claim = None
    if profile is not None and profile.role == "supervisor":
        from cli_agent_orchestrator.services.mailbox_service import claim_mailbox

        mailbox_claim = claim_mailbox(effective_session_name, "supervisor")

    from cli_agent_orchestrator.services.terminal_service import seed_resume_bootstrap

    fork_context = seed_resume_bootstrap(
        agent_profile, resolved_provider, working_directory or os.getcwd()
    )
    terminal = await create_terminal(
        provider=resolved_provider,
        agent_profile=agent_profile,
        session_name=effective_session_name,
        new_session=True,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
        registry=registry,
        env_vars=session_env,
        allow_incomplete_brief=allow_incomplete_brief,
        fork_context=fork_context,
    )
    if mailbox_claim is not None:
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.services.mailbox_service import (
            PublicationCleanupFailed,
            publish_supervisor_incarnation,
        )

        try:
            publication = await asyncio.to_thread(
                publish_supervisor_incarnation, mailbox_claim, terminal.id
            )
        except Exception as cause:
            try:
                deleted = await asyncio.to_thread(
                    __import__(
                        "cli_agent_orchestrator.services.terminal_service",
                        fromlist=["delete_terminal"],
                    ).delete_terminal,
                    terminal.id,
                    registry,
                )
                if not deleted and get_terminal_metadata(terminal.id) is not None:
                    raise RuntimeError("terminal retained")
            except Exception as cleanup_error:
                raise PublicationCleanupFailed(cause) from cleanup_error
            raise
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        await asyncio.to_thread(
            inbox_service.deliver_pending,
            terminal.id,
            registry=registry,
        )
        logger.info(
            "published supervisor mailbox %s generation %s",
            publication["mailbox_id"],
            publication["generation"],
        )
    dispatch_plugin_event(
        registry,
        "post_create_session",
        PostCreateSessionEvent(
            session_id=terminal.session_name,
            session_name=terminal.session_name,
        ),
    )
    return terminal


async def start_session(**kwargs) -> dict:
    """Canonical lifecycle start over the existing create-session transaction."""
    provider = kwargs.get("provider")
    profile = kwargs["agent_profile"]
    resolved = provider or resolve_provider(profile, fallback_provider="kiro_cli")
    require_provider_admitted(resolved)
    from cli_agent_orchestrator.providers.manager import get_provider_class

    seed_mode = get_provider_class(resolved).supports_seed_resume_identity is True
    terminal = await create_session(**kwargs)
    manifest = None
    manifest_error = None
    try:
        from cli_agent_orchestrator.services.session_manifest_service import build_session_manifest

        manifest = build_session_manifest(terminal.session_name)
    except Exception:
        manifest_error = "build_failed"
    return {
        "schema_version": "cao.session-start/v1",
        "session": {"name": terminal.session_name},
        "supervisor_terminal": terminal.model_dump(mode="json"),
        "bootstrap": {
            "mode": "seed_resume" if seed_mode else "not_applicable",
            "status": "seeded" if seed_mode else "not_required",
            **({"session_uuid": terminal.provider_session_id} if seed_mode else {}),
        },
        "manifest": manifest,
        "manifest_error": manifest_error,
    }


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        tmux_sessions = get_backend().list_sessions()
        return [s for s in tmux_sessions if s["id"].startswith(SESSION_PREFIX)]
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals."""
    try:
        if not get_backend().session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        tmux_sessions = get_backend().list_sessions()
        session_data = next((s for s in tmux_sessions if s["id"] == session_name), None)

        if not session_data:
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)

        # Enrich each terminal with its live status. list_terminals_by_session
        # reads only the DB row (no status column), but callers monitoring an
        # orchestration — the web UI, and the cao-ops-mcp get_session_info tool
        # an external supervisor polls — need to distinguish
        # IDLE/PROCESSING/COMPLETED/ERROR per terminal. status_monitor is the
        # single source of truth and is backend-aware (tmux push vs herdr
        # native), so derive it here rather than persisting a stale column.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        for terminal in terminals:
            terminal["status"] = status_monitor.get_status(terminal["id"]).value
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(
    session_name: str, registry: PluginRegistry | None = None, force: bool = False
) -> Dict:
    """Delete session and cleanup.

    Returns:
        Dict with 'deleted' (list of deleted session names) and 'errors' (list of error dicts).
    """
    result: Dict = {"deleted": [], "errors": []}
    leases = []
    lifecycle_lease = None
    try:
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.rebind_lease import (
            acquire_rebind_lease,
            release_rebind_lease,
        )
        from cli_agent_orchestrator.services.session_lifecycle_lease import (
            acquire_session_lifecycle_exclusive,
        )

        terminal_service.quiesce_deferred_session_sync(session_name)
        lifecycle_lease = acquire_session_lifecycle_exclusive(session_name)
        if lifecycle_lease is None:
            raise RuntimeError("resume_in_progress")

        terminals = list_terminals_by_session(session_name)

        from cli_agent_orchestrator.services.terminal_guard_service import require_delete_allowed

        for terminal in terminals:
            require_delete_allowed(terminal["id"], force=force)

        for terminal in sorted(terminals, key=lambda row: row["id"]):
            token = acquire_rebind_lease(terminal["id"])
            if token is None:
                for held in reversed(leases):
                    release_rebind_lease(held)
                raise RuntimeError("rebind_in_progress")
            leases.append(token)

        terminal_service.preflight_session_teardown(terminals)

        # Clean up each terminal (snapshot, kill window, FIFO reader,
        # status buffer, provider, DB) via the event-driven teardown path.
        tokens = {token.terminal_id: token for token in leases}
        for terminal in terminals:
            try:
                terminal_service._delete_terminal_under_lease(
                    terminal["id"], tokens[terminal["id"]], registry=registry
                )
            except Exception as e:
                if str(e) == "resume_in_progress":
                    raise
                logger.warning(f"Failed to cleanup terminal {terminal['id']}: {e}")

        finalize_session(session_name, registry)

        for token in reversed(leases):
            release_rebind_lease(token)
        leases.clear()

        from cli_agent_orchestrator.services.session_lifecycle_lease import (
            release_session_lifecycle_lease,
        )

        release_session_lifecycle_lease(lifecycle_lease)
        lifecycle_lease = None

        result["deleted"].append(session_name)
        logger.info(f"Deleted session: {session_name}")
        return result

    except Exception as e:
        if leases:
            from cli_agent_orchestrator.services.rebind_lease import release_rebind_lease

            for token in reversed(leases):
                try:
                    release_rebind_lease(token)
                except Exception:
                    pass
        if lifecycle_lease is not None:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            release_session_lifecycle_lease(lifecycle_lease)
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
