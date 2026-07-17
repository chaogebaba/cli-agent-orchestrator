"""Terminal service with workflow functions.

This module provides high-level terminal management operations that orchestrate
multiple components (database, tmux, providers) to create a unified terminal
abstraction for CLI agents.

Key Responsibilities:
- Terminal lifecycle management (create, get, delete)
- Provider initialization and cleanup
- Tmux session/window management
- Terminal output capture and message extraction

Terminal Workflow:
1. create_terminal() → Creates tmux window, initializes provider, starts logging
2. send_input() → Sends user message to the agent via tmux
3. get_output() → Retrieves agent response from terminal history
4. delete_terminal() → Cleans up provider, database record, and logging
"""

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, cast

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    claim_deferred_init_failure,
    create_inbox_message,
)
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import (
    create_terminal_with_warm_intent,
    delete_terminal_and_warm_intent,
    get_ready_provider_session,
    get_terminal_metadata,
)
from cli_agent_orchestrator.clients.database import list_all_terminals as db_list_all_terminals
from cli_agent_orchestrator.clients.database import (
    list_deferred_init_recovery_rows,
    list_terminals_by_provider_session_id,
    list_terminals_by_session,
    mark_terminal_init_ready,
    settle_pending_orphan_messages,
    terminal_exists,
    update_last_active,
    update_provider_session_snapshot,
    update_terminal_shell_command,
)
from cli_agent_orchestrator.constants import FIFO_DIR, SESSION_PREFIX, TERMINAL_LOG_DIR
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateTerminalEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.providers.base import (
    RetryableArtifactValidation,
    TerminalArtifactValidation,
)
from cli_agent_orchestrator.providers.manager import get_provider_class, provider_manager
from cli_agent_orchestrator.services.deferred_dispatcher import (
    DeferredCall,
    DeferredExecutorSaturated,
    dispatcher,
)
from cli_agent_orchestrator.services.draft_guard import (
    preserve_draft_before_send,
    stash_draft_before_send,
)
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.fork_context_service import snapshot as fork_snapshot
from cli_agent_orchestrator.services.fork_context_service import staleness as fork_staleness
from cli_agent_orchestrator.services.herdr_inbox_registry import get_herdr_inbox_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path
from cli_agent_orchestrator.utils.provider_auth import ProviderAuthRefreshFailed
from cli_agent_orchestrator.utils.provider_plane import NativeHomeIsolationUnavailable
from cli_agent_orchestrator.utils.sandbox_guard import bind_pane_identity, require_provider_admitted
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
)

logger = logging.getLogger(__name__)

# Track terminals that have already received memory injection (first message only).
_memory_injected_terminals: set = set()
_memory_injected_lock = threading.Lock()

# Strong references to in-flight deferred-init background tasks. asyncio keeps
# only a WEAK reference to tasks from loop.create_task, so without this a
# deferred provider.initialize() + input-send task could be GC'd mid-run,
# silently leaving a worker uninitialized. Tasks drop themselves on completion.
_deferred_init_tasks: set = set()
_deferred_reconciler_tasks: set[asyncio.Task] = set()
_deferred_tasks_lock = threading.Lock()

POLL_INTERVAL = 2.0
DEFERRED_TASK_QUIESCE_S = 10.0
FORK_REFRESH_WAIT_BUDGET = 120.0
SERVER_INIT_OWNER_EPOCH = str(uuid.uuid4())


@dataclass
class _DeferredTaskRecord:
    task: asyncio.Task
    loop: asyncio.AbstractEventLoop
    generation: str
    session_name: str | None = None
    current_call: DeferredCall | None = None
    abandoned: bool = False


_deferred_tasks_by_terminal: dict[str, _DeferredTaskRecord] = {}
_fork_refresh_locks: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}


class TerminalInputBlockedError(Exception):
    """Raised when orchestrated input would answer an active interactive prompt."""


def seed_resume_bootstrap(agent_profile: str, provider_name: str, cwd: str):
    """Return an authoritative resume ForkContext for seed-capable providers."""
    provider_class = get_provider_class(provider_name)
    if provider_class.supports_seed_resume_identity is not True:
        return None
    from cli_agent_orchestrator.models.terminal import ForkContext

    session_uuid = provider_class.seed_resume_identity(cwd, agent_profile)
    return ForkContext(
        mode="resume",
        session_uuid=session_uuid,
        base_name="seed",
        provider=provider_name,
        initial_preamble="",
    )


def has_deferred_init(terminal_id: str) -> bool:
    with _deferred_tasks_lock:
        record = _deferred_tasks_by_terminal.get(terminal_id)
        return record is not None and not record.task.done()


@dataclass(frozen=True)
class _PreparedRuntimeIdentity:
    session_uuid: str
    cwd: str
    shell: str
    settlement_form: str


def _prepare_provider_runtime_identity(
    provider_instance,
    terminal_id: str,
    *,
    settlement_form: str,
) -> _PreparedRuntimeIdentity | None:
    """Perform one-time blocking capture without validating or persisting."""
    if getattr(provider_instance, "supports_reauth_rebind", False) is not True:
        shell = provider_instance.shell_baseline
        if isinstance(shell, str) and shell:
            update_terminal_shell_command(terminal_id, shell)
        return None
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        raise RuntimeError("terminal_metadata_missing")
    from cli_agent_orchestrator.services.fork_context_service import pane_launch_epoch, pane_pid

    pid = pane_pid(metadata["tmux_session"], metadata["tmux_window"])
    cwd = get_backend().get_pane_working_directory(
        metadata["tmux_session"], metadata["tmux_window"]
    )
    allocated = getattr(provider_instance, "allocated_session_uuid", None)
    try:
        hint = provider_instance.resume_session_uuid()
    except Exception as exc:
        raise RuntimeError("identity_persist_failed") from exc
    if hint is not None and not isinstance(hint, str):
        raise RuntimeError("identity_persist_failed")
    session_uuid = (
        allocated
        or hint
        or provider_instance.capture_session_uuid(pid, pane_launch_epoch(pid), cwd)
    )
    shell = provider_instance.shell_baseline or metadata.get("shell_command")
    if not shell:
        raise RuntimeError("shell_baseline_unavailable")
    return _PreparedRuntimeIdentity(session_uuid, cwd, shell, settlement_form)


def _commit_provider_runtime_identity(
    terminal_id: str,
    prepared: _PreparedRuntimeIdentity,
) -> None:
    from cli_agent_orchestrator.clients.database import update_terminal_runtime_identity

    if prepared.settlement_form == "resume":
        persisted = update_terminal_runtime_identity(
            terminal_id,
            prepared.session_uuid,
            prepared.shell,
            supersede_other_claims=True,
        )
    elif prepared.settlement_form == "fallback":
        persisted = update_terminal_runtime_identity(
            terminal_id,
            prepared.session_uuid,
            prepared.shell,
            require_published_uuid=True,
        )
    else:
        persisted = update_terminal_runtime_identity(
            terminal_id, prepared.session_uuid, prepared.shell
        )
    if not persisted:
        raise RuntimeError("terminal_identity_persist_failed")


def _persist_provider_runtime_identity(
    provider_instance,
    terminal_id: str,
    *,
    settlement_form: str = "first_time",
) -> None:
    """Persist resumable identity after init and before initial task delivery."""
    prepared = _prepare_provider_runtime_identity(
        provider_instance,
        terminal_id,
        settlement_form=settlement_form,
    )
    if prepared is None:
        return
    provider_instance.validate_session_artifact(prepared.session_uuid, prepared.cwd)
    _commit_provider_runtime_identity(terminal_id, prepared)


def purge_stale_terminal_records() -> int:
    """Delete DB terminal records whose backend window no longer exists."""
    backend = get_backend()
    purged = 0
    for metadata in db_list_all_terminals():
        terminal_id = metadata["id"]
        if metadata.get("init_state") != "ready":
            logger.warning(
                "stale_terminal_cleanup_skipped_non_ready terminal=%s init_state=%r",
                terminal_id,
                metadata.get("init_state"),
            )
            continue
        try:
            backend.get_history(
                metadata["tmux_session"],
                metadata["tmux_window"],
                tail_lines=1,
            )
        except Exception:
            if delete_terminal_and_warm_intent(terminal_id, preserve_warm_intent=False)[
                "terminal_deleted"
            ]:
                settlement = settle_pending_orphan_messages(receiver_ids=[terminal_id])
                if settlement.busy_aborted:
                    logger.warning("stale_terminal_p5_settlement_busy terminal=%s", terminal_id)
                purged += 1
                logger.debug(
                    "Purged stale terminal record %s for missing window %s:%s",
                    terminal_id,
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                )
    return purged


def inject_memory_context(first_message: str, terminal_id: str, *, consume: bool = True) -> str:
    """Prepend <cao-memory> context block to the first user message.

    Tracks which terminals have already been injected so that only the very
    first user message after init receives the memory block.

    Calls MemoryService.get_memory_context_for_terminal() which returns
    a formatted <cao-memory>...</cao-memory> block (or empty string if
    no memories exist). Stateless — no file mutation, no backup/restore.
    """
    with _memory_injected_lock:
        if terminal_id in _memory_injected_terminals:
            return first_message
        if consume:
            _memory_injected_terminals.add(terminal_id)

    try:
        svc = MemoryService()
        context = svc.get_curated_memory_context(terminal_id, task_description=first_message[:200])
        if context:
            return context + "\n\n" + first_message
    except Exception as e:
        logger.warning(f"Failed to inject memory context for terminal {terminal_id}: {e}")
    return first_message


class OutputMode(str, Enum):
    """Output mode for terminal history retrieval.

    FULL: Returns complete terminal output (scrollback buffer)
    LAST: Returns only the last agent response (extracted by provider)
    """

    FULL = "full"
    LAST = "last"


# Providers that accept a runtime skill_prompt kwarg and append it to the
# system prompt at launch time.  Other providers deliver skills differently:
# Kiro (skill:// resources) and OpenCode (OPENCODE_CONFIG_DIR/skills symlink)
# discover skills natively; Copilot receives a baked catalog at install
# time.
RUNTIME_SKILL_PROMPT_PROVIDERS = {
    ProviderType.CLAUDE_CODE.value,
    ProviderType.CODEX.value,
    ProviderType.GROK_CLI.value,
    ProviderType.KIMI_CLI.value,
    ProviderType.ANTIGRAVITY_CLI.value,
}

SESSION_BRIEF_MARKER = "SESSION BRIEF UNAVAILABLE — world-model incomplete"


def _rollback_terminal_creation(
    terminal_id: str,
    session_name: str | None,
    window_name: str | None,
    session_created: bool,
    window_created: bool,
    fifo_attached: bool,
    db_created: bool,
) -> None:
    """Single rollback seam preserving pipe-pane -> FIFO -> window/session order."""
    if db_created:
        try:
            delete_terminal_and_warm_intent(terminal_id, preserve_warm_intent=False)
        except Exception as exc:
            logger.error(
                "create_rollback_cleanup_failed terminal=%s error=%s",
                terminal_id,
                type(exc).__name__,
            )
    try:
        if fifo_attached and session_name and window_name:
            get_backend().stop_pipe_pane(session_name, window_name)
    except Exception:
        pass
    try:
        if fifo_attached:
            fifo_manager.stop_reader(terminal_id)
    except Exception:
        pass
    try:
        if session_created and session_name:
            get_backend().kill_session(session_name)
            clear_session_env(session_name)
        elif window_created and session_name and window_name:
            get_backend().kill_window(session_name, window_name)
    except Exception:
        pass


