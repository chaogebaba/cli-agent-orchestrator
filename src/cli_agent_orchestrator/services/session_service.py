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
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
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
        base = Path(working_directory or os.getcwd()).resolve()
        orch_sub = base / "orchestrator"
        if orch_sub.is_dir():
            artifact_root = orch_sub / "tmp" / "orch"
        else:
            artifact_root = base / "tmp" / "orch"
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


async def _reconcile_inbox_path_on_publish(
    *, terminal_id: str, mailbox_id: str, generation: int
) -> None:
    """F136-D5: Wire set_supervisor_callback_inbox_path at publication lifecycle.

    Reads cc_team_inbox_path from terminal metadata and reconciles it as the
    canonical callback inbox path for the mailbox. Idempotent no-op when the
    path is unchanged or absent.
    """
    from cli_agent_orchestrator.clients.database import get_terminal_metadata
    from cli_agent_orchestrator.services.mailbox_service import set_supervisor_callback_inbox_path

    try:
        meta_record = get_terminal_metadata(terminal_id)
        if not meta_record:
            return
        md = meta_record.get("metadata") or {}
        candidate_path = md.get("cc_team_inbox_path")
        if not candidate_path:
            return
        await asyncio.to_thread(
            set_supervisor_callback_inbox_path,
            mailbox_id=mailbox_id,
            terminal_id=terminal_id,
            generation=generation,
            path=candidate_path,
        )
    except Exception as exc:
        logger.debug("inbox path reconciliation skipped: %s", exc)