def _settle_published_creation_failure(
    terminal_id: str,
    session_uuid: str,
    uuid_lease_token,
    registry: PluginRegistry | None,
    *,
    existing_rebind_lease=None,
) -> dict:
    """Settle a provisional resume owner truthfully under the global lock order."""
    from cli_agent_orchestrator.clients.database import quarantine_terminal_owner
    from cli_agent_orchestrator.services.rebind_lease import (
        acquire_rebind_lease,
        release_rebind_lease,
    )

    lease = existing_rebind_lease
    acquired_here = False
    if lease is None:
        # A public teardown may momentarily own the new-terminal lease. It will
        # observe resume_in_progress and release; retry without dropping UUID
        # authority or claiming a deletion that has not settled.
        for _ in range(100):
            lease = acquire_rebind_lease(terminal_id)
            if lease is not None:
                acquired_here = True
                break
            time.sleep(0.01)
    if lease is None:
        if get_terminal_metadata(terminal_id) is None:
            return {"status": "deleted", "error_code": None}
        try:
            quarantine_terminal_owner(terminal_id, session_uuid, "rollback_kill_uncertain")
        except Exception as exc:
            raise RuntimeError("quarantine_persist_failed") from exc
        return {"status": "retained", "error_code": "rollback_kill_uncertain"}
    try:
        outcome = _delete_terminal_under_lease(
            terminal_id,
            lease,
            registry=registry,
            require_confirmed_death=True,
            quarantine_session_uuid=session_uuid,
            uuid_lease_token=uuid_lease_token,
        )
        if outcome.get("rollback_kill_uncertain"):
            return {"status": "retained", "error_code": "rollback_kill_uncertain"}
        return {"status": "deleted", "error_code": None}
    finally:
        if acquired_here:
            release_rebind_lease(lease)


# Providers whose tool restrictions are prompt-level text only (no native
# blocking mechanism) — a restricted policy on these is advisory, not enforced.
SOFT_ENFORCEMENT_PROVIDERS = {
    ProviderType.KIMI_CLI.value,
    ProviderType.CODEX.value,
    ProviderType.ANTIGRAVITY_CLI.value,
}

MAX_PEEK_TERMINAL_LINES = 200


def _append_message_contract(message: str, metadata: Dict, orchestration_value: str) -> str:
    """Append a profile-declared contract to CAO-orchestrated deliveries."""
    if orchestration_value not in {
        OrchestrationType.ASSIGN.value,
        OrchestrationType.SEND_MESSAGE.value,
        OrchestrationType.HANDOFF.value,
    }:
        return message

    profile_name = metadata.get("agent_profile")
    if not profile_name:
        return message

    try:
        profile = load_agent_profile(profile_name)
    except Exception:
        return message
    if not profile.messageContract:
        return message
    return f"{message}\n\n[Contract: {profile.messageContract}]"


def _acquire_resume_creation_authority(
    session_name: str,
    resume_uuid: str,
    uuid_lease_token,
    session_lifecycle_lease_token,
    fallback_source_terminal_id,
    fallback_source_lease_token,
):
    """Acquire and preflight resume authority, releasing local tokens on any error."""
    from cli_agent_orchestrator.services.provider_session_lease import (
        acquire_provider_session_lease,
        release_provider_session_lease,
        validate_provider_session_lease,
    )
    from cli_agent_orchestrator.services.session_lifecycle_lease import (
        acquire_session_lifecycle_shared,
        release_session_lifecycle_lease,
        validate_session_lifecycle_shared,
    )

    owned_lifecycle = False
    owned_uuid = False
    try:
        if session_lifecycle_lease_token is None:
            session_lifecycle_lease_token = acquire_session_lifecycle_shared(session_name)
            if session_lifecycle_lease_token is None:
                raise RuntimeError("resume_in_progress")
            owned_lifecycle = True
        else:
            validate_session_lifecycle_shared(session_name, session_lifecycle_lease_token)
        if uuid_lease_token is None:
            uuid_lease_token = acquire_provider_session_lease(resume_uuid)
            if uuid_lease_token is None:
                raise RuntimeError("resume_in_progress")
            owned_uuid = True
        else:
            validate_provider_session_lease(resume_uuid, uuid_lease_token)

        owners = list_terminals_by_provider_session_id(resume_uuid)
        if fallback_source_terminal_id:
            from cli_agent_orchestrator.services.rebind_lease import validate_rebind_lease

            try:
                validate_rebind_lease(fallback_source_terminal_id, fallback_source_lease_token)
                source = get_terminal_metadata(fallback_source_terminal_id)
                if (
                    not source
                    or source.get("provider_session_id") != resume_uuid
                    or source.get("recovery_state") != "fallback_starting"
                ):
                    raise RuntimeError("owner_conflict")
            except Exception as exc:
                raise RuntimeError("owner_conflict") from exc
            owners = [row for row in owners if row["id"] != fallback_source_terminal_id]
        for owner in owners:
            try:
                state = get_backend().window_liveness(owner["tmux_session"], owner["tmux_window"])
            except Exception:
                state = "error"
            if state in {"live", "error"}:
                raise RuntimeError("owner_conflict")
        return (
            uuid_lease_token,
            owned_uuid,
            session_lifecycle_lease_token,
            owned_lifecycle,
        )
    except Exception:
        if owned_uuid:
            release_provider_session_lease(uuid_lease_token)
        if owned_lifecycle:
            release_session_lifecycle_lease(session_lifecycle_lease_token)
        raise