async def _unwind_registered_terminal(
    terminal_id: str,
    registry: PluginRegistry | None,
) -> None:
    """F360 (#215): best-effort unwind of a terminal registered mid-create.

    ``create_session`` allocates and fully registers the terminal id (DB row,
    FIFO reader, StatusMonitor state, tmux window/session) inside
    ``create_terminal``. Any failure AFTER that point must deregister it before
    the error propagates, or the API caller sees a 500 while StatusMonitor
    keeps chasing a ghost id whose DB row no longer resolves.
    """
    from cli_agent_orchestrator.services.status_monitor import status_monitor
    from cli_agent_orchestrator.services.terminal_service import delete_terminal

    try:
        await asyncio.to_thread(delete_terminal, terminal_id, registry)
    except Exception as exc:
        # ValueError("Terminal ... not found") means an earlier cleanup
        # (e.g. the publication-failure path) already unwound the row — that
        # is success, not a leak. Anything else is logged and survived; the
        # StatusMonitor ghost-id drop (F360) is the backstop.
        logger.warning(
            "session_create_unwind_delete_failed terminal=%s: %s", terminal_id, exc
        )
    try:
        status_monitor.unregister(terminal_id)
    except Exception as exc:
        logger.warning(
            "session_create_unwind_monitor_failed terminal=%s: %s", terminal_id, exc
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
    engine: KiroEngine | str | None = None,
    initial_message: str | None = None,
    initial_message_orchestration_type: OrchestrationType | None = None,
    model: str | None = None,
    lifecycle: str | None = None,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Terminal:
    """Create a new session by creating its initial terminal.

    ``env_vars`` are operator-forwarded env vars from ``cao launch --env``.
    They are persisted on the session record so every worker spawned later
    in the same session inherits them. See issue #248.

    When ``initial_message`` is provided, the initial terminal uses the
    existing deferred-init path so provider initialization and delivery can
    continue after the session response. Omitting it preserves the synchronous
    initialization behavior used by existing callers.
    On the deferred path, the ``post_create_session`` plugin event is dispatched
    before provider initialization and message delivery finish.

    ``group``/``metadata`` are the #432 discovery fields, set on the initial
    terminal at creation time (``group`` is also updatable later via
    ``PATCH /terminals/{id}/group``, ``metadata`` via the ``update_metadata``
    MCP tool).
    """
    if initial_message == "":
        raise ValueError("initial_message must not be empty")
    if initial_message is None and initial_message_orchestration_type is not None:
        raise ValueError("initial_message_orchestration_type requires initial_message")

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

    fork_context = await seed_resume_bootstrap(
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
        engine=engine,
        defer_init=initial_message is not None,
        initial_message=initial_message,
        initial_message_orchestration_type=initial_message_orchestration_type,
        model=model,
        lifecycle=lifecycle,
        group=group,
        metadata=metadata,
    )
    # F360 (#215): the terminal id is now allocated and fully registered. Any
    # exception from here on unwinds that registration (deregister + monitor
    # removal) before propagating, so a failed create leaves no terminal row
    # or StatusMonitor entry behind.
    try:
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
                    from cli_agent_orchestrator.services.terminal_service import delete_terminal

                    deleted = await asyncio.to_thread(
                        delete_terminal,
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
            # F136-D5: reconcile inbox path from terminal metadata at publication
            await _reconcile_inbox_path_on_publish(
                terminal_id=terminal.id,
                mailbox_id=publication["mailbox_id"],
                generation=publication["generation"],
            )
        dispatch_plugin_event(
            registry,
            "post_create_session",
            PostCreateSessionEvent(
                session_id=terminal.session_name,
                session_name=terminal.session_name,
            ),
        )
    except Exception:
        await _unwind_registered_terminal(terminal.id, registry)
        raise
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


def _enrich_session_ownership(
    backend: TerminalBackend, session_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Add best-effort ownership metadata from the session's first known terminal."""
    enriched = dict(session_data)
    enriched.setdefault("working_directory", None)
    enriched.setdefault("agent_profile", None)

    # `... or ""` (not `.get("id", "")`): an explicit id=None must collapse to
    # "" too, matching the sibling guard in list_sessions. `.get("id", "")`
    # would yield the truthy string "None" and try to enrich a bogus session.
    session_name = enriched.get("id") or ""
    if not session_name:
        return enriched

    try:
        terminals = list_terminals_by_session(session_name)
    except Exception as e:
        logger.warning(f"Failed to load terminal metadata for {session_name}: {e}")
        terminals = []

    ownership_terminal: Dict[str, Any] = {}
    for terminal in terminals:
        if terminal.get("agent_profile") or terminal.get("working_directory"):
            ownership_terminal = terminal
            break

    if not ownership_terminal:
        for terminal in terminals:
            if terminal.get("tmux_window"):
                ownership_terminal = terminal
                break

    if ownership_terminal:
        enriched["agent_profile"] = ownership_terminal.get("agent_profile")
        persisted_working_directory = ownership_terminal.get("working_directory")
        if persisted_working_directory:
            enriched["working_directory"] = persisted_working_directory
        elif ownership_terminal.get("tmux_window"):
            try:
                enriched["working_directory"] = backend.get_pane_working_directory(
                    session_name, ownership_terminal["tmux_window"]
                )
            except Exception as e:
                logger.warning(f"Failed to resolve working directory for {session_name}: {e}")

    return enriched


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        backend = get_backend()
        tmux_sessions = backend.list_sessions()
        return [
            _enrich_session_ownership(backend, s)
            for s in tmux_sessions
            # Use .get() rather than s["id"]: a backend that returns a session
            # dict without an "id" key must not blank the entire list (KeyError
            # in this comprehension is swallowed by the outer except and returns
            # []). Shipped backends always populate "id"; this hardens against a
            # future backend that does not.
            if (s.get("id") or "").startswith(SESSION_PREFIX)
        ]
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
                result_or_false = terminal_service._delete_terminal_under_lease(
                    terminal["id"], tokens[terminal["id"]], registry=registry
                )
                # Deferred cleanup: provider returned False, row retained
                if result_or_false is False or (
                    isinstance(result_or_false, dict) and not result_or_false.get("terminal_deleted", True)
                ):
                    result["errors"].append({
                        "terminal_id": terminal["id"],
                        "error": "cleanup deferred; retry delete_session",
                    })
            except Exception as e:
                if str(e) == "resume_in_progress":
                    raise
                logger.warning(f"Failed to cleanup terminal {terminal['id']}: {e}")

        if not result["errors"]:
            finalize_session(session_name, registry)
        else:
            # Kill the tmux session even if cleanup is deferred — this stops
            # the processes so a subsequent retry can complete the cleanup.
            backend = get_backend()
            if backend.session_exists(session_name):
                backend.kill_session(session_name)

        for token in reversed(leases):
            release_rebind_lease(token)
        leases.clear()

        from cli_agent_orchestrator.services.session_lifecycle_lease import (
            release_session_lifecycle_lease,
        )

        release_session_lifecycle_lease(lifecycle_lease)
        lifecycle_lease = None

        if not result["errors"]:
            result["deleted"].append(session_name)
            logger.info(f"Deleted session: {session_name}")
        else:
            logger.warning(f"Session {session_name} has deferred cleanups; not fully deleted")
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