async def create_terminal(
    provider: str,
    agent_profile: str,
    session_name: Optional[str] = None,
    new_session: bool = False,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    registry: PluginRegistry | None = None,
    env_vars: Optional[dict[str, str]] = None,
    caller_id: Optional[str] = None,
    defer_init: bool = False,
    initial_message: Optional[str] = None,
    initial_message_orchestration_type: Optional[OrchestrationType] = None,
    fork_context=None,
    refresh_base_name: Optional[str] = None,
    allow_incomplete_brief: bool = False,
    terminal_id: Optional[str] = None,
    lease_token=None,
    strict_backend_registration: bool = False,
    uuid_lease_token=None,
    session_lifecycle_lease_token=None,
    fallback_source_terminal_id: str | None = None,
    fallback_source_lease_token=None,
) -> Terminal:
    """Create a new terminal with an initialized CLI agent.

    This function orchestrates the complete terminal creation workflow:
    1. Generate unique terminal ID and window name
    2. Create tmux session/window (new or existing)
    3. Save terminal metadata to database
    4. Initialize the CLI provider (starts the agent)
    5. Set up terminal logging via tmux pipe-pane

    Args:
        provider: Provider type string (e.g., "kiro_cli", "claude_code")
        agent_profile: Name of the agent profile to use
        session_name: Optional custom session name. If not provided, auto-generated.
        new_session: If True, creates a new tmux session. If False, adds to existing.
        working_directory: Optional working directory for the terminal shell
        env_vars: Operator-forwarded env vars (``cao launch --env``). On
            ``new_session=True``, these are stored on the session record and
            inherited by every worker spawned later in the same session. On
            ``new_session=False``, persisted session vars provide the shared
            session floor and explicit ``env_vars`` are overlaid for the new
            window only, with explicit values winning on collision. The overlay
            is not persisted for later windows. See issue #248.
        caller_id: Terminal ID of the supervisor that created this terminal
            via handoff/assign. Recorded so send_message can route callbacks
            structurally instead of parsing IDs out of message text (issue #284).
            None for operator-launched terminals.

    Returns:
        Terminal object with all metadata populated

    Raises:
        ValueError: If session already exists (new_session=True) or not found (new_session=False)
        TimeoutError: If provider initialization times out
    """
    require_provider_admitted(provider)
    if working_directory is not None:
        if not os.path.isabs(os.path.expanduser(working_directory)):
            raise ValueError(
                f"invalid_working_directory: Working directory must be an absolute path: "
                f"{working_directory}"
            )
        try:
            working_directory = resolve_and_validate_path(
                working_directory, description="Working directory"
            )
        except ValueError as exc:
            raise ValueError(f"invalid_working_directory: {exc}") from exc
    provider_class = get_provider_class(provider)
    if provider_class.supports_seed_resume_identity is True and fork_context is None:
        raise RuntimeError("seed_required")
    resume_uuid = (
        fork_context.session_uuid
        if fork_context is not None and fork_context.mode == "resume"
        else None
    )
    if not session_name:
        session_name = generate_session_name()
    if new_session and not session_name.startswith(SESSION_PREFIX):
        session_name = f"{SESSION_PREFIX}{session_name}"
    owned_lifecycle_lease = False
    owned_uuid_lease = False
    if resume_uuid:
        (
            uuid_lease_token,
            owned_uuid_lease,
            session_lifecycle_lease_token,
            owned_lifecycle_lease,
        ) = _acquire_resume_creation_authority(
            session_name,
            resume_uuid,
            uuid_lease_token,
            session_lifecycle_lease_token,
            fallback_source_terminal_id,
            fallback_source_lease_token,
        )

    try:
        try:
            early_profile = load_agent_profile(agent_profile)
        except FileNotFoundError:
            early_profile = None
        candidate_brief_mode = early_profile.sessionBrief if early_profile else None
        brief_mode = (
            candidate_brief_mode if candidate_brief_mode in ("required", "optional") else None
        )
        if brief_mode and provider not in RUNTIME_SKILL_PROMPT_PROVIDERS:
            raise ValueError(
                f"sessionBrief requires a runtime-context provider; resolved provider={provider}"
            )
    except Exception:
        if owned_uuid_lease:
            from cli_agent_orchestrator.services.provider_session_lease import (
                release_provider_session_lease,
            )

            release_provider_session_lease(uuid_lease_token)
        if owned_lifecycle_lease:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            release_session_lifecycle_lease(session_lifecycle_lease_token)
        raise

    session_created = False  # tracks whether THIS call created the tmux session
    window_created = False
    fifo_attached = False
    db_created = False
    try:
        # Step 1: Generate unique identifiers
        terminal_id = terminal_id or generate_terminal_id()
        env_vars = bind_pane_identity(env_vars, terminal_id)
        if lease_token is not None:
            from cli_agent_orchestrator.services.rebind_lease import validate_rebind_lease

            validate_rebind_lease(terminal_id, lease_token)

        window_name = generate_window_name(agent_profile)

        # Step 2: Create tmux session or window
        if new_session:
            # Ensure session name has the CAO prefix for identification
            # Prevent duplicate sessions
            if get_backend().session_exists(session_name):
                raise ValueError(f"Session '{session_name}' already exists")

            # Wipe any stale mapping a prior aborted lifecycle for this name
            # may have left behind, so a no-env relaunch can't inherit them.
            clear_session_env(session_name)

            # Create new tmux session with initial window
            get_backend().create_session(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                extra_env=env_vars,
            )
            session_created = True  # only set after successful creation
            window_created = True

            # Persist forwarded env only after the tmux session actually
            # exists; the failure path below clears it if a later step
            # tears the session back down.
            if env_vars:
                set_session_env(session_name, env_vars)
        else:
            # Add window to existing session
            if not get_backend().session_exists(session_name):
                raise ValueError(f"Session '{session_name}' not found")
            session_floor = get_session_env(session_name)
            window_overlay = {
                key: value for key, value in (env_vars or {}).items() if key != "CAO_ARTIFACTS_DIR"
            }
            extra_env = {**session_floor, **window_overlay}
            try:
                window_name = get_backend().create_window(
                    session_name,
                    window_name,
                    terminal_id,
                    working_directory,
                    extra_env=extra_env,
                )
            except Exception as exc:
                if lease_token is not None:
                    raise RuntimeError("window_create_failed") from exc
                raise
            window_created = True

        # Step 3: Load the profile once for allowed tool resolution before
        # provider initialization. The skill catalog is computed only for
        # providers that consume it at launch time (see RUNTIME_SKILL_PROMPT_PROVIDERS).
        try:
            profile = load_agent_profile(agent_profile)
        except FileNotFoundError:
            profile = None
        skill_prompt = (
            build_skill_catalog(profile.skills if profile else None)
            if provider in RUNTIME_SKILL_PROMPT_PROVIDERS
            else None
        )
        # Step 3b: Resolve allowed_tools from profile if not explicitly provided
        if allowed_tools is None and profile is not None:
            from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

            mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
            allowed_tools = resolve_allowed_tools(
                profile.allowedTools, profile.role, mcp_server_names
            )

        # Soft-enforcement guard: kimi_cli/codex have NO native tool-blocking
        # mechanism (kimi runs --yolo; restrictions are prompt-level text
        # only), so a restricted policy on them is advisory, not enforced.
        # Surface that loudly at launch so operators route restricted or
        # write-capable roles to hard-enforcement providers instead.
        if provider in SOFT_ENFORCEMENT_PROVIDERS and allowed_tools and "*" not in allowed_tools:
            logger.warning(
                f"Terminal {terminal_id}: provider '{provider}' cannot enforce tool "
                f"restrictions (soft/prompt-level only) but profile '{agent_profile}' "
                f"requests {allowed_tools}. Treat this worker as unrestricted; for "
                f"enforced restrictions use claude_code, kiro_cli, or "
                f"copilot_cli."
            )

        # Step 4: Set up the FIFO event-driven output pipeline for pipe-pane
        # backends (tmux). Event-inbox backends (herdr) deliver via their own
        # socket events and their pipe_pane is a no-op, so skip the FIFO there and
        # rely on the herdr inbox registration below.
        if not get_backend().supports_event_inbox():
            # Reader must exist BEFORE pipe-pane starts so it captures from the start.
            try:
                fifo_manager.create_reader(terminal_id)
                fifo_attached = True
            except Exception as exc:
                if lease_token is not None:
                    raise RuntimeError("fifo_create_failed") from exc
                raise

            # Configure pipe-pane to stream output to the FIFO. This enables
            # real-time event-driven processing via StatusMonitor and LogWriter
            # (LogWriter writes TERMINAL_LOG_DIR/{id}.log from the FIFO). A pane
            # has a single pipe-pane target, so we pipe ONLY to the FIFO.
            fifo_path = FIFO_DIR / f"{terminal_id}.fifo"
            try:
                get_backend().pipe_pane(session_name, window_name, str(fifo_path))
            except Exception as exc:
                if lease_token is not None:
                    raise RuntimeError("fifo_create_failed") from exc
                raise

            # Nudge the shell so it re-renders its prompt AFTER pipe-pane attaches.
            # pipe-pane only captures output produced after it starts; on a fast
            # shell the initial prompt is drawn before the pipe attaches, leaving
            # the StatusMonitor buffer empty so wait_for_shell() times out. A bare
            # Enter produces a fresh prompt line that flows through the pipe.
            get_backend().send_special_key(session_name, window_name, "Enter")

        # Step 5: Persist terminal metadata after output capture is attached.
        # The manifest below then sees the new row, while rollback unwinds DB
        # before pipe-pane/FIFO/window in exact reverse acquisition order.
        try:
            from cli_agent_orchestrator.services.inbox_service import get_delivery_lock
            from cli_agent_orchestrator.services.mailbox_service import (
                get_mailbox_authority_lock,
            )

            init_fields = {}
            if defer_init:
                from cli_agent_orchestrator.services.settings_service import get_server_settings

                init_fields = {
                    "init_state": "init_pending",
                    "init_started_at": datetime.now(timezone.utc),
                    "init_owner_epoch": SERVER_INIT_OWNER_EPOCH,
                    "init_deadline_s": float(get_server_settings()["artifact_validate_deadline_s"]),
                }
            delivery_authority = get_delivery_lock(terminal_id)
            mailbox_authority = get_mailbox_authority_lock(session_name, "supervisor")
            with delivery_authority:
                with mailbox_authority:
                    if fork_context and fork_context.mode == "fork":
                        create_terminal_with_warm_intent(
                            terminal_id=terminal_id,
                            tmux_session=session_name,
                            tmux_window=window_name,
                            provider=provider,
                            agent_profile=agent_profile,
                            allowed_tools=allowed_tools,
                            caller_id=caller_id,
                            parent_base_name=fork_context.base_name,
                            fork_mode=fork_context.mode,
                            **init_fields,
                        )
                    else:
                        attempted_resume_uuid = resume_uuid
                        if attempted_resume_uuid:
                            db_create_terminal(
                                terminal_id,
                                session_name,
                                window_name,
                                provider,
                                agent_profile,
                                allowed_tools,
                                caller_id=caller_id,
                                provider_session_id=attempted_resume_uuid,
                                **init_fields,
                            )
                        else:
                            db_create_terminal(
                                terminal_id,
                                session_name,
                                window_name,
                                provider,
                                agent_profile,
                                allowed_tools,
                                caller_id=caller_id,
                                **init_fields,
                            )
        except Exception as exc:
            if lease_token is not None:
                raise RuntimeError("db_publish_failed") from exc
            raise
        db_created = True

        # The live snapshot is transactional launch context. Build it only after
        # the terminal row and output plumbing exist, so it includes itself and a
        # required-profile failure can unwind every preceding allocation.
        if brief_mode:
            from cli_agent_orchestrator.services.session_manifest_service import (
                build_session_manifest,
                core_sections_complete,
                render_session_brief,
            )

            relax = allow_incomplete_brief or os.environ.get("CAO_SESSION_BRIEF_RELAX") == "1"
            if os.environ.get("CAO_SESSION_BRIEF_RELAX") == "1":
                logger.warning("CAO_SESSION_BRIEF_RELAX=1: required session brief is best-effort")
            try:
                manifest = build_session_manifest(session_name, terminal_id)
                if brief_mode == "required" and not core_sections_complete(manifest) and not relax:
                    failed = [
                        name
                        for name in ("profiles", "skills")
                        if manifest["sections"].get(name) == "error"
                    ]
                    raise ValueError(
                        f"required session brief core section failed: {','.join(failed)}"
                    )
                brief = render_session_brief(manifest)
                if brief_mode == "required" and not manifest["complete"]:
                    brief = f"{SESSION_BRIEF_MARKER}\n\n{brief}"
                skill_prompt = f"{skill_prompt}\n\n{brief}" if skill_prompt else brief
            except Exception as exc:
                if lease_token is not None:
                    raise RuntimeError("context_build_failed") from exc
                if brief_mode == "required" and not relax:
                    raise
                if brief_mode == "required":
                    skill_prompt = (
                        f"{skill_prompt}\n\n{SESSION_BRIEF_MARKER}"
                        if skill_prompt
                        else SESSION_BRIEF_MARKER
                    )

        # Step 6: Create and initialize the CLI provider
        # This starts the agent (e.g., runs "kiro-cli chat --agent developer").
        # Only runtime-prompt providers (Claude Code, Codex, Kimi) receive
        # the skill catalog here; Kiro (skill:// resources) and OpenCode
        # (OPENCODE_CONFIG_DIR/skills symlink) discover skills natively;
        # Copilot gets the catalog baked at install time.
        try:
            provider_instance = provider_manager.create_provider(
                provider,
                terminal_id,
                session_name,
                window_name,
                agent_profile,
                allowed_tools,
                skill_prompt=skill_prompt,
                model=profile.model if profile else None,
                fork_context=fork_context,
            )
        except Exception as exc:
            if lease_token is not None:
                raise RuntimeError("provider_construct_failed") from exc
            raise
        allocated_uuid = getattr(provider_instance, "allocated_session_uuid", None)
        if not isinstance(allocated_uuid, str):
            allocated_uuid = None

        # Deferred-init path: return fast so callers (e.g. MCP assign) do not
        # block on `provider.initialize()`. The remaining initialize + input
        # send runs as a background task, so two concurrent assigns can each
        # kick off their init in parallel. Kiro-cli 2.11's per-tool client
        # timeout (~120s observed) previously cancelled assign RPCs when init
        # took long enough to push the round-trip past that cap; deferring init
        # keeps the tool call under 2s.
        if defer_init:
            shell_command = None  # unknown until initialize() runs
            if fork_context and initial_message and refresh_base_name is None:
                initial_message = f"{fork_context.initial_preamble}\n\n{initial_message}"
            published_snapshot = get_terminal_metadata(terminal_id)
            if published_snapshot is None:
                raise RuntimeError("terminal_metadata_missing")
            _schedule_deferred_init(
                provider_instance,
                terminal_id,
                initial_message,
                initial_message_orchestration_type,
                registry,
                uuid_lease_token=uuid_lease_token,
                owns_uuid_lease=owned_uuid_lease,
                session_lifecycle_lease_token=session_lifecycle_lease_token,
                owns_lifecycle_lease=owned_lifecycle_lease,
                settlement_form=(
                    "fallback"
                    if fallback_source_terminal_id
                    else "resume" if resume_uuid else "first_time"
                ),
                caller_snapshot=published_snapshot,
                fork_context=fork_context,
                refresh_base_name=refresh_base_name,
            )
        else:
            try:
                await provider_instance.initialize()
            except (NativeHomeIsolationUnavailable, ProviderAuthRefreshFailed):
                raise
            except TimeoutError:
                raise
            except Exception as exc:
                if lease_token is not None:
                    raise RuntimeError("initialize_failed") from exc
                raise
            try:
                _persist_provider_runtime_identity(
                    provider_instance,
                    terminal_id,
                    settlement_form=(
                        "fallback"
                        if fallback_source_terminal_id
                        else "resume" if resume_uuid else "first_time"
                    ),
                )
            except Exception as exc:
                if lease_token is None:
                    raise
                message = str(exc)
                if message in {"session_capture_ambiguous", "session_capture_mismatch"}:
                    raise
                if message.startswith("session_artifact_"):
                    raise RuntimeError("artifact_invalid") from exc
                raise RuntimeError("identity_persist_failed") from exc

            # Persist shell_command baseline if the provider captured one
            shell_command = provider_instance.shell_baseline
            if not isinstance(shell_command, str):
                shell_command = None
            if shell_command:
                update_terminal_shell_command(terminal_id, shell_command)

        # Build and return the Terminal object. In the deferred-init path the
        # provider is still initializing on a background task, so the terminal
        # is NOT ready for input yet — report UNKNOWN (not IDLE) so a client
        # can't mistake it for ready and send input early. Callers poll
        # GET /terminals/{id} for the live status once init completes. The
        # synchronous path has already reached IDLE by here.
        initial_status = TerminalStatus.UNKNOWN if defer_init else TerminalStatus.IDLE
        terminal = Terminal(
            id=terminal_id,
            name=window_name,
            provider=ProviderType(provider),
            session_name=session_name,
            agent_profile=agent_profile,
            caller_id=caller_id,
            allowed_tools=allowed_tools,
            shell_command=shell_command,
            status=initial_status,
            last_active=datetime.now(),
            provider_session_id=resume_uuid or allocated_uuid,
        )

        logger.info(
            f"Created terminal: {terminal_id} in session: {session_name} (new_session={new_session})"
        )
        dispatch_plugin_event(
            registry,
            "post_create_terminal",
            PostCreateTerminalEvent(
                session_id=terminal.session_name,
                terminal_id=terminal.id,
                agent_name=terminal.agent_profile,
                provider=provider,
            ),
        )

        # Register with herdr inbox service for message delivery
        svc = get_herdr_inbox_service()
        if svc:
            try:
                pane_id = get_backend().get_pane_id(terminal_id, session_name, window_name)
                is_kiro = provider == ProviderType.KIRO_CLI.value
                svc.register_terminal(terminal_id, pane_id, is_kiro)
            except Exception as e:
                if strict_backend_registration:
                    raise RuntimeError("herdr_register_failed") from e
                logger.warning(f"Failed to register terminal {terminal_id} with herdr inbox: {e}")
        if resume_uuid and not defer_init and owned_uuid_lease:
            from cli_agent_orchestrator.services.provider_session_lease import (
                release_provider_session_lease,
            )

            release_provider_session_lease(uuid_lease_token)
        if resume_uuid and not defer_init and owned_lifecycle_lease:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            release_session_lifecycle_lease(session_lifecycle_lease_token)
        return terminal

    except Exception as e:
        # Cleanup on failure: clean up FIFO reader, status monitor, provider, and session
        logger.error(f"Failed to create terminal: {e}")
        quiesce_error = None
        if defer_init and has_deferred_init(terminal_id):
            try:
                await quiesce_deferred_terminal(terminal_id)
            except Exception as exc:
                quiesce_error = str(exc)
        settlement_error = None
        settlement_retained = False
        if quiesce_error is not None:
            settlement_error = quiesce_error
            settlement_retained = True
        elif resume_uuid and db_created:
            try:
                settlement = _settle_published_creation_failure(
                    terminal_id,
                    resume_uuid,
                    uuid_lease_token,
                    registry,
                    existing_rebind_lease=lease_token,
                )
                settlement_error = settlement.get("error_code")
                settlement_retained = settlement.get("status") == "retained"
            except Exception as settle_exc:
                settlement_error = str(settle_exc)
        elif lease_token is not None and db_created:
            rollback = _delete_terminal_under_lease(
                terminal_id,
                lease_token,
                registry=registry,
                require_confirmed_death=True,
                quarantine_session_uuid=(fork_context.session_uuid if fork_context else None),
                uuid_lease_token=uuid_lease_token,
            )
            settlement_error = (
                "rollback_kill_uncertain" if rollback.get("rollback_kill_uncertain") else None
            )
            settlement_retained = bool(rollback.get("rollback_kill_uncertain"))
        else:
            _rollback_terminal_creation(
                terminal_id,
                session_name,
                locals().get("window_name"),
                session_created,
                window_created,
                fifo_attached,
                db_created,
            )
        if not settlement_retained:
            try:
                status_monitor.clear_terminal(terminal_id)
            except Exception:
                pass  # Ignore cleanup errors
        if not ((resume_uuid or lease_token is not None) and db_created):
            try:
                provider_manager.cleanup_provider(terminal_id)
            except Exception:
                pass
        if resume_uuid and uuid_lease_token is not None and owned_uuid_lease:
            from cli_agent_orchestrator.services.provider_session_lease import (
                release_provider_session_lease,
            )

            try:
                release_provider_session_lease(uuid_lease_token)
            except RuntimeError:
                pass
        if resume_uuid and session_lifecycle_lease_token is not None and owned_lifecycle_lease:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            try:
                release_session_lifecycle_lease(session_lifecycle_lease_token)
            except RuntimeError:
                pass
        if settlement_error:
            raise RuntimeError(settlement_error) from e
        raise


_PERSIST_FAILURE_CODES = {
    "terminal_metadata_missing",
    "identity_persist_failed",
    "shell_baseline_unavailable",
    "terminal_identity_persist_failed",
}


class _DeferredInitFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, (NativeHomeIsolationUnavailable, ProviderAuthRefreshFailed)):
        return exc.code
    if isinstance(exc, (RetryableArtifactValidation, TerminalArtifactValidation)):
        return exc.code
    if isinstance(exc, (DeferredExecutorSaturated, _DeferredInitFailure)):
        return exc.code
    if isinstance(exc, RuntimeError) and str(exc) in _PERSIST_FAILURE_CODES:
        return str(exc)
    return "deferred_init_internal"


def _notice_text(
    *,
    code: str,
    deadline_s: float,
    token: str,
    worker: str,
    profile: str,
    provider: str,
) -> str:
    fields = (code, token, worker, profile, provider)
    if any(
        not isinstance(value, str)
        or not value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        for value in fields
    ):
        raise ValueError("deferred_notice_identifier_invalid")
    return (
        f"code={code} deadline_s={repr(float(deadline_s))} token={token} "
        f"worker={worker} profile={profile} provider={provider}"
    )


def _register_deferred_call(terminal_id: str, generation: str, call: DeferredCall) -> None:
    with _deferred_tasks_lock:
        record = _deferred_tasks_by_terminal.get(terminal_id)
        if record is None or record.generation != generation:
            call.quiesce_failed = True
            return
        record.current_call = call

    def finished(_future: concurrent.futures.Future) -> None:
        def cleanup_closed_loop_record() -> None:
            with _deferred_tasks_lock:
                current = _deferred_tasks_by_terminal.get(terminal_id)
                if (
                    current is not None
                    and current.generation == generation
                    and current.current_call is call
                    and current.task.done()
                ):
                    _deferred_tasks_by_terminal.pop(terminal_id, None)

        def cleanup_completed_record() -> None:
            current = _deferred_tasks_by_terminal.get(terminal_id)
            if (
                current is not None
                and current.generation == generation
                and current.current_call is call
                and current.task.done()
            ):
                _deferred_tasks_by_terminal.pop(terminal_id, None)

        if record.loop.is_closed():
            cleanup_closed_loop_record()
            return
        try:
            record.loop.call_soon_threadsafe(cleanup_completed_record)
        except RuntimeError:
            cleanup_closed_loop_record()

    call.future.add_done_callback(finished)


def _claim_deferred_call_result(call: DeferredCall, owner: str) -> bool:
    if call.result_owner != "open":
        return False
    call.result_owner = owner
    return True


def _clear_consumed_deferred_call(
    terminal_id: str,
    generation: str,
    call: DeferredCall,
) -> bool:
    """Clear a call only after its owning asyncio task consumed the result."""
    owns_result = _claim_deferred_call_result(call, "task")
    current = _deferred_tasks_by_terminal.get(terminal_id)
    if current is not None and current.generation == generation and current.current_call is call:
        current.current_call = None
    return owns_result


async def _tracked_blocking(
    terminal_id: str,
    generation: str,
    call_type: str,
    operation: str,
    function,
    *args,
    deadline: float | None = None,
    **kwargs,
):
    with _deferred_tasks_lock:
        record = _deferred_tasks_by_terminal.get(terminal_id)
        if record is not None and (record.generation != generation or record.abandoned):
            raise asyncio.CancelledError
    registered_call: DeferredCall | None = None

    def register(call: DeferredCall) -> None:
        nonlocal registered_call
        registered_call = call
        _register_deferred_call(terminal_id, generation, call)

    try:
        result, grant = await dispatcher.run(
            terminal_id,
            generation,
            call_type,
            operation,
            function,
            *args,
            deadline=deadline,
            on_registered=register,
            **kwargs,
        )
    except asyncio.CancelledError:
        # Quiescence owns observation of a retained call after cancellation.
        raise
    except BaseException:
        if registered_call is not None and not _clear_consumed_deferred_call(
            terminal_id,
            generation,
            registered_call,
        ):
            raise asyncio.CancelledError
        raise
    if registered_call is not None and not _clear_consumed_deferred_call(
        terminal_id,
        generation,
        registered_call,
    ):
        raise asyncio.CancelledError
    return result, grant


def _commit_ready_if_generation_current(terminal_id: str, generation: str) -> bool:
    """Run the DB ready CAS behind the quiesce-owned abandonment fence."""
    record = _deferred_tasks_by_terminal.get(terminal_id)
    if record is None or record.generation != generation:
        return False
    call = record.current_call
    if call is None:
        return False

    def still_current() -> bool:
        with call.ready_winner_lock:
            if call.ready_winner == "commit_decided":
                return True
        current = _deferred_tasks_by_terminal.get(terminal_id)
        return bool(
            current is record
            and current.generation == generation
            and not current.abandoned
            and not call.quiesce_failed
            and call.abandon_event is not None
            and not call.abandon_event.is_set()
        )

    def decide_commit() -> bool:
        with call.ready_winner_lock:
            if call.ready_winner == "timeout":
                return False
            call.ready_winner = "commit_decided"
            return True

    def commit_is_decided() -> bool:
        with call.ready_winner_lock:
            return call.ready_winner == "commit_decided"

    committed = mark_terminal_init_ready(
        terminal_id,
        should_commit=still_current,
        decide_commit=decide_commit,
        commit_is_decided=commit_is_decided,
        on_committed=lambda: setattr(call, "ready_committed", True),
    )
    if committed:
        call.ready_committed = True
    return committed


async def _mark_ready_if_generation_current(terminal_id: str, generation: str) -> bool:
    committed, _ = await _tracked_blocking(
        terminal_id,
        generation,
        "abandonable",
        "ready_commit",
        _commit_ready_if_generation_current,
        terminal_id,
        generation,
    )
    return bool(committed)


def _deferred_worker_live(terminal_id: str) -> bool:
    metadata = get_terminal_metadata(terminal_id)
    if metadata is None or metadata.get("init_state") != "init_pending":
        return False
    try:
        return (
            get_backend().window_liveness(metadata["tmux_session"], metadata["tmux_window"])
            == "live"
        )
    except Exception:
        return False


async def _validate_deferred_artifact(
    provider_instance,
    prepared: _PreparedRuntimeIdentity,
    terminal_id: str,
    generation: str,
    deadline_s: float,
) -> None:
    origin = time.monotonic()
    deadline = origin + deadline_s
    while True:
        try:
            _result, _grant = await _tracked_blocking(
                terminal_id,
                generation,
                "abandonable",
                "validate",
                provider_instance.validate_session_artifact,
                prepared.session_uuid,
                prepared.cwd,
                deadline=deadline,
            )
            return
        except RetryableArtifactValidation as exc:
            if time.monotonic() >= deadline:
                raise exc
            live, _ = await _tracked_blocking(
                terminal_id,
                generation,
                "abandonable",
                "metadata_read",
                _deferred_worker_live,
                terminal_id,
                deadline=deadline,
            )
            if not live:
                raise _DeferredInitFailure("worker_vanished")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise exc
            await asyncio.sleep(min(POLL_INTERVAL, remaining))


def _delete_terminal_core(
    terminal_id: str,
    registry: PluginRegistry | None = None,
    *,
    preserve_warm_intent: bool = False,
) -> bool:
    from cli_agent_orchestrator.services.rebind_lease import (
        acquire_rebind_lease,
        release_rebind_lease,
    )

    token = acquire_rebind_lease(terminal_id)
    if token is None:
        raise RuntimeError("rebind_in_progress")
    try:
        kwargs = {"preserve_warm_intent": True} if preserve_warm_intent else {}
        result = _delete_terminal_under_lease(
            terminal_id,
            token,
            registry=registry,
            **kwargs,
        )
        return bool(result["terminal_deleted"] if isinstance(result, dict) else result)
    finally:
        release_rebind_lease(token)


def _settle_deferred_failure_sync(
    terminal_id: str,
    registry: PluginRegistry | None = None,
    uuid_lease_token=None,
) -> dict:
    metadata = get_terminal_metadata(terminal_id)
    if metadata is None:
        return {"status": "deleted", "error_code": None}
    session_uuid = metadata.get("provider_session_id")
    if session_uuid:
        return _settle_published_creation_failure(
            terminal_id,
            session_uuid,
            uuid_lease_token,
            registry,
        )
    deleted = _delete_terminal_core(terminal_id, registry=registry)
    return {"status": "deleted" if deleted else "retained", "error_code": None}


async def _claim_and_settle_deferred_failure(
    terminal_id: str,
    generation: str,
    snapshot: dict[str, Any],
    code: str,
    registry: PluginRegistry | None,
    uuid_lease_token=None,
    *,
    fatal_claim_failure: bool = False,
) -> None:
    token = str(uuid.uuid4())
    owner_epoch = snapshot.get("init_owner_epoch")
    try:
        owner_epoch = str(uuid.UUID(str(owner_epoch)))
    except (ValueError, TypeError, AttributeError):
        owner_epoch = SERVER_INIT_OWNER_EPOCH

    async def deadletter(
        *,
        stage: str,
        notice: str,
        rejection_reason: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        from cli_agent_orchestrator.services.deferred_deadletter_service import (
            write_deferred_failure_deadletter,
        )

        payload = {
            "terminal_id": terminal_id,
            "caller_id": snapshot.get("caller_id"),
            "owner_epoch": owner_epoch,
            "failure_token": token,
            "notice": notice,
            "stage": stage,
        }
        if rejection_reason is not None:
            payload["rejection_reason"] = rejection_reason
        if attempts is not None:
            payload["attempt_log"] = attempts
        try:
            await _tracked_blocking(
                terminal_id,
                generation,
                "mutating",
                "deadletter",
                write_deferred_failure_deadletter,
                payload,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.critical(
                "deferred_init_deadletter_write_failed terminal=%s token=%s stage=%s",
                terminal_id,
                token,
                stage,
                exc_info=True,
            )

    deadline_s = snapshot.get("init_deadline_s")
    if not isinstance(deadline_s, (int, float)) or not 1.0 <= float(deadline_s) <= 600.0:
        notice = (
            f"Worker {terminal_id} deferred initialization failed before claim validation "
            f"(invalid_stored_deadline); token={token}."
        )
        logger.critical(
            "deferred_init_internal terminal=%s invalid_stored_deadline token=%s",
            terminal_id,
            token,
        )
        await deadletter(
            stage="pre_claim_validation",
            notice=notice,
            rejection_reason="invalid_stored_deadline",
        )
        return
    try:
        notice = _notice_text(
            code=code,
            deadline_s=float(deadline_s),
            token=token,
            worker=terminal_id,
            profile=snapshot.get("agent_profile"),
            provider=snapshot.get("provider"),
        )
    except ValueError:
        notice = (
            f"Worker {terminal_id} deferred initialization failed before claim validation "
            f"(notice_rejected); token={token}."
        )
        logger.critical(
            "deferred_init_internal terminal=%s notice_rejected token=%s",
            terminal_id,
            token,
        )
        await deadletter(
            stage="pre_claim_validation",
            notice=notice,
            rejection_reason="notice_rejected",
        )
        return
    attempt_log: list[dict[str, Any]] = []
    claim = None
    retry_delays = (1.0, 5.0, 25.0)
    total_attempts = 1 if fatal_claim_failure else 4
    for attempt_index in range(total_attempts):
        try:
            claim, _ = await _tracked_blocking(
                terminal_id,
                generation,
                "mutating",
                "h3_claim",
                claim_deferred_init_failure,
                terminal_id,
                caller_id=snapshot.get("caller_id"),
                failure_token=token,
                notice=notice,
            )
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            chain: list[str] = []
            cursor: BaseException | None = exc
            while cursor is not None:
                chain.append(f"{type(cursor).__name__}: {cursor}")
                cursor = cursor.__cause__ or cursor.__context__
            attempt_log.append(
                {
                    "attempt": attempt_index + 1,
                    "exception": type(exc).__name__,
                    "chain": chain,
                }
            )
            busy_exhausted = str(exc) == "deferred_init_claim_busy_exhausted"
            logger.error(
                "deferred_init_claim_failed terminal=%s code=%s attempt=%s error=%s",
                terminal_id,
                code,
                attempt_index + 1,
                type(exc).__name__,
                exc_info=True,
            )
            if fatal_claim_failure:
                if busy_exhausted:
                    raise
                return
            if busy_exhausted or attempt_index + 1 >= total_attempts:
                logger.critical(
                    "deferred_init_claim_exhausted_retaining terminal=%s code=%s token=%s",
                    terminal_id,
                    code,
                    token,
                    exc_info=True,
                )
                await deadletter(
                    stage="h3_claim",
                    notice=notice,
                    attempts=attempt_log,
                )
                return
            await asyncio.sleep(retry_delays[attempt_index])
    if claim is None:
        return
    if claim["status"] == "claimed_caller_gone":
        logger.error("caller_gone_zero_notice terminal=%s token=%s", terminal_id, token)
    if claim["status"] == "row_missing":
        return
    if claim.get("init_state") not in {
        "init_failed_notified",
        "init_failed_caller_gone",
    }:
        return
    try:
        await _tracked_blocking(
            terminal_id,
            generation,
            "mutating",
            "settlement",
            _settle_deferred_failure_sync,
            terminal_id,
            registry,
            uuid_lease_token,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("deferred_init_settlement_failed terminal=%s", terminal_id)


async def _late_mutation_reconciler(
    terminal_id: str,
    operation: str,
    future: concurrent.futures.Future,
) -> None:
    if operation == "h3_claim":
        try:
            result = future.result()
        except Exception:
            logger.error("reconcile_h3_rolled_back terminal=%s", terminal_id)
            return
        if result.get("init_state") in {
            "init_failed_notified",
            "init_failed_caller_gone",
        }:
            try:
                await dispatcher.run(
                    terminal_id,
                    "reconciler",
                    "mutating",
                    "settlement",
                    _settle_deferred_failure_sync,
                    terminal_id,
                )
            except Exception:
                logger.exception(
                    "reconcile_h3_committed terminal=%s settlement=failed", terminal_id
                )
            else:
                logger.error("reconcile_h3_committed terminal=%s", terminal_id)
        else:
            logger.error("reconcile_h3_rolled_back terminal=%s", terminal_id)
    elif operation == "delete":
        try:
            future.result()
            logger.error("reconcile_delete_result terminal=%s", terminal_id)
        except Exception as exc:
            logger.error(
                "reconcile_delete_result terminal=%s error=%s", terminal_id, type(exc).__name__
            )
    else:
        try:
            future.result()
            logger.error("reconcile_settlement_result terminal=%s", terminal_id)
        except Exception as exc:
            logger.error(
                "reconcile_settlement_result terminal=%s error=%s", terminal_id, type(exc).__name__
            )


def _schedule_late_reconciler(
    record: _DeferredTaskRecord,
    terminal_id: str,
    call: DeferredCall,
) -> None:
    def spawn(_future: concurrent.futures.Future) -> None:
        if record.loop.is_closed():
            return

        def create() -> None:
            task = record.loop.create_task(
                _late_mutation_reconciler(terminal_id, call.operation, call.future)
            )
            setattr(task, "_cao_terminal_id", terminal_id)
            setattr(task, "_cao_operation", call.operation)
            _deferred_reconciler_tasks.add(task)
            task.add_done_callback(_deferred_reconciler_tasks.discard)

        record.loop.call_soon_threadsafe(create)

    call.future.add_done_callback(spawn)


async def quiesce_deferred_terminal(
    terminal_id: str,
    *,
    timeout_s: float = DEFERRED_TASK_QUIESCE_S,
) -> None:
    deadline = time.monotonic() + timeout_s
    # Reads and winner flags are atomic object operations under CPython. Avoid
    # acquiring the registry's threading.Lock on the event-loop thread: a
    # blocking ready DB call must not be able to postpone this deadline.
    record = _deferred_tasks_by_terminal.get(terminal_id)
    if record is None:
        return
    call = record.current_call
    record.loop.call_soon_threadsafe(record.task.cancel)
    try:
        remaining = max(0.0, deadline - time.monotonic())
        await asyncio.wait_for(asyncio.shield(record.task), remaining)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        pass
    while call is not None and not call.future.done() and time.monotonic() < deadline:
        await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if call is not None and not call.future.done():
        mutation_in_flight = call.call_type == "mutating"
        if call.operation == "ready_commit":
            winner_acquired = call.ready_winner_lock.acquire(blocking=False)
            if not winner_acquired:
                mutation_in_flight = True
            else:
                try:
                    if call.ready_winner == "commit_decided":
                        mutation_in_flight = True
                    else:
                        call.ready_winner = "timeout"
                finally:
                    call.ready_winner_lock.release()
        record.abandoned = True
        call.quiesce_failed = True
        if call.abandon_event is not None:
            call.abandon_event.set()
        if mutation_in_flight:
            if _claim_deferred_call_result(call, "reconciler"):
                _schedule_late_reconciler(record, terminal_id, call)
            raise RuntimeError("quiesce_timeout_mutation_in_flight")
        _claim_deferred_call_result(call, "quiesce")
        raise RuntimeError("deferred_task_quiesce_timeout")
    if call is not None and _claim_deferred_call_result(call, "quiesce"):
        call.future.result()
    if not record.task.done():
        record.abandoned = True
        raise RuntimeError("deferred_task_quiesce_timeout")


def quiesce_deferred_terminal_sync(
    terminal_id: str,
    *,
    timeout_s: float = DEFERRED_TASK_QUIESCE_S,
) -> None:
    with _deferred_tasks_lock:
        record = _deferred_tasks_by_terminal.get(terminal_id)
    if record is None:
        return
    try:
        if asyncio.get_running_loop() is record.loop:
            raise RuntimeError("deferred_quiesce_requires_async_call")
    except RuntimeError as exc:
        if str(exc) == "deferred_quiesce_requires_async_call":
            raise
    future = asyncio.run_coroutine_threadsafe(
        quiesce_deferred_terminal(terminal_id, timeout_s=timeout_s), record.loop
    )
    try:
        future.result(timeout=timeout_s + 1.0)
    except concurrent.futures.TimeoutError as exc:
        raise RuntimeError("deferred_task_quiesce_timeout") from exc


async def shutdown_deferred_tasks(
    *,
    timeout_s: float = DEFERRED_TASK_QUIESCE_S,
) -> None:
    with _deferred_tasks_lock:
        terminal_ids = list(_deferred_tasks_by_terminal)
    for terminal_id in terminal_ids:
        try:
            await quiesce_deferred_terminal(terminal_id, timeout_s=timeout_s)
        except Exception as exc:
            logger.error("deferred_shutdown_timeout terminal=%s code=%s", terminal_id, exc)
    tasks = list(_deferred_reconciler_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        if getattr(task, "_cao_operation", None) == "delete":
            terminal_id = getattr(task, "_cao_terminal_id", "unknown")
            if get_terminal_metadata(terminal_id) is None:
                logger.error("reconcile_audit_lost_row_gone terminal=%s", terminal_id)


async def recover_deferred_inits(
    registry: PluginRegistry | None = None,
    *,
    owner_epoch: str = SERVER_INIT_OWNER_EPOCH,
) -> None:
    rows = list_deferred_init_recovery_rows(owner_epoch)
    for row in rows:
        terminal_id = row["id"]
        state = row.get("init_state")
        if row.get("recovery_state") == "rollback_kill_uncertain":
            logger.warning("deferred_init_recovery_quarantined terminal=%s", terminal_id)
            continue
        if state == "init_pending":
            try:
                owner = row.get("init_owner_epoch")
                if str(uuid.UUID(owner)) != owner or row.get("init_started_at") is None:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                logger.error("deferred_init_corrupt_pending terminal=%s", terminal_id)
                continue
            await _claim_and_settle_deferred_failure(
                terminal_id,
                f"h5-{owner_epoch}",
                row,
                "server_restart_during_deferred_init",
                registry,
                fatal_claim_failure=True,
            )
        elif state in {"init_failed_notified", "init_failed_caller_gone"}:
            if state == "init_failed_caller_gone":
                logger.error("caller_gone_zero_notice terminal=%s", terminal_id)
            try:
                await dispatcher.run(
                    terminal_id,
                    f"h5-{owner_epoch}",
                    "mutating",
                    "settlement",
                    _settle_deferred_failure_sync,
                    terminal_id,
                    registry,
                )
            except Exception:
                logger.exception("deferred_init_settlement_failed terminal=%s", terminal_id)


def _fork_refresh_lock(base_name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (loop, base_name)
    lock = _fork_refresh_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _fork_refresh_locks[key] = lock
    return lock


def _fork_refresh_prompt(base_name: str, changed: list[str]) -> str:
    paths = "\n".join(f"- {path}" for path in changed)
    return (
        f"[CAO AUTO-REFRESH] Refresh registered base '{base_name}'. Re-read and "
        "ingest the current contents of every changed file below. Do no unrelated "
        f"work; reply only after the refresh is complete.\n\n{paths}"
    )


def _dispatch_base_refresh(
    base_terminal_id: str,
    message: str,
    *,
    sender_id: str | None,
    registry: PluginRegistry | None,
) -> bool:
    from cli_agent_orchestrator.services.terminal_guard_service import require_input_allowed

    require_input_allowed(base_terminal_id, refresh_ingest=True)
    return send_input(
        base_terminal_id,
        message,
        registry=registry,
        sender_id=sender_id,
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        expect_callback=False,
    )


async def _wait_for_base_ready(
    base_terminal_id: str,
    deadline: float,
    *,
    input_gen: int | None = None,
) -> bool:
    while time.monotonic() < deadline:
        status = status_monitor.get_status(base_terminal_id)
        if status == TerminalStatus.ERROR:
            return False
        if status in {None, TerminalStatus.UNKNOWN} and not terminal_exists(base_terminal_id):
            return False
        if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
            if input_gen is None:
                return True
            status_gen = status_monitor.get_status_gen(base_terminal_id)
            if status_gen is None or status_gen >= input_gen:
                return True
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return False


async def _prepare_fork_refresh(
    terminal_id: str,
    generation: str,
    base_name: str,
    stale_preamble: str,
    registry: PluginRegistry | None,
    caller_snapshot: dict,
) -> str:
    """Coalesce one bounded refresh and return a fresh or stale preamble."""
    deadline = time.monotonic() + FORK_REFRESH_WAIT_BUDGET
    lock = _fork_refresh_lock(base_name)
    try:
        await asyncio.wait_for(lock.acquire(), max(0.0, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        return stale_preamble
    try:
        row, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_read",
            get_ready_provider_session,
            base_name,
            deadline=deadline,
        )
        if row is None or row.get("kind", "base") != "base":
            return stale_preamble
        changed_and_preamble, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_compare",
            fork_staleness,
            row,
            deadline=deadline,
        )
        changed, current_preamble = changed_and_preamble
        if not changed:
            return cast(str, current_preamble)
        base_terminal_id = row.get("source_terminal_id")
        if not base_terminal_id:
            return stale_preamble
        if not await _wait_for_base_ready(base_terminal_id, deadline):
            if not terminal_exists(base_terminal_id):
                logger.warning(
                    "Fork refresh source terminal is gone; using stale base. "
                    "base=%s source_terminal_id=%s. Re-register the base to restore "
                    "fresh auto-refresh.",
                    base_name,
                    base_terminal_id,
                )
            return stale_preamble
        dispatched, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_send",
            _dispatch_base_refresh,
            base_terminal_id,
            _fork_refresh_prompt(base_name, changed),
            sender_id=caller_snapshot.get("caller_id"),
            registry=registry,
            deadline=deadline,
        )
        if not dispatched:
            return stale_preamble
        input_gen = status_monitor.get_input_gen(base_terminal_id)
        if not await _wait_for_base_ready(base_terminal_id, deadline, input_gen=input_gen):
            if not terminal_exists(base_terminal_id):
                logger.warning(
                    "Fork refresh source terminal is gone; using stale base. "
                    "base=%s source_terminal_id=%s. Re-register the base to restore "
                    "fresh auto-refresh.",
                    base_name,
                    base_terminal_id,
                )
            return stale_preamble
        snapshot_result, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_snapshot",
            fork_snapshot,
            row["cwd"],
            deadline=deadline,
        )
        sha, hashes = snapshot_result
        current, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_read",
            get_ready_provider_session,
            base_name,
            deadline=deadline,
        )
        if (
            current is None
            or current.get("kind", "base") != "base"
            or current.get("source_terminal_id") != base_terminal_id
            or current.get("session_uuid") != row.get("session_uuid")
        ):
            return stale_preamble
        updated, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_snapshot_write",
            update_provider_session_snapshot,
            current["id"],
            git_sha=sha,
            dirty_hashes=hashes,
            deadline=deadline,
        )
        if updated is None:
            return stale_preamble
        refreshed, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_compare",
            fork_staleness,
            updated,
            deadline=deadline,
        )
        return cast(str, refreshed[1])
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("fork_refresh_failed base=%s terminal=%s", base_name, terminal_id)
        return stale_preamble
    finally:
        lock.release()


async def _prepare_fork_message(
    terminal_id: str,
    generation: str,
    initial_message: str | None,
    fork_context,
    refresh_base_name: str | None,
    registry: PluginRegistry | None,
    caller_snapshot: dict,
) -> str | None:
    if fork_context is None or not initial_message:
        return initial_message
    preamble = fork_context.initial_preamble
    if refresh_base_name is not None:
        preamble = await _prepare_fork_refresh(
            terminal_id,
            generation,
            refresh_base_name,
            preamble,
            registry,
            caller_snapshot,
        )
    return f"{preamble}\n\n{initial_message}"


def _schedule_deferred_init(
    provider_instance,
    terminal_id: str,
    initial_message: Optional[str],
    orchestration_type: Optional[OrchestrationType],
    registry: PluginRegistry | None,
    uuid_lease_token=None,
    owns_uuid_lease: bool = False,
    session_lifecycle_lease_token=None,
    owns_lifecycle_lease: bool = False,
    settlement_form: str = "first_time",
    caller_snapshot: dict | None = None,
    fork_context=None,
    refresh_base_name: str | None = None,
) -> None:
    """Kick off provider.initialize() in the background and, on success,
    deliver the initial message via send_input.

    Runs as an asyncio task on the running event loop so it doesn't block
    the caller. Because assign() has already returned success=True by the
    time this runs, a failure here must be made OBSERVABLE to the supervisor
    rather than silently swallowed — otherwise the supervisor waits forever
    on a callback that can never arrive and a later inspect 404s. On failure
    we notify the caller's inbox (best-effort) and then tear the worker down.

    ``TerminalInputBlockedError`` (the worker is parked on a WAITING_USER_ANSWER
    prompt right after init) is NOT a teardown case: the worker is alive and
    answerable via answer_user_prompt, so we leave it in place and only log.
    """

    snapshot = dict(caller_snapshot or get_terminal_metadata(terminal_id) or {})
    generation = str(uuid.uuid4())

    def _blocked_notice_receiver() -> str | None:
        caller_id = snapshot.get("caller_id")
        if isinstance(caller_id, str) and get_terminal_metadata(caller_id) is not None:
            return caller_id
        session_name = snapshot.get("tmux_session")
        if not isinstance(session_name, str):
            return None
        for terminal in list_terminals_by_session(session_name):
            if terminal.get("id") == terminal_id:
                continue
            try:
                candidate = load_agent_profile(terminal.get("agent_profile") or "")
            except (FileNotFoundError, ValueError):
                continue
            if getattr(candidate, "role", None) == "supervisor":
                return cast(str, terminal["id"])
        return None

    async def _notify_blocked_wait(rule_name: str) -> None:
        receiver = _blocked_notice_receiver()
        if receiver is None:
            logger.warning(
                "deferred_init_blocked_no_supervisor terminal=%s rule=%s",
                terminal_id,
                rule_name,
            )
            return
        notice = (
            f"Worker {terminal_id} initialization is paused by auto-responder "
            f"wait rule '{rule_name}'. The worker remains alive and init_pending."
        )
        await _tracked_blocking(
            terminal_id,
            generation,
            "mutating",
            "notice",
            create_inbox_message,
            terminal_id,
            receiver,
            notice,
        )

    async def _run() -> None:
        try:
            provider_instance.blocked_wait_notifier = _notify_blocked_wait
            prepared_message = await _prepare_fork_message(
                terminal_id,
                generation,
                initial_message,
                fork_context,
                refresh_base_name,
                registry,
                snapshot,
            )
            await provider_instance.initialize()
            prepared, _ = await _tracked_blocking(
                terminal_id,
                generation,
                "abandonable",
                "capture_persist",
                _prepare_provider_runtime_identity,
                provider_instance,
                terminal_id,
                settlement_form=settlement_form,
            )
            if prepared is not None:
                await _validate_deferred_artifact(
                    provider_instance,
                    prepared,
                    terminal_id,
                    generation,
                    float(snapshot["init_deadline_s"]),
                )
                await _tracked_blocking(
                    terminal_id,
                    generation,
                    "abandonable",
                    "capture_persist",
                    _commit_provider_runtime_identity,
                    terminal_id,
                    prepared,
                )
            shell_command = provider_instance.shell_baseline
            if isinstance(shell_command, str) and shell_command:
                await _tracked_blocking(
                    terminal_id,
                    generation,
                    "abandonable",
                    "capture_persist",
                    update_terminal_shell_command,
                    terminal_id,
                    shell_command,
                )
            if prepared_message:
                # For assign/handoff the sender is the CALLER (the supervisor),
                # not this MCP server. But the deferred path is used only via
                # /assign, and _assign_impl on the MCP-server side already
                # embedded the callback instructions into initial_message.
                # We still pass sender_id=caller_id if present in DB metadata
                # so plugin events see it.
                await _tracked_blocking(
                    terminal_id,
                    generation,
                    "abandonable",
                    "send_input",
                    send_input,
                    terminal_id,
                    prepared_message,
                    registry=registry,
                    sender_id=snapshot.get("caller_id"),
                    orchestration_type=orchestration_type,
                )
            await _mark_ready_if_generation_current(terminal_id, generation)
        except TerminalInputBlockedError as e:
            # The worker initialized but is parked on an interactive prompt
            # (WAITING_USER_ANSWER). It is alive and can be driven via
            # answer_user_prompt — do NOT delete it. Just surface the state to
            # the supervisor so it knows delivery is pending on a prompt.
            logger.warning(
                "Deferred init for terminal %s: worker is waiting on a user "
                "prompt; task not yet delivered. Leaving worker alive for "
                "answer_user_prompt. (%s)",
                terminal_id,
                e,
            )
            queued = False
            try:
                await _tracked_blocking(
                    terminal_id,
                    generation,
                    "abandonable",
                    "blocked_queue",
                    create_inbox_message,
                    snapshot.get("caller_id") or "unknown",
                    terminal_id,
                    prepared_message,
                    OrchestrationType.ASSIGN,
                )
                queued = True
            except Exception:
                logger.exception(
                    "Could not queue blocked assigned task for terminal %s", terminal_id
                )
            if not queued:
                await _claim_and_settle_deferred_failure(
                    terminal_id,
                    generation,
                    snapshot,
                    "deferred_init_internal",
                    registry,
                    uuid_lease_token,
                )
                return
            await _mark_ready_if_generation_current(terminal_id, generation)
            notice = (
                f"Worker {terminal_id} is waiting on a dialog; the assigned task is "
                f"queued and will deliver when the dialog clears."
            )
            if snapshot.get("caller_id"):
                try:
                    await _tracked_blocking(
                        terminal_id,
                        generation,
                        "mutating",
                        "notice",
                        create_inbox_message,
                        terminal_id,
                        snapshot["caller_id"],
                        notice,
                    )
                except Exception:
                    logger.exception("Deferred blocked notice failed terminal=%s", terminal_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # exc_info=True preserves the traceback for debugging; {e!r} avoids
            # newline/control-character injection into logs and the inbox message
            # (the exception text can contain provider-supplied content).
            logger.error(
                "Deferred init for terminal %s failed: %r. "
                "Notifying caller and tearing down worker.",
                terminal_id,
                e,
                exc_info=True,
            )
            await _claim_and_settle_deferred_failure(
                terminal_id,
                generation,
                snapshot,
                _failure_code(e),
                registry,
                uuid_lease_token,
            )
        finally:
            if owns_uuid_lease and uuid_lease_token is not None:
                from cli_agent_orchestrator.services.provider_session_lease import (
                    release_provider_session_lease,
                )

                try:
                    release_provider_session_lease(uuid_lease_token)
                except RuntimeError:
                    pass
            if owns_lifecycle_lease and session_lifecycle_lease_token is not None:
                from cli_agent_orchestrator.services.session_lifecycle_lease import (
                    release_session_lifecycle_lease,
                )

                try:
                    release_session_lifecycle_lease(session_lifecycle_lease_token)
                except RuntimeError:
                    pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error(f"Deferred init for {terminal_id}: no running event loop; init skipped")
        return
    task = loop.create_task(_run())
    _deferred_init_tasks.add(task)
    with _deferred_tasks_lock:
        _deferred_tasks_by_terminal[terminal_id] = _DeferredTaskRecord(
            task=task,
            loop=loop,
            generation=generation,
            session_name=snapshot.get("tmux_session"),
        )

    def _done(completed):
        _deferred_init_tasks.discard(completed)
        with _deferred_tasks_lock:
            record = _deferred_tasks_by_terminal.get(terminal_id)
            if (
                record is not None
                and record.task is completed
                and (record.current_call is None or record.current_call.future.done())
            ):
                _deferred_tasks_by_terminal.pop(terminal_id, None)

    task.add_done_callback(_done)


def get_terminal(terminal_id: str) -> Dict:
    """Get terminal data."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        status = status_monitor.get_status(terminal_id).value
        input_gen = status_monitor.get_input_gen(terminal_id)
        status_gen = status_monitor.get_status_gen(terminal_id)

        return {
            "id": metadata["id"],
            "name": metadata["tmux_window"],
            "provider": metadata["provider"],
            "session_name": metadata["tmux_session"],
            "agent_profile": metadata["agent_profile"],
            "caller_id": metadata.get("caller_id"),
            "caller_mailbox_id": metadata.get("caller_mailbox_id"),
            "allowed_tools": metadata.get("allowed_tools"),
            "provider_session_id": metadata.get("provider_session_id"),
            "status": status,
            "input_gen": input_gen,
            "status_gen": 0 if status_gen is None else status_gen,
            "last_active": metadata["last_active"],
        }

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


def get_working_directory(terminal_id: str) -> Optional[str]:
    """Get the current working directory of a terminal's pane.

    Args:
        terminal_id: The terminal identifier

    Returns:
        Working directory path, or None if pane has no directory

    Raises:
        ValueError: If terminal not found
        Exception: If unable to query working directory
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        working_dir = get_backend().get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]
        )
        return working_dir

    except Exception as e:
        logger.error(f"Failed to get working directory for terminal {terminal_id}: {e}")
        raise


def send_input(
    terminal_id: str,
    message: str,
    registry: PluginRegistry | None = None,
    sender_id: str | None = None,
    orchestration_type: OrchestrationType | None = None,
    defer_on_dialog: bool = False,
    *,
    expect_callback: bool = True,
) -> bool:
    """Send input to terminal via tmux paste buffer.

    Uses bracketed paste mode (-p) to bypass TUI hotkey handling. The number
    of Enter keys sent after pasting is determined by the provider's
    ``paste_enter_count`` property (e.g., some TUIs need 2 Enters because
    bracketed paste triggers multi-line mode).
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        provider = provider_manager.get_provider(terminal_id)
        orchestration_value = (
            orchestration_type.value
            if isinstance(orchestration_type, OrchestrationType)
            else str(orchestration_type or "")
        )
        if (
            provider
            and provider.blocks_orchestrated_input_while_waiting_user_answer is True
            and orchestration_value
            in {OrchestrationType.ASSIGN.value, OrchestrationType.HANDOFF.value}
            and status_monitor.get_status(terminal_id) == TerminalStatus.WAITING_USER_ANSWER
        ):
            raise TerminalInputBlockedError(
                f"Terminal {terminal_id} is waiting for a user answer. "
                "Use answer_user_prompt to submit a selection or approval before "
                f"sending {orchestration_value} input."
            )

        # Inject profile contracts only for orchestrated deliveries. Direct
        # human pane input and answer_user_prompt keep their literal text.
        original_message = message
        message = _append_message_contract(message, metadata, orchestration_value)

        # Inject memory context into the very first user message after init.
        # Phase 1 wires injection inline for every provider. The Kiro
        # AgentSpawn hook will replace this path once the plugin
        # migration PR lands; until then, inline injection is the only
        # delivery path.
        # Keep the original message for the PostSendMessageEvent so
        # plugins/webhooks see what the caller sent — not the
        # internal <cao-memory> block that we paste into the TUI.
        message = inject_memory_context(message, terminal_id)

        # Check how many Enter keys the provider needs after paste
        enter_count = provider.paste_enter_count if provider else 1

        # Arm the StatusMonitor stickiness gate so that the next provider-
        # detected PROCESSING transition is honored (overriding the latched
        # IDLE/COMPLETED). Without this, sticky ready-status would block
        # the genuine PROCESSING signal that arrives once the agent starts
        # working on the new message.
        status_monitor.notify_input_sent(terminal_id)

        # Clear ONLY the rolling byte buffer BEFORE sending keys, so stale idle
        # prompts from BEFORE the input can't trigger a false COMPLETED
        # (kiro-cli 2.11's TUI keeps the "ask a question" placeholder in the raw
        # buffer, which combined with input_received=True would return COMPLETED
        # within seconds of send_input). Clearing here — not after send_keys —
        # avoids a race: send_keys includes a submit-delay sleep during which
        # the agent can begin emitting output; a post-send_keys clear would wipe
        # that newly-emitted first chunk of the turn (lost from
        # GET /terminals/{id}/output?mode=full and from early detection). This
        # uses clear_rolling_buffer (byte-only), which preserves the sticky-latch
        # arm set by notify_input_sent above; reset_buffer would wipe the arm and
        # latch-block the IDLE→PROCESSING transition for the whole turn.
        status_monitor.clear_rolling_buffer(terminal_id)

        backend = get_backend()
        if isinstance(getattr(provider, "composer_stash_keys", None), list):
            chip_present_at_inject = stash_draft_before_send(
                terminal_id, metadata, provider, defer_on_dialog=defer_on_dialog
            )
            if chip_present_at_inject:
                enter_count = 1
            preserved_draft = None
        else:
            preserved_draft = preserve_draft_before_send(terminal_id, metadata, provider)

        backend.send_keys(
            metadata["tmux_session"],
            metadata["tmux_window"],
            message,
            enter_count=enter_count,
            force_bracketed_paste=True,
            submit_delay=provider.paste_submit_delay if provider else 0.3,
        )
        if preserved_draft is not None:
            preserved_draft.restore(backend)

        # Notify the provider that external input was received.
        # This allows providers to adjust status
        # detection — specifically to stop reporting IDLE for the post-init
        # state and resume normal COMPLETED detection after a real task.
        if provider:
            provider.mark_input_received()

        update_last_active(terminal_id)
        if (
            expect_callback
            and metadata.get("caller_id")
            and orchestration_value
            in {
                OrchestrationType.ASSIGN.value,
                OrchestrationType.SEND_MESSAGE.value,
            }
        ):
            from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                stalled_callback_watchdog,
            )

            if orchestration_value == OrchestrationType.ASSIGN.value:
                stalled_callback_watchdog.record_inbound_task(
                    terminal_id,
                    metadata["caller_id"],
                    metadata.get("agent_profile") or "",
                )
            elif sender_id == metadata["caller_id"] and stalled_callback_watchdog.has_episode(
                terminal_id
            ):
                stalled_callback_watchdog.record_inbound_task(
                    terminal_id,
                    metadata["caller_id"],
                    metadata.get("agent_profile") or "",
                )
        logger.info(f"Sent input to terminal: {terminal_id}")
        if registry is not None and sender_id is not None and orchestration_type is not None:
            dispatch_plugin_event(
                registry,
                "post_send_message",
                PostSendMessageEvent(
                    session_id=metadata["tmux_session"],
                    sender=sender_id,
                    receiver=terminal_id,
                    message=original_message,
                    orchestration_type=orchestration_type,
                ),
            )
        return True

    except Exception as e:
        logger.error(f"Failed to send input to terminal {terminal_id}: {e}")
        raise


def prepare_input(
    terminal_id: str, message: str, orchestration_type: OrchestrationType | None = None
) -> str:
    """Shape inbox input without consuming first-message memory state."""
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        raise ValueError(f"Terminal '{terminal_id}' not found")
    value = (
        orchestration_type.value
        if isinstance(orchestration_type, OrchestrationType)
        else str(orchestration_type or "")
    )
    return inject_memory_context(
        _append_message_contract(message, metadata, value), terminal_id, consume=False
    )


def send_prepared_input(
    terminal_id: str,
    message: str,
    *,
    defer_on_dialog: bool = False,
    registry: PluginRegistry | None = None,
    sender_id: str | None = None,
    orchestration_type: OrchestrationType | None = None,
    original_message: str | None = None,
    on_submitted=None,
):
    """Send already-shaped bytes; never apply contract or memory shaping again."""
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        raise ValueError(f"Terminal '{terminal_id}' not found")
    backend = get_backend()
    if getattr(backend, "supports_identity_readback", False) is not True:
        logger.warning(
            "pane_identity_proof_unsupported terminal=%s backend=%s",
            terminal_id,
            type(backend).__name__,
        )
    else:
        from cli_agent_orchestrator.services.pane_identity_service import (
            PaneIdentityMismatchError,
            pane_identity_failure,
        )

        identity_failure = pane_identity_failure(terminal_id, metadata, backend)
        if identity_failure is not None:
            logger.critical(
                "pane_identity_proof_failed terminal=%s session=%s window=%s "
                "reason=%s stage=send",
                terminal_id,
                metadata["tmux_session"],
                metadata["tmux_window"],
                identity_failure,
            )
            raise PaneIdentityMismatchError(identity_failure)
    provider = provider_manager.get_provider(terminal_id)
    enter_count = provider.paste_enter_count if provider else 1
    status_monitor.notify_input_sent(terminal_id)
    status_monitor.clear_rolling_buffer(terminal_id)
    if isinstance(getattr(provider, "composer_stash_keys", None), list):
        if stash_draft_before_send(terminal_id, metadata, provider, defer_on_dialog):
            enter_count = 1
        preserved = None
    else:
        preserved = preserve_draft_before_send(terminal_id, metadata, provider)
    with _memory_injected_lock:
        _memory_injected_terminals.add(terminal_id)
    backend.send_keys(
        metadata["tmux_session"],
        metadata["tmux_window"],
        message,
        enter_count=enter_count,
        force_bracketed_paste=True,
        submit_delay=provider.paste_submit_delay if provider else 0.3,
    )
    injection_observation = status_monitor.mark_injection_completed(terminal_id)
    if on_submitted is not None:
        on_submitted(injection_observation)
    if preserved is not None:
        preserved.restore(backend)
    if provider:
        provider.mark_input_received()
    update_last_active(terminal_id)
    if registry is not None and sender_id is not None and orchestration_type is not None:
        dispatch_plugin_event(
            registry,
            "post_send_message",
            PostSendMessageEvent(
                session_id=metadata["tmux_session"],
                sender=sender_id,
                receiver=terminal_id,
                message=original_message or message,
                orchestration_type=orchestration_type,
            ),
        )
    return injection_observation


def send_special_key(terminal_id: str, key: str) -> bool:
    """Send a tmux special key sequence (e.g., C-d, C-c) to terminal.

    Unlike send_input(), this sends the key as a tmux key name (not literal text)
    and does not append a carriage return. Used for control signals like Ctrl+D (EOF).

    Args:
        terminal_id: Target terminal identifier
        key: Tmux key name (e.g., "C-d", "C-c", "Escape")

    Returns:
        True if the key was sent successfully

    Raises:
        ValueError: If terminal not found
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Arm StatusMonitor stickiness: special keys (Enter on a permission
        # prompt, C-c interrupting work, C-d sending EOF) all initiate a new
        # processing cycle that must be allowed to push past any latched
        # ready status.
        status_monitor.notify_input_sent(terminal_id)
        get_backend().send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)

        update_last_active(terminal_id)
        logger.info(f"Sent special key '{key}' to terminal: {terminal_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send special key to terminal {terminal_id}: {e}")
        raise


def exit_terminal_cli(terminal_id: str) -> None:
    """Send the provider-specific exit command to gracefully shut down the CLI.

    Mirrors the ``POST /terminals/{id}/exit`` endpoint: resolve the provider,
    send ``provider.exit_cli()`` — as a tmux key sequence when it is one (e.g.
    ``C-d``), else as literal input (e.g. ``/exit``). This is the graceful CLI
    shutdown that should precede ``delete_terminal`` (which goes straight to
    ``kill_window``). Both the endpoint and ``run_agent_step`` call this so the
    exit-then-delete lifecycle is implemented once.

    Raises:
        ValueError: if no provider is registered for ``terminal_id``.
    """
    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        raise ValueError(f"Provider not found for terminal {terminal_id}")
    exit_command = provider.exit_cli()
    # Some providers use tmux key sequences (e.g., "C-d" for Ctrl+D) instead of
    # text commands (e.g., "/exit"). Key sequences must be sent via
    # send_special_key() to be interpreted by tmux, not as literal text.
    if exit_command.startswith(("C-", "M-")):
        send_special_key(terminal_id, exit_command)
    else:
        send_input(terminal_id, exit_command)


def get_output(terminal_id: str, mode: OutputMode = OutputMode.FULL) -> str:
    """Get terminal output.

    ``FULL`` mode returns the StatusMonitor rolling buffer (the streamed output
    accumulated from the FIFO pipeline), which is bounded to the most recent
    ``STATE_BUFFER_MAX`` bytes (8KB); it falls back to a tmux history capture
    only when that buffer is empty. This is a deliberate trade-off in the
    event-driven architecture (instant, no tmux call) — it is *not* unbounded
    scrollback, so very long sessions are truncated to the tail. Use the
    on-disk ``{id}.log`` (LogWriter) or the delete-time ``{id}.scrollback``
    snapshot when complete history is required.

    For ``LAST`` mode, if the provider declares ``extraction_retries > 0``,
    retries extraction with 10 s delays between attempts.  This handles
    TUI-based providers (e.g. Antigravity CLI's renderer) whose notification
    spinners can temporarily obscure response text in the tmux capture buffer.

    If the provider exposes an ``extraction_tail_lines`` attribute, that
    fixed value is used for the history capture and the escalating-fetch
    logic below is skipped.

    Otherwise, extraction uses an escalating fetch strategy: start with a
    small capture window and widen until the response marker is found.
    Steps: 200 -> 500 -> 1000 -> 5000.  If no marker is found at 5000 lines,
    the raw tail is returned with a [PARTIAL RESPONSE] prefix so the caller
    knows the output may be incomplete.
    """
    # Escalation steps used when the provider does not declare extraction_tail_lines.
    _ESCALATION_STEPS = [200, 500, 1000, 5000]

    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Get output from StatusMonitor buffer (instant, no tmux call)
        full_output = status_monitor.get_buffer(terminal_id)
        if not full_output:
            # Fallback to backend history only if buffer not available (edge case)
            full_output = get_backend().get_history(
                metadata["tmux_session"], metadata["tmux_window"]
            )

        if mode == OutputMode.FULL:
            return full_output
        elif mode == OutputMode.LAST:
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                raise ValueError(f"Provider not found for terminal {terminal_id}")

            # If the provider pins a fixed scrollback depth, honour it and skip
            # escalation — the provider knows what it needs.
            fixed_extract_lines = getattr(provider, "extraction_tail_lines", None)
            if fixed_extract_lines is not None:
                full_output = get_backend().get_history(
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    tail_lines=fixed_extract_lines,
                )
                retries = provider.extraction_retries
                last_err: Exception | None = None
                for attempt in range(1 + retries):
                    try:
                        if attempt > 0:
                            time.sleep(10.0)
                            full_output = get_backend().get_history(
                                metadata["tmux_session"],
                                metadata["tmux_window"],
                                tail_lines=fixed_extract_lines,
                            )
                        return provider.extract_last_message_from_script(full_output)
                    except ValueError as exc:
                        last_err = exc
                        logger.debug(
                            "Output extraction attempt %d/%d for %s failed: %s",
                            attempt + 1,
                            1 + retries,
                            terminal_id,
                            exc,
                        )
                raise last_err  # type: ignore[misc]

            # Escalating fetch: try progressively larger capture windows until
            # the response marker is found or we hit the cap.
            last_err = None
            full_output = ""
            for step_lines in _ESCALATION_STEPS:
                full_output = get_backend().get_history(
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    tail_lines=step_lines,
                )
                try:
                    result = provider.extract_last_message_from_script(full_output)
                    if step_lines > _ESCALATION_STEPS[0]:
                        logger.debug(
                            "get_output: %s marker found at %d lines",
                            terminal_id,
                            step_lines,
                        )
                    return result
                except ValueError as exc:
                    last_err = exc
                    logger.debug(
                        "get_output: %s no marker at %d lines, escalating",
                        terminal_id,
                        step_lines,
                    )

            # All tail-based steps failed — try full scrollback before giving up.
            logger.debug(
                "get_output: %s escalation exhausted, trying full_history",
                terminal_id,
            )
            full_output = get_backend().get_history(
                metadata["tmux_session"],
                metadata["tmux_window"],
                full_history=True,
            )
            try:
                result = provider.extract_last_message_from_script(full_output)
                logger.debug("get_output: %s marker found in full_history", terminal_id)
                return result
            except ValueError:
                pass

            # Full scrollback also failed — distinguish overflow from no response.
            # If the buffer is close to full (>=90% of last escalation cap), the
            # response marker was likely produced but pushed past the scrollback
            # limit (overflow).  If the buffer is mostly empty, the agent never
            # produced a text response (e.g. only tool calls, crash, or timeout).
            actual_lines = full_output.count("\n") + 1
            overflow_threshold = int(_ESCALATION_STEPS[-1] * 0.9)
            if actual_lines >= overflow_threshold:
                logger.warning(
                    "get_output: %s response marker not found, buffer near-full "
                    "(%d lines >= %d threshold) — likely overflow",
                    terminal_id,
                    actual_lines,
                    overflow_threshold,
                )
                return (
                    f"[PARTIAL RESPONSE - response marker not found, buffer overflow likely "
                    f"({actual_lines} lines retrieved)]\n{full_output}"
                )
            else:
                logger.warning(
                    "get_output: %s response marker not found, buffer sparse "
                    "(%d lines < %d threshold) — agent likely produced no text response",
                    terminal_id,
                    actual_lines,
                    overflow_threshold,
                )
                return (
                    f"[NO RESPONSE - agent completed without producing a text response "
                    f"({actual_lines} lines in buffer)]\n{full_output}"
                )

    except Exception as e:
        logger.error(f"Failed to get output from terminal {terminal_id}: {e}")
        raise


def peek_terminal(terminal_id: str, lines: int = 40) -> str:
    """Return the rendered pane tail for a terminal through the active backend."""
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        raise ValueError(f"Terminal '{terminal_id}' not found")

    capped_lines = max(1, min(int(lines), MAX_PEEK_TERMINAL_LINES))
    return get_backend().get_history(
        metadata["tmux_session"],
        metadata["tmux_window"],
        tail_lines=capped_lines,
        strip_escapes=True,
    )


def provider_session_owner(session_uuid: str) -> dict:
    saw_error = False
    for terminal in list_terminals_by_provider_session_id(session_uuid):
        state = get_backend().window_liveness(terminal["tmux_session"], terminal["tmux_window"])
        if state == "live":
            return {"state": "live", "terminal_id": terminal["id"]}
        saw_error = saw_error or state == "error"
    return {"state": "error" if saw_error else "gone", "terminal_id": None}


def delete_terminal(terminal_id: str, registry: PluginRegistry | None = None) -> bool:
    quiesce_deferred_terminal_sync(terminal_id)
    return _delete_terminal_core(terminal_id, registry=registry)


def quiesce_deferred_terminals_sync(terminals: list[dict]) -> None:
    for terminal in terminals:
        quiesce_deferred_terminal_sync(terminal["id"])


def quiesce_deferred_session_sync(session_name: str) -> None:
    """Quiesce schedule-time session members before the leased DB snapshot."""
    with _deferred_tasks_lock:
        terminal_ids = [
            terminal_id
            for terminal_id, record in _deferred_tasks_by_terminal.items()
            if record.session_name == session_name
        ]
    for terminal_id in terminal_ids:
        quiesce_deferred_terminal_sync(terminal_id)


async def quiesce_deferred_terminals(terminals: list[dict]) -> None:
    for terminal in terminals:
        await quiesce_deferred_terminal(terminal["id"])


def preflight_session_teardown(terminals: list[dict]) -> None:
    """Reject a session teardown before mutation when any UUID owner is provisional."""
    from cli_agent_orchestrator.services.provider_session_lease import (
        provider_session_lease_held,
    )

    for terminal in terminals:
        metadata = get_terminal_metadata(terminal["id"])
        session_uuid = metadata.get("provider_session_id") if metadata else None
        if session_uuid and provider_session_lease_held(session_uuid):
            raise RuntimeError("resume_in_progress")


def _delete_terminal_under_lease(
    terminal_id: str,
    lease_token,
    registry: PluginRegistry | None = None,
    preserve_warm_intent: bool = False,
    require_confirmed_death: bool = False,
    quarantine_session_uuid: str | None = None,
    uuid_lease_token=None,
) -> Dict:
    """Delete terminal and kill its tmux window."""
    from cli_agent_orchestrator.services.rebind_lease import validate_rebind_lease

    validate_rebind_lease(terminal_id, lease_token)

    provisional = get_terminal_metadata(terminal_id)
    provisional_uuid = provisional.get("provider_session_id") if provisional else None
    if provisional_uuid:
        from cli_agent_orchestrator.services.provider_session_lease import (
            provider_session_lease_held,
            validate_provider_session_lease,
        )

        if provider_session_lease_held(provisional_uuid):
            try:
                validate_provider_session_lease(provisional_uuid, uuid_lease_token)
            except Exception as exc:
                raise RuntimeError("resume_in_progress") from exc
            if not require_confirmed_death:
                raise RuntimeError("resume_in_progress")

    def detach_observation(metadata: Dict, *, unregister: bool = True) -> None:
        if unregister:
            svc = get_herdr_inbox_service()
            if svc:
                try:
                    svc.unregister_terminal(terminal_id)
                except Exception as exc:
                    logger.warning(
                        f"Failed to unregister terminal {terminal_id} from herdr inbox: {exc}"
                    )
        try:
            get_backend().stop_pipe_pane(metadata["tmux_session"], metadata["tmux_window"])
        except Exception as exc:
            logger.warning(f"Failed to stop pipe-pane for {terminal_id}: {exc}")
        try:
            fifo_manager.stop_reader(terminal_id)
        except Exception as exc:
            logger.warning(f"Failed to stop FIFO reader for {terminal_id}: {exc}")
        try:
            status_monitor.clear_terminal(terminal_id)
        except Exception as exc:
            logger.warning(f"Failed to clear state detector for {terminal_id}: {exc}")

    try:
        if not require_confirmed_death:
            svc = get_herdr_inbox_service()
            if svc:
                try:
                    svc.unregister_terminal(terminal_id)
                except Exception as exc:
                    logger.warning(
                        f"Failed to unregister terminal {terminal_id} from herdr inbox: {exc}"
                    )
        # Reuse the provisional-owner read so rollback ordering does not add a
        # second observation read before kill.
        metadata = provisional

        if metadata:
            # Snapshot scrollback + metadata before killing (for debugging/restore)
            try:
                # Capture plain text full scrollback (no -e, no line cap)
                scrollback = get_backend().get_history(
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    strip_escapes=True,
                    full_history=True,
                )
                scrollback_path = TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"
                scrollback_path.write_text(scrollback, encoding="utf-8")

                import json as _json

                snapshot = {
                    "terminal_id": terminal_id,
                    "session_name": metadata["tmux_session"],
                    "window_name": metadata["tmux_window"],
                    "agent_profile": metadata.get("agent_profile"),
                    "provider": metadata["provider"],
                    "working_directory": get_backend().get_pane_working_directory(
                        metadata["tmux_session"], metadata["tmux_window"]
                    ),
                    "allowed_tools": metadata.get("allowed_tools"),
                    "caller_id": metadata.get("caller_id"),
                }
                snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
                snapshot_path.write_text(_json.dumps(snapshot, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning(f"Failed to snapshot terminal {terminal_id}: {e}")

            # Ordinary deletion detaches observation before killing. Confirmed-death
            # rollback keeps it attached until death is proven so an uncertain live
            # owner remains observable and diagnostically authoritative.
            if not require_confirmed_death:
                detach_observation(metadata, unregister=False)

            # Kill the tmux window (this terminates the agent process)
            try:
                get_backend().kill_window(metadata["tmux_session"], metadata["tmux_window"])
            except Exception as e:
                logger.warning(f"Failed to kill tmux window for {terminal_id}: {e}")
            if require_confirmed_death:
                try:
                    death = get_backend().window_liveness(
                        metadata["tmux_session"], metadata["tmux_window"]
                    )
                except Exception:
                    death = "error"
                if death != "gone":
                    from cli_agent_orchestrator.clients.database import quarantine_terminal_owner

                    try:
                        quarantined = quarantine_terminal_owner(
                            terminal_id, quarantine_session_uuid, "rollback_kill_uncertain"
                        )
                    except Exception as exc:
                        raise RuntimeError("quarantine_persist_failed") from exc
                    if not quarantined:
                        raise RuntimeError("quarantine_persist_failed")
                    return {
                        "terminal_deleted": False,
                        "intent_deleted": False,
                        "intent_error": None,
                        "intent_retain_reason": None,
                        "rollback_kill_uncertain": True,
                    }
                detach_observation(metadata)

        # Cleanup provider state and database record
        provider_manager.cleanup_provider(terminal_id)
        with _memory_injected_lock:
            _memory_injected_terminals.discard(terminal_id)
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        stalled_callback_watchdog.clear_terminal(terminal_id)
        from cli_agent_orchestrator.services.inbox_service import clear_terminal_delivery_state

        clear_terminal_delivery_state(terminal_id)
        try:
            from cli_agent_orchestrator.services.auto_responder import auto_responder

            auto_responder.clear_terminal(terminal_id)
        except Exception as e:
            logger.warning(f"Failed to clear auto-responder for {terminal_id}: {e}")
        # Drop any per-curator dispatch lock so the registry doesn't grow
        # forever as memory_manager terminals come and go.
        from cli_agent_orchestrator.services.memory_service import _curator_locks

        _curator_locks.pop(terminal_id, None)
        deletion = delete_terminal_and_warm_intent(
            terminal_id,
            preserve_warm_intent=preserve_warm_intent,
        )
        deleted = deletion["terminal_deleted"]
        intent_deleted = deletion["intent_deleted"]
        intent_error = None
        logger.info(f"Deleted terminal: {terminal_id}")
        if deleted and metadata:
            dispatch_plugin_event(
                registry,
                "post_kill_terminal",
                PostKillTerminalEvent(
                    session_id=metadata["tmux_session"],
                    terminal_id=terminal_id,
                    agent_name=metadata.get("agent_profile"),
                ),
            )
        return {
            "terminal_deleted": deleted,
            "intent_deleted": intent_deleted,
            "intent_error": intent_error,
            "intent_retain_reason": "keep_bases" if preserve_warm_intent else None,
            "rollback_kill_uncertain": False,
        }

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise
