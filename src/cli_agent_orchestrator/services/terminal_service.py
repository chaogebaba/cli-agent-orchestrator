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
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, assert_never, cast

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    _utcnow,
    claim_deferred_init_failure,
    create_digest_pending_notice,
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
    list_deferred_init_overdue_pending_rows,
    list_deferred_init_recovery_rows,
    list_siblings_by_group_prefix,
    list_terminals_by_provider_session_id,
    list_terminals_by_session,
    mark_terminal_init_ready,
    settle_pending_orphan_messages,
    terminal_exists,
    update_last_active,
    update_provider_session_snapshot,
    update_terminal_group,
    update_terminal_metadata,
    update_terminal_shell_command,
    update_terminal_tmux_window,
)
from cli_agent_orchestrator.constants import (
    FIFO_DIR,
    PIPE_LIVENESS_TAIL_LINES,
    PYTE_SCREEN_ROWS,
    SESSION_PREFIX,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine
from cli_agent_orchestrator.models.native_publish import DispatchTxn, NativePublishRequest
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import RecoveryState, Terminal, TerminalStatus
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
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    probe_kiro_capabilities,
    requested_kiro_capabilities,
)
from cli_agent_orchestrator.providers.manager import get_provider_class, provider_manager
from cli_agent_orchestrator.services import base_digest_service, worktree_service
from cli_agent_orchestrator.services.deferred_dispatcher import (
    DeferredCall,
    DeferredExecutorSaturated,
    dispatcher,
)
from cli_agent_orchestrator.services.draft_guard import (
    DeliveryDeferredError,
    apply_prepared_native_stash,
    prepare_native_stash_before_send,
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
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor, status_monitor
from cli_agent_orchestrator.services.step_output_store import _validate_key_part
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path
from cli_agent_orchestrator.utils.provider_auth import ProviderAuthRefreshFailed
from cli_agent_orchestrator.utils.provider_plane import NativeHomeIsolationUnavailable
from cli_agent_orchestrator.utils.sandbox_guard import (
    bind_pane_identity,
    is_sandbox,
    require_provider_admitted,
)
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
    wait_until_status,
)

logger = logging.getLogger(__name__)


class _LegacyCreateTerminalPublisher(Protocol):
    def __call__(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        provider: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        shell_command: Optional[str] = None,
        caller_id: Optional[str] = None,
        provider_session_id: Optional[str] = None,
        init_state: str = "ready",
        init_started_at: Optional[datetime] = None,
        init_owner_epoch: Optional[str] = None,
        init_deadline_s: Optional[float] = None,
    ) -> Dict[str, Any]: ...


class _LegacyWarmTerminalPublisher(Protocol):
    def __call__(
        self,
        *,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        provider: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[list[str]],
        caller_id: Optional[str],
        parent_base_name: Optional[str],
        fork_mode: Optional[str],
        cas_hook: Any = None,
        init_state: str = "ready",
        init_started_at: Optional[datetime] = None,
        init_owner_epoch: Optional[str] = None,
        init_deadline_s: Optional[float] = None,
    ) -> Dict[str, Any]: ...


# Upper bound (bytes) on a single offset-ranged read of a terminal log
# (U5 / #504, BR-2). ``read_output_range`` clamps its ``length`` to this so a
# caller (playback fetching output around a selected event) can never trigger
# an unbounded read of a large log file. 1 MiB is a defensible ceiling: it is
# far larger than any realistic per-event output window (the rolling
# STATE_BUFFER_MAX is only 8 KiB) yet bounds the worst-case allocation and
# response size to a fixed, predictable amount regardless of on-disk log size.
TERMINAL_RANGE_MAX_LENGTH = 1024 * 1024

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
DISK_SPACE_FLOOR_GB = 3.0
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


def _preflight_disk_space(path: str, floor_gb: float = DISK_SPACE_FLOOR_GB) -> None:
    """Raise RuntimeError if free disk on *path*'s filesystem is below *floor_gb*."""
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    if free_gb < floor_gb:
        raise RuntimeError(
            f"disk_space_low: {free_gb:.1f}GB free on {path} "
            f"(floor: {floor_gb}GB). Refusing to create terminal — free disk space first."
        )


async def seed_resume_bootstrap(agent_profile: str, provider_name: str, cwd: str):
    """Return an authoritative resume ForkContext for seed-capable providers."""
    provider_class = get_provider_class(provider_name)
    if provider_class.supports_seed_resume_identity is not True:
        return None
    try:
        from cli_agent_orchestrator.models.terminal import ForkContext

        session_uuid = await asyncio.to_thread(
            provider_class.seed_resume_identity, cwd, agent_profile
        )
        return ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name="seed",
            provider=provider_name,
            initial_preamble="",
        )
    except Exception as exc:
        logger.error(f"seed_resume_bootstrap failed for {agent_profile}/{provider_name}: {exc}")
        raise


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
    if cwd is None:
        # F26 D5: a deleted/unavailable pane cwd must fail through this site's
        # own failure channel (a recognized _PERSIST_FAILURE_CODES code), never
        # escape as a TypeError from quote(None) inside capture_session_uuid /
        # validate_session_artifact.
        raise RuntimeError("terminal_cwd_unavailable")
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
            tmux_session = metadata["tmux_session"]
            tmux_window = metadata["tmux_window"]
            state = backend.window_liveness(tmux_session, tmux_window)
            if state == "live":
                continue
            if state == "error":
                continue
            if getattr(backend, "supports_identity_readback", False) is not True:
                continue

            enum_state, windows = backend.enumerate_windows(tmux_session)
            enumeration_failed = enum_state == "error"
            if enumeration_failed:
                windows = []  # type narrowing — loop won't execute

            matches: list[str] = []
            unreadable: list[str] = []
            for window in windows:  # type: ignore[union-attr]
                name = str(window["name"])
                result = backend.read_pane_identity(tmux_session, name)
                if result.reason in {
                    "read_error",
                    "pane_cardinality",
                    "incarnation_changed",
                }:
                    unreadable.append(name)
                    continue
                if result.identity == terminal_id:
                    matches.append(name)
            if len(matches) > 1:
                logger.warning(
                    "purge_identity_ambiguous terminal=%s windows=%s", terminal_id, matches
                )
                continue
            match = matches[0] if matches else None
            if match is not None:
                if not update_terminal_tmux_window(terminal_id, match):
                    logger.warning(
                        "purge_rename_conflict terminal=%s window=%s", terminal_id, match
                    )
                else:
                    logger.info(
                        "purge_reconciled_rename terminal=%s old=%s new=%s",
                        terminal_id,
                        tmux_window,
                        match,
                    )
                continue
            if enumeration_failed or unreadable:
                logger.warning(
                    "purge_inconclusive terminal=%s session=%s enumeration_failed=%s unreadable=%d",
                    terminal_id,
                    tmux_session,
                    enumeration_failed,
                    len(unreadable),
                )
                continue
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
        except Exception:
            logger.exception("purge_row_failed terminal=%s", terminal_id)
            continue
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
    # F138: Abandon the incarnation on creation failure
    try:
        from cli_agent_orchestrator.clients.database import (
            ProcessIncarnationModel,
            SessionLocal,
            f138_abandon_incarnation,
        )

        with SessionLocal() as db:
            inc = (
                db.query(ProcessIncarnationModel)
                .filter_by(terminal_id=terminal_id, state="launching")
                .order_by(ProcessIncarnationModel.created_at.desc())
                .first()
            )
            if inc is not None:
                f138_abandon_incarnation(inc.id)
    except Exception:
        pass

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


def _capture_f138_issuance_context() -> tuple[int | None, str | None]:
    """D18: Capture issuance context for pre-issuance fence.

    Returns (issuance_ticks, issuance_boot_id).
    issuance_ticks: integer clock ticks since boot (CLOCK_BOOTTIME * SC_CLK_TCK).
    issuance_boot_id: current kernel boot_id string.
    """
    issuance_boot_id: str | None = None
    issuance_ticks: int | None = None
    try:
        issuance_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    try:
        issuance_ticks = int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, AttributeError):
        pass
    return issuance_ticks, issuance_boot_id


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
    dispatch_barrier: dict[str, object] | None = None,
    park_warm: bool = False,
    engine: Optional[KiroEngine | str] = None,
    kiro_capability_probe: Optional[Callable[[KiroEngine, set[str]], KiroCapabilities]] = None,
    model: Optional[str] = None,
    lifecycle: str | None = None,
    use_worktree: bool = False,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
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
            is not persisted for later windows. Per-step vars (e.g. workflow
            routing ids) must reach the window even inside an existing session.
            See issues #248 and #408.
        caller_id: Terminal ID of the supervisor that created this terminal
            via handoff/assign. Recorded so send_message can route callbacks
            structurally instead of parsing IDs out of message text (issue #284).
            None for operator-launched terminals.
        engine: Explicit Kiro engine. For Kiro, it must agree with the selected
            profile's engine when both are present; omitted resolves to v2.
        kiro_capability_probe: Optional test seam for the bounded wrapper probe.
        model: Explicit per-call model override, forwarded to the provider
            (where supported -- see each provider's own __init__) ahead of
            the provider's existing profile/providers.toml resolution. Lets a
            caller (e.g. MCP handoff/assign's own `model` parameter) pin a
            specific model for one worker without needing a dedicated agent profile.
            None leaves that existing resolution chain unchanged.
        use_worktree: If True, provision an isolated ``git worktree`` (issue
            #100) for this terminal instead of using ``working_directory`` as
            given -- resolves the repo root from ``working_directory`` (or the
            server's own cwd when unset), creates a fresh worktree on its own
            branch there, and overrides ``working_directory`` to the new
            worktree path before the tmux session/window is created. Requires
            the resolved directory to actually be inside a git repository.
            Default False = behavior unchanged.
        group: Ordered, general-to-specific grouping array for list_siblings
            discovery (#432). None = this terminal opts out of discovery.
        metadata: Free-form JSON describing what this terminal is doing.
            Also updatable later by the running agent via the
            ``update_metadata`` MCP tool.

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
    else:
        working_directory = resolve_and_validate_path(os.getcwd(), description="Working directory")
    _preflight_disk_space(working_directory)
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
        profile_lifecycle = getattr(early_profile, "lifecycle", None)
        if profile_lifecycle not in {"ephemeral", "sticky"}:
            profile_lifecycle = None
        resolved_lifecycle = lifecycle or profile_lifecycle or "ephemeral"
        if resolved_lifecycle not in {"ephemeral", "sticky"}:
            raise ValueError("invalid_terminal_lifecycle")

        # Existing-session managed creates take shared authority before any
        # create_window call. The direct CLI restore path is intentionally
        # unmanaged and does not pass through this function.
        if not new_session and session_lifecycle_lease_token is None:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                acquire_session_lifecycle_shared,
            )

            session_lifecycle_lease_token = acquire_session_lifecycle_shared(session_name)
            if session_lifecycle_lease_token is None:
                raise RuntimeError("resume_in_progress")
            owned_lifecycle_lease = True
        elif not new_session and not resume_uuid:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                validate_session_lifecycle_shared,
            )

            validate_session_lifecycle_shared(session_name, session_lifecycle_lease_token)
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

    persona_plan = None
    session_created = False  # tracks whether THIS call created the tmux session
    window_created = False
    fifo_attached = False
    db_created = False
    # Reassigned to the resolved repo root once a worktree is actually created
    # below (Step 1b), so the failure-cleanup path (the `except` block) knows
    # whether there is a worktree to roll back too. Still None if Step 1b never
    # ran (use_worktree=False) or itself failed before create_worktree returned.
    worktree_repo_root: Optional[str] = None
    try:
        # Resolve profile policy and Kiro engine BEFORE allocating any backend
        # resource. Capability probe runs first so a missing wrapper flag fails
        # closed with no window, database row, FIFO, Herdr registration, or
        # provider process (F107: KAS is enabled once the probe accepts it).
        try:
            profile = load_agent_profile(agent_profile)
        except FileNotFoundError:
            profile = None
        # Production loaders return AgentProfile. Treat a test double or an
        # otherwise malformed object as no selected profile rather than
        # accepting arbitrary attributes as configuration.
        if profile is not None and not isinstance(profile, AgentProfile):
            profile = None

        if provider == ProviderType.KIRO_CLI.value:
            resolved_engine = resolve_kiro_engine(
                explicit=engine,
                profile=getattr(profile, "engine", None),
            )
            if allowed_tools is None and profile is not None:
                from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

                mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
                allowed_tools = resolve_allowed_tools(
                    profile.allowedTools, profile.role, mcp_server_names
                )
            # Kiro runs headlessly, so current CAO behavior always bypasses its
            # interactive approval prompt. Profile/MCP policy remains enforced
            # by CAO, while unrestricted profiles additionally force legacy UI.
            # F107 B2 (build-gate r1 B1): resolve the effective model ONCE via
            # the same seam as launch (spawn override > providers.toml >
            # profile field) BEFORE the pre-allocation probe, and pass that
            # value to BOTH requested_kiro_capabilities and create_provider.
            # Probing only `model or profile.model` would let a toml-only
            # model = "auto" allocate then fail at argv construction.
            if model:
                resolved_model = model
            else:
                provider_defaults = get_provider_defaults("kiro_cli")
                profile_name = getattr(profile, "name", None) or agent_profile
                profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
                resolved_model = resolve_provider_string_option(
                    profile_defaults,
                    provider_defaults,
                    profile,
                    "model",
                    "model",
                )
            model = resolved_model
            requested = requested_kiro_capabilities(
                resolved_engine,
                model=model,
                yolo=True,
            )
            probe = kiro_capability_probe or probe_kiro_capabilities
            await asyncio.to_thread(probe, resolved_engine, requested)
        else:
            if engine is not None:
                raise ValueError("Kiro engine selection is only valid for provider 'kiro_cli'")
            resolved_engine = None

        # Resolve tool policy before persistence for non-Kiro providers too.
        if allowed_tools is None and profile is not None:
            from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

            mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
            allowed_tools = resolve_allowed_tools(
                profile.allowedTools, profile.role, mcp_server_names
            )

        # Step 1: Generate unique identifiers
        terminal_id = terminal_id or generate_terminal_id()
        assert terminal_id is not None
        if lease_token is not None:
            from cli_agent_orchestrator.services.rebind_lease import validate_rebind_lease

            validate_rebind_lease(terminal_id, lease_token)

        from cli_agent_orchestrator.models.agent_profile import ContextPolicy

        context_policy = getattr(early_profile, "contextPolicy", None)
        if isinstance(context_policy, ContextPolicy):
            if is_sandbox():
                logger.warning(
                    "Terminal %s profile %s requested contextPolicy in a sandbox; "
                    "shared-auth provider isolation takes precedence",
                    terminal_id,
                    agent_profile,
                )
            else:
                from cli_agent_orchestrator.utils.persona_context import compose_persona_plan

                persona_plan = compose_persona_plan(
                    terminal_id,
                    provider,
                    agent_profile,
                    context_policy,
                    working_directory,
                )
        # F138: Reserve process incarnation token before window creation.
        # Process-less providers (has_process_child=False) skip this entirely.
        _f138_incarnation_id: str | None = None
        _f138_token: str | None = None
        _f138_generation: int = 0
        _has_process_child = True
        try:
            _provider_cls = get_provider_class(provider)
            _has_process_child = getattr(_provider_cls, "has_process_child", True)
        except Exception:
            pass

        # F138-R6 D24: sandbox fixture variant override — manifest is authoritative
        # for process-less determination before reservation (class attr is too late).
        if _has_process_child and provider == "mock_cli":
            try:
                from cli_agent_orchestrator.utils.provider_plane import (
                    load_active_fixture_provider,
                )

                _fixture_cap = load_active_fixture_provider("mock_cli")
                if _fixture_cap.variant == "process-less":
                    _has_process_child = False
            except Exception:
                pass  # Non-sandbox or manifest read failure → class attr stands

        if _has_process_child:
            from cli_agent_orchestrator.clients.database import f138_reserve_incarnation
            from cli_agent_orchestrator.services.orphan_reconcile_service import (
                generate_incarnation_token,
                hash_token,
            )

            _f138_token = generate_incarnation_token()
            _f138_token_hash = hash_token(_f138_token)
            # Get current lifecycle_generation from the terminal_id's existing row
            # (for new terminals it will be incremented by the create call below)
            _f138_generation = 0
            try:
                _existing_meta = get_terminal_metadata(terminal_id)
                if _existing_meta:
                    _f138_generation = int(_existing_meta.get("lifecycle_generation", 0)) + 1
                else:
                    _f138_generation = 1  # new terminal starts at gen 1
            except Exception:
                _f138_generation = 1

            # D18: Capture issuance context for pre-issuance fence.
            _f138_issuance_ticks, _f138_issuance_boot_id = _capture_f138_issuance_context()

            _f138_incarnation_id = f138_reserve_incarnation(
                terminal_id=terminal_id,
                terminal_generation=_f138_generation,
                token=_f138_token,
                token_hash=_f138_token_hash,
                owner_uid=os.getuid(),
                provider=provider,
                issuance_ticks=_f138_issuance_ticks,
                issuance_boot_id=_f138_issuance_boot_id,
            )

        env_vars = bind_pane_identity(
            env_vars, terminal_id, plan=persona_plan, incarnation_token=_f138_token
        )

        window_name = generate_window_name(agent_profile, terminal_id)

        # Step 1b: Provision an isolated git worktree (issue #100, Phase 1) before
        # the tmux session/window below consumes `working_directory` -- the
        # worktree's own path REPLACES whatever `working_directory` was given
        # (explicit or caller-inherited), so the terminal always launches inside
        # its own isolated checkout rather than the shared one it would
        # otherwise have used.
        if use_worktree:
            # `find_repo_root`/`create_worktree` are synchronous `subprocess.run`
            # calls (a full worktree checkout can take seconds to tens of
            # seconds on a large repo); `create_terminal` is awaited directly on
            # the shared event loop, so running them in-line here would freeze
            # every other cao-server request (status monitor ticks, inbox
            # delivery, unrelated terminal calls) for the duration. Offload to a
            # thread, same posture as `delete_terminal`'s own blocking subprocess
            # work (see its `run_in_executor` call site in api/main.py).
            worktree_repo_root = await asyncio.to_thread(
                worktree_service.find_repo_root, working_directory or os.getcwd()
            )
            working_directory = await asyncio.to_thread(
                worktree_service.create_worktree, worktree_repo_root, terminal_id
            )
            # F121: Build the CAO-owned authority record for branch integrity
            # verification. Written in the same INSERT as the terminal row below.
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            _worktree_info_dict: dict[str, str] | None = {
                "repo_root": worktree_repo_root,
                "worktree_path": working_directory,
                "expected_branch": worktree_service.branch_for(terminal_id),
                "terminal_id": terminal_id,
                "provisioned_at": _dt.now(_tz.utc).isoformat(),
            }
        else:
            _worktree_info_dict = None

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

        parent_writer = getattr(get_backend(), "set_window_parent", None)
        if callable(parent_writer):
            parent_writer(session_name, window_name, caller_id)

        # Step 3: Build a runtime skill catalog only for providers that consume
        # it at launch time (see RUNTIME_SKILL_PROMPT_PROVIDERS).
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
            fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

            # Reader must exist BEFORE pipe-pane starts so it captures from the start.
            # Enroll it in the pipe-pane liveness watchdog (issue #388).
            def _probe_pane(s=session_name, w=window_name) -> str:
                return get_backend().get_history(s, w, tail_lines=PIPE_LIVENESS_TAIL_LINES)

            def _rearm_pipe(s=session_name, w=window_name, p=str(fifo_path)) -> None:
                get_backend().stop_pipe_pane(s, w)
                get_backend().pipe_pane(s, w, p)

            try:
                fifo_manager.create_reader(
                    terminal_id,
                    pane_probe=_probe_pane,
                    rearm=_rearm_pipe,
                    terminal_generation=_f138_generation,
                    incarnation_id=_f138_incarnation_id,
                )
                fifo_attached = True
            except Exception as exc:
                if lease_token is not None:
                    raise RuntimeError("fifo_create_failed") from exc
                raise

            # Configure pipe-pane to stream output to the FIFO. This enables
            # real-time event-driven processing via StatusMonitor and LogWriter
            # (LogWriter writes TERMINAL_LOG_DIR/{id}.log from the FIFO). A pane
            # has a single pipe-pane target, so we pipe ONLY to the FIFO.
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
                        if dispatch_barrier is None:
                            cast(
                                _LegacyWarmTerminalPublisher,
                                create_terminal_with_warm_intent,
                            )(
                                terminal_id=terminal_id,
                                tmux_session=session_name,
                                tmux_window=window_name,
                                provider=provider,
                                agent_profile=agent_profile,
                                allowed_tools=allowed_tools,
                                caller_id=caller_id,
                                **(
                                    {"lifecycle": resolved_lifecycle}
                                    if resolved_lifecycle != "ephemeral"
                                    else {}
                                ),
                                parent_base_name=fork_context.base_name,
                                fork_mode=fork_context.mode,
                                engine=(
                                    resolved_engine.value if resolved_engine is not None else None
                                ),
                                group=group,
                                metadata=metadata,
                                worktree_info=_worktree_info_dict,
                                **init_fields,
                            )
                        else:
                            cast(Any, create_terminal_with_warm_intent)(
                                terminal_id=terminal_id,
                                tmux_session=session_name,
                                tmux_window=window_name,
                                provider=provider,
                                agent_profile=agent_profile,
                                allowed_tools=allowed_tools,
                                caller_id=caller_id,
                                **(
                                    {"lifecycle": resolved_lifecycle}
                                    if resolved_lifecycle != "ephemeral"
                                    else {}
                                ),
                                parent_base_name=fork_context.base_name,
                                fork_mode=fork_context.mode,
                                dispatch_barrier=dispatch_barrier,
                                engine=(
                                    resolved_engine.value if resolved_engine is not None else None
                                ),
                                group=group,
                                metadata=metadata,
                                worktree_info=_worktree_info_dict,
                                **init_fields,
                            )
                    else:
                        attempted_resume_uuid = resume_uuid
                        if attempted_resume_uuid:
                            if dispatch_barrier is None:
                                cast(_LegacyCreateTerminalPublisher, db_create_terminal)(
                                    terminal_id,
                                    session_name,
                                    window_name,
                                    provider,
                                    agent_profile,
                                    allowed_tools,
                                    caller_id=caller_id,
                                    **(
                                        {"lifecycle": resolved_lifecycle}
                                        if resolved_lifecycle != "ephemeral"
                                        else {}
                                    ),
                                    provider_session_id=attempted_resume_uuid,
                                    engine=(
                                        resolved_engine.value
                                        if resolved_engine is not None
                                        else None
                                    ),
                                    group=group,
                                    metadata=metadata,
                                    worktree_info=_worktree_info_dict,
                                    **init_fields,
                                )
                            else:
                                cast(Any, db_create_terminal)(
                                    terminal_id,
                                    session_name,
                                    window_name,
                                    provider,
                                    agent_profile,
                                    allowed_tools,
                                    caller_id=caller_id,
                                    **(
                                        {"lifecycle": resolved_lifecycle}
                                        if resolved_lifecycle != "ephemeral"
                                        else {}
                                    ),
                                    provider_session_id=attempted_resume_uuid,
                                    dispatch_barrier=dispatch_barrier,
                                    engine=(
                                        resolved_engine.value
                                        if resolved_engine is not None
                                        else None
                                    ),
                                    group=group,
                                    metadata=metadata,
                                    worktree_info=_worktree_info_dict,
                                    **init_fields,
                                )
                        else:
                            if dispatch_barrier is None:
                                cast(_LegacyCreateTerminalPublisher, db_create_terminal)(
                                    terminal_id,
                                    session_name,
                                    window_name,
                                    provider,
                                    agent_profile,
                                    allowed_tools,
                                    caller_id=caller_id,
                                    **(
                                        {"lifecycle": resolved_lifecycle}
                                        if resolved_lifecycle != "ephemeral"
                                        else {}
                                    ),
                                    engine=(
                                        resolved_engine.value
                                        if resolved_engine is not None
                                        else None
                                    ),
                                    group=group,
                                    metadata=metadata,
                                    worktree_info=_worktree_info_dict,
                                    **init_fields,
                                )
                            else:
                                cast(Any, db_create_terminal)(
                                    terminal_id,
                                    session_name,
                                    window_name,
                                    provider,
                                    agent_profile,
                                    allowed_tools,
                                    caller_id=caller_id,
                                    **(
                                        {"lifecycle": resolved_lifecycle}
                                        if resolved_lifecycle != "ephemeral"
                                        else {}
                                    ),
                                    dispatch_barrier=dispatch_barrier,
                                    engine=(
                                        resolved_engine.value
                                        if resolved_engine is not None
                                        else None
                                    ),
                                    group=group,
                                    metadata=metadata,
                                    worktree_info=_worktree_info_dict,
                                    **init_fields,
                                )
        except Exception as exc:
            if lease_token is not None:
                raise RuntimeError("db_publish_failed") from exc
            raise
        if not resume_uuid and owned_lifecycle_lease:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            release_session_lifecycle_lease(session_lifecycle_lease_token)
            owned_lifecycle_lease = False
            session_lifecycle_lease_token = None
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
                model=model,
                fork_context=fork_context,
                persona_plan=persona_plan,
                engine=resolved_engine,
            )
        except Exception as exc:
            if lease_token is not None:
                raise RuntimeError("provider_construct_failed") from exc
            raise
        allocated_uuid = getattr(provider_instance, "allocated_session_uuid", None)
        if not isinstance(allocated_uuid, str):
            allocated_uuid = None
            engine = (resolved_engine,)

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
                park_warm=park_warm,
                f138_incarnation_id=_f138_incarnation_id,
            )
        else:
            # D21: Exposure boundary = pane+token already bound before initialize
            _f138_sync_exposure_crossed = _f138_incarnation_id is not None
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
            # F138 D21: strict activation in sync path (after launch health)
            if _f138_incarnation_id is not None:
                await _confirm_launch_health(terminal_id, provider_instance)
                from cli_agent_orchestrator.clients.database import (
                    f138_strict_activate,
                )

                _sync_act = f138_strict_activate(_f138_incarnation_id)
                if _sync_act.outcome in ("activated", "already_active"):
                    pass  # expected — continue
                elif _sync_act.outcome == "needs_settlement":
                    raise RuntimeError("incarnation_needs_settlement")
                else:
                    # "missing" with non-null pinned ID = non-durable
                    raise RuntimeError("incarnation_non_durable_missing")
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
            lifecycle=resolved_lifecycle,
            allowed_tools=allowed_tools,
            engine=resolved_engine,
            shell_command=shell_command,
            group=group,
            metadata=metadata,
            status=initial_status,
            last_active=_utcnow(),
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

        # D23: Post-exposure sync settlement — if exposure boundary was crossed,
        # force-reconcile first. Only durable proof allows teardown; non-durable
        # must retain pane/row/FIFO/provider, quarantine, attention, re-raise.
        _sync_exposed = locals().get("_f138_sync_exposure_crossed", False)
        _sync_inc_id = locals().get("_f138_incarnation_id")
        if _sync_exposed and _sync_inc_id is not None:
            from cli_agent_orchestrator.clients.database import (
                f138_emit_attention_message,
                f138_force_reconcile_incarnation,
                set_terminal_recovery_state,
            )

            try:
                fr_result = f138_force_reconcile_incarnation(
                    _sync_inc_id, source="sync_post_exposure"
                )
            except Exception:
                fr_result = None
                logger.exception("f138_sync_post_exposure_force_failed terminal=%s", terminal_id)

            durable = fr_result is not None and fr_result.outcome in (
                "created",
                "job_already_exists",
                "reconciled_proven",
            )

            if not durable:
                # Non-durable: retain physical resources, quarantine, attention, re-raise
                quarantine_ok = False
                try:
                    quarantine_ok = set_terminal_recovery_state(
                        terminal_id,
                        "rollback_kill_uncertain",
                        error=f"sync_post_exposure_non_durable: {repr(e)[:150]}",
                    )
                except Exception:
                    logger.exception("f138_sync_quarantine_failed terminal=%s", terminal_id)
                f138_emit_attention_message(
                    terminal_id,
                    f"[F138] Terminal {terminal_id} sync post-exposure failure "
                    f"with non-durable reconcile "
                    f"(quarantine={'persisted' if quarantine_ok else 'FAILED'}). "
                    f"Physical resources retained. Manual review needed.",
                )
                raise  # re-raise original exception without rollback fallthrough

        # --- Ordinary rollback (pre-exposure or durable-proven) ---
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
            if persona_plan is not None:
                try:
                    from cli_agent_orchestrator.utils.persona_context import cleanup_persona

                    cleanup_persona(terminal_id)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed to clean persona after terminal creation rollback %s: %s",
                        terminal_id,
                        cleanup_exc,
                    )
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
        if session_lifecycle_lease_token is not None and owned_lifecycle_lease:
            from cli_agent_orchestrator.services.session_lifecycle_lease import (
                release_session_lifecycle_lease,
            )

            try:
                release_session_lifecycle_lease(session_lifecycle_lease_token)
            except RuntimeError:
                pass
        if settlement_error:
            raise RuntimeError(settlement_error) from e
        if worktree_repo_root is not None:
            # A worktree WAS created (Step 1b succeeded) before some later step
            # failed -- roll it back too, same best-effort posture as everything
            # else in this block. Without this, a provider-init timeout (or any
            # later failure) on a worktree-backed terminal would leave an orphan
            # worktree + branch behind with no CAO-side record pointing at it.
            # Offloaded to a thread for the same reason Step 1b's create is:
            # `git worktree remove` is a blocking subprocess call and this
            # `except` block still runs on the shared event loop.
            await asyncio.to_thread(
                worktree_service.remove_worktree, worktree_repo_root, terminal_id
            )
        raise


_PERSIST_FAILURE_CODES = {
    "terminal_metadata_missing",
    "identity_persist_failed",
    "shell_baseline_unavailable",
    "terminal_identity_persist_failed",
    "terminal_cwd_unavailable",
    "provider_launch_failed",
}


class _DeferredInitFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderLaunchFailed(_DeferredInitFailure):
    """F124 S6: provider process tree confirmed dead immediately after initialize()."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("provider_launch_failed")
        self.detail = detail


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
    reason: str = "",
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
    base = (
        f"code={code} deadline_s={repr(float(deadline_s))} token={token} "
        f"worker={worker} profile={profile} provider={provider}"
    )
    if reason:
        base += f" reason={reason}"
    # F124 S7: append actionable hint based on pre/post delivery classification
    hint = _notice_action_hint(code)
    if hint:
        base += f" | {hint}"
    return base


# F124 S7: pre-delivery failure codes — task was NOT delivered to the worker
_PRE_DELIVERY_CODES = frozenset(
    {
        "provider_launch_failed",
        "identity_persist_failed",
        "terminal_cwd_unavailable",
        "shell_baseline_unavailable",
        "terminal_metadata_missing",
    }
)

# F124 S7: unknown-delivery codes — watchdog fired but delivery state is ambiguous
_UNKNOWN_DELIVERY_CODES = frozenset(
    {
        "deferred_init_watchdog_deadline",
    }
)


def _notice_action_hint(code: str) -> str:
    """F124 S7: return a plain-text actionable hint for the failure code."""
    if code in _PRE_DELIVERY_CODES:
        return "The assigned task was NOT delivered. " "Re-dispatch with assign if needed."
    if code in _UNKNOWN_DELIVERY_CODES:
        return (
            "Worker initialization timed out; task delivery state is unknown. "
            "Inspect the terminal before re-dispatching."
        )
    # Post-delivery or unclassified code — task MAY have been delivered
    return (
        "Task delivery was attempted but the worker died before confirming receipt. "
        "Re-dispatch with assign if needed."
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
    reason: str = "",
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
            reason=reason,
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


async def sweep_overdue_deferred_inits(registry: PluginRegistry | None = None) -> int:
    """Settle init_pending rows whose deadline has elapsed (runtime watchdog body).

    Returns the number of rows for which settlement was attempted.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    overdue = list_deferred_init_overdue_pending_rows(now)
    if not overdue:
        return 0

    async def _settle_overdue(row: dict) -> None:
        tid = row["id"]
        try:
            await _claim_and_settle_deferred_failure(
                tid,
                f"watchdog-{SERVER_INIT_OWNER_EPOCH}",
                row,
                "deferred_init_watchdog_deadline",
                registry,
                reason="watchdog_deadline_elapsed",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deferred_init_watchdog_settle_failed terminal=%s", tid)

    await asyncio.gather(*[_settle_overdue(row) for row in overdue])
    return len(overdue)


def _fork_refresh_lock(base_name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (loop, base_name)
    lock = _fork_refresh_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _fork_refresh_locks[key] = lock
    return lock


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
    max_iterations = max(1, int((deadline - time.monotonic()) / 0.1 * 3))
    iterations = 0
    while time.monotonic() < deadline and iterations < max_iterations:
        iterations += 1
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
    if iterations >= max_iterations:
        logger.warning("_wait_for_base_ready: iteration cap reached (%d), exiting", max_iterations)
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
        stale, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_compare",
            fork_staleness,
            row,
            deadline=deadline,
        )
        if not stale:
            return stale.preamble
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
        decision, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_digest",
            base_digest_service.evaluate,
            row,
            stale.delta,
            deadline=deadline,
        )
        if isinstance(decision, base_digest_service.DigestPending):
            caller_id = caller_snapshot.get("caller_id")
            if caller_id:
                try:
                    await _tracked_blocking(
                        terminal_id,
                        generation,
                        "abandonable",
                        "fork_refresh_digest_notice",
                        create_digest_pending_notice,
                        caller_id,
                        base_name,
                        base_digest_service.state_key(decision.delta),
                        (
                            "A covered digest artifact is required before refresh."
                            if row.get("digest_head")
                            else "Base has never published a digest."
                        ),
                        deadline=deadline,
                        genesis=row.get("digest_head") is None,
                    )
                except Exception:
                    logger.exception("digest_pending_notice_failed base=%s", base_name)
            return stale_preamble
        if isinstance(decision, base_digest_service.DigestInvalid):
            logger.warning("digest_refresh_invalid base=%s reason=%s", base_name, decision.reason)
            return stale_preamble
        if isinstance(decision, base_digest_service.DigestUnobservable):
            logger.warning(
                "digest_refresh_unobservable base=%s reason=%s", base_name, decision.reason
            )
            return stale_preamble
        if not isinstance(decision, base_digest_service.DigestCovered):
            assert_never(decision)
        dispatched, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_send",
            _dispatch_base_refresh,
            base_terminal_id,
            base_digest_service.refresh_prompt(decision.artifact),
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
        captured = snapshot_result
        if captured.acquisition_error or not captured.git_sha:
            return stale_preamble
        post_dispatch, _ = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            "fork_refresh_compare",
            fork_staleness,
            row,
            deadline=deadline,
        )
        coverage = base_digest_service.coverage(decision.artifact, post_dispatch.delta)
        if isinstance(coverage, base_digest_service.Disproven):
            logger.warning("digest_post_dispatch_mismatch base=%s", base_name)
            return stale_preamble
        if isinstance(coverage, base_digest_service.Unobservable):
            logger.warning(
                "digest_post_dispatch_unobservable base=%s reason=%s",
                base_name,
                coverage.reason,
            )
            return stale_preamble
        if not isinstance(coverage, base_digest_service.Proven):
            assert_never(coverage)
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
            git_sha=captured.git_sha,
            dirty_hashes=captured.dirty_hashes(),
            digest_head=decision.artifact.artifact_sha,
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
        return cast(str, refreshed.preamble)
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


# --- deferred-init submit verification ----------------------------------------
_DEFERRED_SUBMIT_CONFIRM_TIMEOUT = 8.0
_DEFERRED_SUBMIT_MAX_RESUBMITS = 3
_DEFERRED_SUBMIT_STABILITY_DELAY = 0.1
_DEFERRED_STARTED_STATUSES = {
    TerminalStatus.PROCESSING,
    TerminalStatus.COMPLETED,
    TerminalStatus.WAITING_USER_ANSWER,
}


def _normalized_composer_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _worker_is_started_direct(terminal_id: str, provider: Any) -> bool:
    """Direct visible-screen status check bypassing the event-driven status cache.

    The deferred-init retry loop polls ``status_monitor.get_status()`` which
    returns the **cached** status updated only by the event-driven pipeline
    (pyte screener at rising-edge/quiescence edges). When that lags behind
    reality the cached status stays IDLE even though the worker already
    transitioned to PROCESSING.

    This function does a live ``capture-pane`` to grab the visible screen
    (not the 8 KB rolling buffer, which is too small to reliably hold the
    footer) and calls ``provider.get_status()`` directly, catching the real
    state so the retry loop doesn't re-deliver into a working terminal.

    Only providers that set ``supports_direct_status_probe = True`` should
    be passed to this function; the ``get_status()`` contract for other
    providers (e.g. kiro_cli, antigravity_cli, cursor_cli) relies on
    dispatch bookkeeping and cannot distinguish IDLE from COMPLETED on a
    rendered capture-pane snapshot.
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return False
        session_name = metadata.get("tmux_session")
        window_name = metadata.get("tmux_window")
        if not session_name or not window_name:
            return False
        output = get_backend().get_history(session_name, window_name, tail_lines=200)
        status = provider.get_status(output)
    except Exception:
        logger.debug(
            "Direct status probe for %s failed (falling through to cached path)",
            terminal_id,
            exc_info=True,
        )
        return False
    return status in _DEFERRED_STARTED_STATUSES


def _message_visible_in_box(terminal_id: str, message: str) -> bool:
    """Return whether the provider parser sees exactly the expected task draft."""
    expected = _normalized_composer_text(message)
    if len(expected) < 8:
        return False
    metadata = get_terminal_metadata(terminal_id)
    provider = provider_manager.get_provider(terminal_id)
    if not metadata or provider is None:
        return False
    try:
        captured = get_backend().get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            tail_lines=PYTE_SCREEN_ROWS,
            strip_escapes=not bool(provider.composer_parse_accepts_escapes),
        )
        draft = provider.read_composer_draft(captured.splitlines())
    except Exception:
        return False
    return isinstance(draft, str) and _normalized_composer_text(draft) == expected


async def _confirm_worker_started_or_resubmit(
    terminal_id: str,
    message: str,
    registry: PluginRegistry | None,
    sender_id: str | None,
    orchestration_type: OrchestrationType | None,
    *,
    provider: Any = None,
    generation: str | None = None,
    park_warm: bool = False,
) -> bool:
    """Confirm deferred input started, retrying only through guarded send seams."""

    async def run_blocking(operation: str, function, *args, **kwargs):
        if generation is None:
            return await asyncio.to_thread(function, *args, **kwargs)
        result, _grant = await _tracked_blocking(
            terminal_id,
            generation,
            "abandonable",
            operation,
            function,
            *args,
            **kwargs,
        )
        return result

    if await wait_until_status(
        terminal_id,
        _DEFERRED_STARTED_STATUSES,
        timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
        polling_interval=0.5,
    ):
        return True

    # F139 D10: a process-less fixture provider has no child and no pane
    # composer, so status stays IDLE and _message_visible_in_box can never
    # confirm. The provider reports a stable IDLE immediately after init and
    # its send path already recorded the sandbox receipt — treat as started.
    _fixture_cap = getattr(provider, "_fixture_capability", None)
    if _fixture_cap is not None and getattr(_fixture_cap, "variant", None) == "process-less":
        return True

    for attempt in range(1, _DEFERRED_SUBMIT_MAX_RESUBMITS + 1):
        current_status = status_monitor.get_status(terminal_id)
        if current_status == TerminalStatus.WAITING_USER_ANSWER:
            raise TerminalInputBlockedError(
                f"Terminal {terminal_id} is waiting for a user answer during deferred submit"
            )
        if current_status == TerminalStatus.ERROR:
            return False

        # The cached status can lag behind the visible provider screen. Providers
        # must opt in because only some get_status implementations are safe on a
        # direct capture. This is a fast path inside the existing WPM4a guarded
        # confirmation loop; Codex inherits the default False capability.
        if provider is not None and getattr(provider, "supports_direct_status_probe", False):
            if await run_blocking(
                "deferred_submit_direct_status",
                _worker_is_started_direct,
                terminal_id,
                provider,
            ):
                return True

        task_is_stable = await run_blocking(
            "deferred_submit_probe", _message_visible_in_box, terminal_id, message
        )
        if task_is_stable:
            await asyncio.sleep(_DEFERRED_SUBMIT_STABILITY_DELAY)
            task_is_stable = await run_blocking(
                "deferred_submit_probe",
                _message_visible_in_box,
                terminal_id,
                message,
            )
        if task_is_stable:
            if status_monitor.get_status(terminal_id) == TerminalStatus.WAITING_USER_ANSWER:
                raise TerminalInputBlockedError(
                    f"Terminal {terminal_id} entered a user dialog during deferred submit"
                )
            logger.warning(
                "Deferred assign to %s is present but unsubmitted; retrying Enter (attempt %d)",
                terminal_id,
                attempt,
            )
            await run_blocking("deferred_submit_enter", send_special_key, terminal_id, "Enter")
        else:
            logger.warning(
                "Deferred assign to %s was not accepted; re-delivering through guards "
                "(attempt %d)",
                terminal_id,
                attempt,
            )
            await run_blocking(
                "deferred_submit_send",
                send_input,
                terminal_id,
                message,
                registry=registry,
                sender_id=sender_id,
                orchestration_type=orchestration_type,
                defer_on_dialog=True,
                expect_callback=False,
            )
        if await wait_until_status(
            terminal_id,
            _DEFERRED_STARTED_STATUSES,
            timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
            polling_interval=0.5,
        ):
            return True
    return False


# --- F124 S5/S6: launch health probe -----------------------------------------


async def _provider_child_alive(terminal_id: str, provider) -> bool | None:
    """F124 S5: prove provider process tree is not an empty/dead pane.

    Returns:
        True  — alive (descendants found or exec-replaced or no-process provider)
        False — confirmed dead (empty shell, vanished pane)
        None  — inconclusive (missing procfs/baseline) → degrade to F110 watchdog
    """
    from cli_agent_orchestrator.services.fork_context_service import (
        _PROC_ROOT,
        _descendants,
        _procfs_available,
    )

    # Step 1: process-less provider short-circuits without procfs access
    if not getattr(provider, "has_process_child", True):
        return True

    # Step 1b (F139 r5 D15): provider confirmed fixture child death during
    # initialize — deterministic False without procfs/tmux race.
    if provider.launch_health_failure_confirmed:
        return False

    # Step 2: procfs availability
    if not _procfs_available():
        logger.warning(
            "f124_procfs_unavailable terminal=%s — degrading to F110 watchdog",
            terminal_id,
        )
        return None

    # Step 3: resolve pane PID
    metadata = get_terminal_metadata(terminal_id)
    if metadata is None:
        return False

    from cli_agent_orchestrator.services.fork_context_service import pane_pid as _pane_pid

    try:
        pid = _pane_pid(metadata["tmux_session"], metadata["tmux_window"])
    except Exception:
        return False

    # Verify the pane PID's /proc entry exists
    if not (_PROC_ROOT / str(pid) / "stat").exists():
        return False

    # Step 4: full descendant tree
    descendants = _descendants(pid)
    if len(descendants) > 1:
        return True

    # Step 5: exec-replacement check (pane command != shell baseline)
    baseline = getattr(provider, "shell_baseline", None) or getattr(
        provider, "_shell_baseline", None
    )
    try:
        from cli_agent_orchestrator.backends.registry import get_backend as _get_backend

        current_command = _get_backend().get_pane_current_command(
            metadata["tmux_session"], metadata["tmux_window"]
        )
    except Exception:
        current_command = None

    if not baseline or not current_command:
        # Cannot compare — inconclusive
        return None

    if current_command != baseline:
        # Shell was exec-replaced by the provider binary
        return True

    # Step 6: current command equals baseline → confirmed empty shell
    return False


async def _confirm_launch_health(terminal_id: str, provider) -> None:
    """F124 S6: confirm provider is alive after initialize(); raise on confirmed death."""
    import asyncio as _asyncio

    grace = max(0.0, float(getattr(provider, "launch_health_grace_s", 0.0)))
    if grace:
        await _asyncio.sleep(grace)

    alive = await _provider_child_alive(terminal_id, provider)
    if alive is False:
        raise ProviderLaunchFailed(
            f"provider process tree is empty/dead for terminal {terminal_id}"
        )


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
    park_warm: bool = False,
    f138_incarnation_id: str | None = None,
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
        # D21: Exposure boundary = pane+token already bound by the time _run starts.
        # If we have a pinned incarnation_id, the process-bearing token is in the
        # live pane — ANY exception from here on must enter force settlement.
        _f138_exposure_crossed = f138_incarnation_id is not None
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
            # F124 S6: confirm provider process is alive before proceeding
            await _confirm_launch_health(terminal_id, provider_instance)
            # F138 D21: strict activation with pinned incarnation ID
            if f138_incarnation_id is not None:
                from cli_agent_orchestrator.clients.database import (
                    ActivationResult,
                    f138_strict_activate,
                )

                _act_result = f138_strict_activate(f138_incarnation_id)
                if _act_result.outcome in ("activated", "already_active"):
                    pass  # expected — continue
                elif _act_result.outcome == "needs_settlement":
                    raise _DeferredInitFailure("incarnation_needs_settlement")
                else:
                    # "missing" with non-null pinned ID = non-durable, not process-less
                    raise _DeferredInitFailure("incarnation_activation_missing")
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
            send_kwargs = {
                "registry": registry,
                "sender_id": snapshot.get("caller_id"),
                "orchestration_type": orchestration_type,
            }
            if park_warm:
                send_kwargs["expect_callback"] = False
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
                _DEFERRED_DELIVERY_MAX_RETRIES = 3
                _DEFERRED_DELIVERY_RETRY_DELAY = 2.0
                for _attempt in range(_DEFERRED_DELIVERY_MAX_RETRIES):
                    try:
                        await _tracked_blocking(
                            terminal_id,
                            generation,
                            "abandonable",
                            "send_input",
                            send_input,
                            terminal_id,
                            prepared_message,
                            **send_kwargs,
                        )
                        break
                    except DeliveryDeferredError:
                        if _attempt == _DEFERRED_DELIVERY_MAX_RETRIES - 1:
                            raise  # falls to outer handler → teardown
                        logger.warning(
                            "deferred_init_delivery_deferred terminal=%s attempt=%d/%d",
                            terminal_id,
                            _attempt + 1,
                            _DEFERRED_DELIVERY_MAX_RETRIES,
                        )
                        await asyncio.sleep(_DEFERRED_DELIVERY_RETRY_DELAY)
                started = await _confirm_worker_started_or_resubmit(
                    terminal_id,
                    prepared_message,
                    registry,
                    snapshot.get("caller_id"),
                    orchestration_type,
                    provider=provider_instance,
                    generation=generation,
                    park_warm=park_warm,
                )
                if not started:
                    logger.error(
                        "Deferred init for %s never started after guarded resubmits; "
                        "notifying caller and tearing down",
                        terminal_id,
                    )
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
            # Log the cancellation (making this path non-silent — the original
            # F110 seam). Actual settlement is deferred to the periodic watchdog
            # (§2) rather than inline, because the CancelledError may arrive
            # while the dispatcher thread still holds the DB slot from the
            # in-flight operation that was interrupted — inline settlement via
            # _tracked_blocking would deadlock.
            logger.warning(
                "Deferred init for terminal %s cancelled before completion; "
                "watchdog will settle if row remains init_pending.",
                terminal_id,
                exc_info=True,
            )
            raise
        except Exception as e:
            # exc_info=True preserves the traceback for debugging; {e!r} avoids
            # newline/control-character injection into logs and the inbox message
            # (the exception text can contain provider-supplied content).
            logger.error(
                "Deferred init for terminal %s failed: %r. " "exposure_crossed=%s",
                terminal_id,
                e,
                _f138_exposure_crossed,
                exc_info=True,
            )
            if not _f138_exposure_crossed or f138_incarnation_id is None:
                # Pre-exposure or process-less: ordinary rollback path
                await _claim_and_settle_deferred_failure(
                    terminal_id,
                    generation,
                    snapshot,
                    _failure_code(e),
                    registry,
                    uuid_lease_token,
                    reason=repr(e)[:200],
                )
            else:
                # D23: Post-exposure — force reconcile, then check durability
                from cli_agent_orchestrator.clients.database import (
                    f138_emit_attention_message,
                    f138_force_reconcile_incarnation,
                    set_terminal_recovery_state,
                )

                try:
                    fr_result = f138_force_reconcile_incarnation(
                        f138_incarnation_id, source="deferred_post_exposure"
                    )
                except Exception:
                    fr_result = None
                    logger.exception(
                        "f138_post_exposure_force_reconcile_failed terminal=%s",
                        terminal_id,
                    )

                # Durable = force created a job OR proved reconciliation complete
                durable = fr_result is not None and fr_result.outcome in (
                    "created",
                    "job_already_exists",
                    "reconciled_proven",
                )

                if durable:
                    # Durable: teardown allowed — bare delete (no abandon)
                    logger.info(
                        "f138_post_exposure_durable terminal=%s outcome=%s — teardown",
                        terminal_id,
                        fr_result.outcome,
                    )
                    await _claim_and_settle_deferred_failure(
                        terminal_id,
                        generation,
                        snapshot,
                        _failure_code(e),
                        registry,
                        uuid_lease_token,
                        reason=repr(e)[:200],
                    )
                else:
                    # Non-durable: fail closed — retain pane/row/FIFO/provider.
                    # Attempt quarantine marker; if THAT fails, still retain
                    # physical resources and emit best-effort attention.
                    logger.error(
                        "f138_post_exposure_non_durable terminal=%s fr_outcome=%s — "
                        "retaining as rollback_kill_uncertain",
                        terminal_id,
                        fr_result.outcome if fr_result else "db_error",
                    )
                    quarantine_persisted = False
                    try:
                        quarantine_persisted = set_terminal_recovery_state(
                            terminal_id,
                            "rollback_kill_uncertain",
                            error=f"post_exposure_non_durable: {repr(e)[:150]}",
                        )
                    except Exception:
                        logger.exception("f138_quarantine_commit_failed terminal=%s", terminal_id)

                    # Shared DB-only one-shot attention (works even without caller_id)
                    detail = f"quarantine={'persisted' if quarantine_persisted else 'FAILED'}"
                    f138_emit_attention_message(
                        terminal_id,
                        f"[F138] Terminal {terminal_id} post-exposure failure "
                        f"with non-durable reconcile ({detail}). "
                        f"Physical resources retained. Manual review needed.",
                    )
                    # Return without teardown — finally block releases leases only
                    return
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
        try:
            _settle_deferred_failure_sync(terminal_id, registry)
        except Exception:
            logger.exception("Deferred init no-loop settlement failed terminal=%s", terminal_id)
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

        # §1a: if the task did not complete normally and the row is still
        # init_pending, log a warning. The periodic watchdog (§2) will settle
        # the row on its next sweep. We avoid inline settlement here because
        # the done-callback fires after task teardown and the dispatcher may
        # still hold the old task's thread slot — inline re-entry through
        # _tracked_blocking would deadlock the single-threaded dispatcher.
        if not completed.cancelled() and completed.exception() is None:
            # Normal completion — init committed ready (or the task body already
            # settled via _claim_and_settle_deferred_failure). Nothing to do.
            return
        if loop.is_closed():
            return
        meta = get_terminal_metadata(terminal_id)
        if meta is not None and meta.get("init_state") == "init_pending":
            logger.warning(
                "Deferred init done-callback detected unsettled init_pending for "
                "terminal %s (cancelled=%s); watchdog will settle on next sweep.",
                terminal_id,
                completed.cancelled(),
            )

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

        result = {
            "id": metadata["id"],
            "name": metadata["tmux_window"],
            "provider": metadata["provider"],
            "session_name": metadata["tmux_session"],
            "agent_profile": metadata["agent_profile"],
            "caller_id": metadata.get("caller_id"),
            "caller_mailbox_id": metadata.get("caller_mailbox_id"),
            "allowed_tools": metadata.get("allowed_tools"),
            "provider_session_id": metadata.get("provider_session_id"),
            "engine": metadata.get("engine"),
            "group": metadata.get("group"),
            "metadata": metadata.get("metadata"),
            "status": status,
            "input_gen": input_gen,
            "status_gen": 0 if status_gen is None else status_gen,
            "last_active": metadata["last_active"],
        }

        # F121: pull-based branch integrity verification for worktree-backed terminals
        _wt_info = metadata.get("worktree_info")
        if _wt_info is not None:
            from dataclasses import asdict as _asdict

            try:
                live_cwd = get_backend().get_pane_working_directory(
                    metadata["tmux_session"], metadata["tmux_window"]
                )
            except Exception:
                live_cwd = None
            if live_cwd:
                from cli_agent_orchestrator.services.worktree_service import (
                    verify_worktree_integrity,
                )

                integrity = verify_worktree_integrity(live_cwd, _wt_info)
                result["branch_integrity"] = _asdict(integrity)
            else:
                from cli_agent_orchestrator.services.worktree_service import (
                    WorktreeIntegrityResult,
                )

                result["branch_integrity"] = _asdict(
                    WorktreeIntegrityResult(
                        ok=False,
                        expected_branch=_wt_info["expected_branch"],
                        expected_worktree_path=_wt_info["worktree_path"],
                        error="cwd_unavailable",
                    )
                )

        return result

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


def update_group(terminal_id: str, group: Optional[List[str]]) -> bool:
    """Replace a terminal's group array.

    Used by consumers whose own grouping can change after a terminal already
    exists (e.g. harness-control folder/project reassignment) so ``group``
    doesn't go stale (#432). ``None``/``[]`` opts the terminal back out of
    discovery.

    Returns:
        False if the terminal does not exist, True otherwise.
    """
    return update_terminal_group(terminal_id, group)


def update_metadata(terminal_id: str, metadata: Optional[Dict[str, Any]]) -> bool:
    """Replace a terminal's free-form metadata dict.

    Whole-dict replace, not a merge: concurrent calls are last-write-wins
    (tedswinyar, PR #433 review). Acceptable for this field -- callers should
    re-send the full intended dict each time rather than assuming a partial
    update accumulates on top of a prior one.

    Returns:
        False if the terminal does not exist, True otherwise.
    """
    return update_terminal_metadata(terminal_id, metadata)


def list_siblings(
    caller_id: str, depth: Optional[int] = None, cross_session: bool = False
) -> List[Dict[str, Any]]:
    """Resolve ``caller_id``'s own group and return matching sibling terminals.

    Depth is clamped server-side to ``[1, len(caller_group)]`` (#432): it can
    never be widened past the caller's own group length, and an explicit 0 is
    rejected by the API layer's query-param validation before this is ever
    called (never silently reinterpreted as an unscoped, all-terminals
    query). ``depth=None`` defaults to the caller's full own group length —
    the widest scope the caller is allowed to see.

    A caller with no group set finds no siblings (participates in no
    discovery, per #432) rather than erroring.

    Session-scoped by default (issue #432 design discussion): results are
    additionally filtered to the caller's own ``tmux_session`` unless
    ``cross_session=True`` is explicitly passed — see
    ``list_siblings_by_group_prefix``'s own docstring for the full rationale.

    Returns:
        List of ``{id, group, metadata, status}`` dicts for every OTHER
        terminal whose group shares the resolved prefix. ``status`` is a
        live, point-in-time snapshot (tedswinyar, PR #433 review): a handoff
        terminal that has COMPLETED can still delete itself between this
        call returning and a caller's follow-up ``send_message`` to it, so a
        discovered sibling is never a guarantee it's still reachable --
        ``status`` lets a caller skip an obviously-finished sibling
        proactively, but callers should still expect sends to occasionally
        fail against a sibling that disappeared in that window.
    """
    caller_metadata = get_terminal_metadata(caller_id)
    caller_group = caller_metadata.get("group") if caller_metadata else None
    if not caller_group:
        return []
    caller_session = caller_metadata.get("tmux_session") if caller_metadata else None
    max_depth = len(caller_group)
    effective_depth = max_depth if depth is None else depth
    effective_depth = max(1, min(effective_depth, max_depth))
    prefix = caller_group[:effective_depth]
    siblings = list_siblings_by_group_prefix(
        caller_id, prefix, caller_session=caller_session, cross_session=cross_session
    )
    for sibling in siblings:
        sibling["status"] = status_monitor.get_status(sibling["id"]).value
    return siblings


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


def _is_coroutine_function(value: Any) -> bool:
    """Return whether ``value`` is an async coroutine function.

    ``asyncio.iscoroutinefunction`` is deprecated in Python 3.16; use the
    inspect equivalent, which handles both ``def async`` and partial-wrapped
    coroutines identically for the fixture send-override check.
    """
    import inspect

    return inspect.iscoroutinefunction(value)


def _fixture_send_input_override(provider, message: str) -> bool:
    """F139 D9/D10/D11: dispatch a sandbox fixture provider's send_input override.

    Returns True when the provider is a sandbox fixture provider whose
    manifest-pinned variant requires fixture-specific send behavior
    (process-less receipt, post-send-death receipt+raise, procfs-unavailable
    block). The override is an async coroutine; it runs on a fresh event loop
    in this thread (the module send paths run in a dispatcher/executor thread
    with no running loop). Production providers never define a fixture
    capability and are untouched.
    """
    if provider is None:
        return False
    capability = getattr(provider, "_fixture_capability", None)
    if capability is None:
        return False
    variant = getattr(capability, "variant", None)
    if variant in (None, "healthy", "empty-shell"):
        # healthy uses the ordinary paste path; empty-shell never reaches
        # delivery (F124 launch-health settles it first).
        return False
    override = getattr(provider, "send_input", None)
    if not callable(override) or not _is_coroutine_function(override):
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(override(message))
    else:
        # Running inside an event-loop thread — run the override on a fresh
        # daemon thread so we never block the server loop.
        import threading as _threading

        exc_holder: list[BaseException] = []

        def _run() -> None:
            try:
                asyncio.run(override(message))
            except BaseException as exc:  # noqa: BLE001 - re-raised in caller
                exc_holder.append(exc)

        thread = _threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join()
        if exc_holder:
            raise exc_holder[0]
    return True


def send_input(
    terminal_id: str,
    message: str,
    registry: PluginRegistry | None = None,
    sender_id: str | None = None,
    orchestration_type: OrchestrationType | None = None,
    defer_on_dialog: bool = False,
    *,
    expect_callback: bool = True,
    _lifecycle_internal: bool = False,
) -> bool:
    """Send input to terminal via tmux paste buffer.

    Uses bracketed paste mode (-p) to bypass TUI hotkey handling. The number
    of Enter keys sent after pasting is determined by the provider's
    ``paste_enter_count`` property (e.g., some TUIs need 2 Enters because
    bracketed paste triggers multi-line mode).

    Args:
        _lifecycle_internal: When True, bypass the pre-send status check.
            Reserved for internal lifecycle operations (exit_terminal_cli)
            that must send input while the terminal is in a recovery state
            that would otherwise block public callers.  Never expose this
            to API callers or orchestration endpoints.
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
        # F139 D9/D10/D11: fixture providers with a manifest-pinned non-healthy
        # variant intercept the entire send path (receipt / block / raise).
        if _fixture_send_input_override(provider, message):
            return True
        if provider and not _lifecycle_internal:
            current_status = status_monitor.get_status(terminal_id)
            # Pre-paste guard order: dead-provider ERROR, interactive WAITING,
            # then the existing draft guard immediately before injection.
            # Inbox callers complete their InjectSafetyResult gate before this seam.
            if current_status == TerminalStatus.ERROR:
                raise TerminalInputBlockedError(
                    f"Terminal {terminal_id} provider is in ERROR state; refusing input"
                )
            if (
                provider.blocks_orchestrated_input_while_waiting_user_answer is True
                and orchestration_value
                in {OrchestrationType.ASSIGN.value, OrchestrationType.HANDOFF.value}
                and current_status == TerminalStatus.WAITING_USER_ANSWER
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

        status_monitor.bind_dispatch_provider(terminal_id, provider)
        dispatch_txn: DispatchTxn = status_monitor.begin_dispatch(terminal_id)
        try:
            backend.send_keys(
                metadata["tmux_session"],
                metadata["tmux_window"],
                message,
                enter_count=enter_count,
                force_bracketed_paste=True,
                submit_delay=provider.paste_submit_delay if provider else 0.3,
            )
        except BaseException:
            status_monitor.abort_dispatch(dispatch_txn)
            raise
        else:
            status_monitor.commit_dispatch(dispatch_txn)
            if not isinstance(status_monitor, StatusMonitor) and provider is not None:
                provider.mark_input_received()
        if preserved_draft is not None:
            preserved_draft.restore(backend)

        # Notify the provider that external input was received.
        # This allows providers to adjust status
        # detection — specifically to stop reporting IDLE for the post-init
        # state and resume normal COMPLETED detection after a real task.
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
            # Telemetry (opt-in; no-ops without the [otel] extra or when the SDK
            # is disabled): record a GenAI ``execute_tool`` span for the dispatch,
            # count it, and propagate the active trace context into the plugin
            # event so downstream consumers can continue the trace.
            from cli_agent_orchestrator.telemetry import (
                execute_tool_span,
                inject_traceparent,
                record_orchestration_dispatch,
            )

            with execute_tool_span(
                f"send_message:{orchestration_value}",
                conversation_id=metadata["tmux_session"],
            ):
                record_orchestration_dispatch(orchestration_value)
                dispatch_plugin_event(
                    registry,
                    "post_send_message",
                    PostSendMessageEvent(
                        session_id=metadata["tmux_session"],
                        sender=sender_id,
                        receiver=terminal_id,
                        message=original_message,
                        orchestration_type=orchestration_type,
                        traceparent=inject_traceparent(),
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
    if status_monitor.get_status(terminal_id) == TerminalStatus.WAITING_USER_ANSWER:
        raise TerminalInputBlockedError(
            f"Terminal {terminal_id} is waiting for a user answer. "
            "Use answer_user_prompt before sending inbox input."
        )
    if getattr(backend, "supports_identity_readback", False) is not True:
        native_identity = backend.read_native_identity(
            terminal_id,
            metadata["tmux_session"],
            metadata["tmux_window"],
            metadata.get("provider", "unknown"),
        )
        native_verdict = getattr(native_identity, "verdict", None)
        if native_verdict == "mismatch":
            from cli_agent_orchestrator.services.pane_identity_service import (
                PaneIdentityMismatchError,
            )

            raise PaneIdentityMismatchError("native_identity_mismatch")
        if native_verdict == "unavailable":
            raise DeliveryDeferredError("identity_unverified")
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
    # F139 D9/D10/D11: fixture providers intercept the send path.
    if _fixture_send_input_override(provider, message):
        return None
    enter_count = provider.paste_enter_count if provider else 1
    prepared_stash = None
    if isinstance(getattr(provider, "composer_stash_keys", None), list):
        prepared_stash = prepare_native_stash_before_send(
            terminal_id,
            provider,
            defer_on_dialog=defer_on_dialog,
        )
    status_monitor.notify_input_sent(terminal_id)
    status_monitor.clear_rolling_buffer(terminal_id)
    if prepared_stash is not None:
        if apply_prepared_native_stash(prepared_stash):
            enter_count = 1
        preserved = None
    else:
        preserved = preserve_draft_before_send(terminal_id, metadata, provider)
    with _memory_injected_lock:
        _memory_injected_terminals.add(terminal_id)
    status_monitor.bind_dispatch_provider(terminal_id, provider)
    dispatch_txn: DispatchTxn = status_monitor.begin_dispatch(terminal_id)
    try:
        backend.send_keys(
            metadata["tmux_session"],
            metadata["tmux_window"],
            message,
            enter_count=enter_count,
            force_bracketed_paste=True,
            submit_delay=provider.paste_submit_delay if provider else 0.3,
        )
    except BaseException:
        status_monitor.abort_dispatch(dispatch_txn)
        raise
    else:
        status_monitor.commit_dispatch(dispatch_txn)
        if not isinstance(status_monitor, StatusMonitor) and provider is not None:
            provider.mark_input_received()
    injection_observation = status_monitor.mark_injection_completed(terminal_id)
    if on_submitted is not None:
        on_submitted(injection_observation)
    if preserved is not None:
        preserved.restore(backend)
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
        send_input(terminal_id, exit_command, _lifecycle_internal=True)

    # Layer B (F115): suppress auto-responder scans on this terminal after exit.
    # Exit residue (final report table + exit chrome) should not trigger unknown
    # dialog alerts. Cleared on re-register (rebind) or clear_terminal (delete).
    from cli_agent_orchestrator.services.auto_responder import auto_responder

    auto_responder.mark_exit_suppress(terminal_id)


def get_output(terminal_id: str, mode: OutputMode = OutputMode.FULL) -> str:
    """Get terminal output.

    ``FULL`` mode returns the StatusMonitor rolling buffer (the streamed output
    accumulated from the FIFO pipeline), which is bounded to the most recent
    ``state_buffer_max`` bytes (server setting, see settings_service.py; 32KB
    default); it falls back to a tmux history capture only when that buffer
    is empty. This is a deliberate trade-off in the
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


def read_output_range(terminal_id: str, offset: int, length: int) -> str:
    """Read a byte range from a terminal's append-only on-disk log (U5 / #504).

    This is a SEPARATE read path from ``get_output``: that function returns the
    bounded rolling buffer / tmux tail, whereas this reads an exact byte window
    from ``TERMINAL_LOG_DIR / f"{terminal_id}.log"`` — the append-only,
    monotonic file LogWriter maintains (BR-1). Playback (FR-4.3 / FR-7.3) uses
    the ``terminal_offset_start`` / ``terminal_offset_len`` an event carries to
    fetch exactly the output produced around that event, without copying the
    log into the journal (BR-3).

    Args:
        terminal_id: The terminal whose log to read. Validated against the
            workflow name/id charset before it is joined into the log path, so
            a value containing ``/`` / ``..`` / a NUL can never escape
            ``TERMINAL_LOG_DIR`` (path-traversal defense; reuses
            ``_validate_key_part``).
        offset: Byte offset to seek to. Must be ``>= 0``. An offset at or beyond
            EOF is not an error — the read simply returns the available tail
            (empty string when nothing follows the offset) so playback degrades
            gracefully (BR-4).
        length: Maximum number of bytes to read. Clamped to
            ``TERMINAL_RANGE_MAX_LENGTH`` (BR-2) to bound the read.

    Returns:
        The decoded slice, ``bytes.decode("utf-8", errors="replace")`` so a
        range that starts or ends mid-multibyte-sequence never raises (BR-5,
        matching LogWriter's write encoding). Returns ``""`` for a valid
        terminal whose log does not exist yet (nothing has been logged) — a
        missing log is NOT a playback-breaking error (BR-4).

    Raises:
        ValueError: ``terminal_id`` fails id validation, or ``offset`` is
            negative. Translated to a 400 at the request boundary.
        OSError: A genuine file I/O failure (e.g. a permission error, or the
            path exists but is unreadable). Surfaced to the caller, NOT
            swallowed into an empty string — "nothing logged yet" (return "")
            and "the read failed" (raise) are deliberately distinct outcomes
            (BR-4 / construction error-handling guardrail).
    """
    # Path-traversal defense: reject any id that is not a plain key BEFORE it is
    # joined into the log path. Reuses the workflow key/id validator so the
    # charset rule is defined once (rejects "/", "..", ".", NUL, whitespace).
    _validate_key_part(terminal_id, "terminal_id")

    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")

    # Clamp the read window (BR-2). A non-positive length reads nothing rather
    # than raising — the route enforces length >= 1, so this is defense in depth.
    capped_length = max(0, min(length, TERMINAL_RANGE_MAX_LENGTH))

    log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"

    try:
        with open(log_path, "rb") as f:
            f.seek(offset)  # seeking past EOF is legal; the read below yields b""
            data = f.read(capped_length)
    except FileNotFoundError:
        # Valid terminal that has not logged anything yet (or whose log has been
        # cleaned up): an empty range, never an error (BR-4).
        logger.debug(
            "read_output_range: no log file for terminal %s (offset=%d, length=%d) — "
            "returning empty range",
            terminal_id,
            offset,
            capped_length,
        )
        return ""
    except OSError as e:
        # A genuine I/O failure (permission, etc.) is NOT the same as "nothing
        # logged" — surface it rather than masking a real fault as empty output.
        logger.error(
            "read_output_range: I/O error reading log for terminal %s "
            "(offset=%d, length=%d): %s",
            terminal_id,
            offset,
            capped_length,
            e,
        )
        raise

    return data.decode("utf-8", errors="replace")


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


def _cascade_plan(
    terminals: list[dict[str, Any]],
    root_id: str,
    *,
    orphan: bool,
    force: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """Return children-before-parents reap order and named survivors."""
    from cli_agent_orchestrator.services.terminal_guard_service import classify_deletion

    children: dict[str, list[str]] = {}
    by_id = {row["id"]: row for row in terminals}
    for row in terminals:
        parent = row.get("caller_id")
        if parent:
            children.setdefault(parent, []).append(row["id"])
    for child_ids in children.values():
        child_ids.sort()

    reaped: list[str] = []
    skipped: list[dict[str, str]] = []

    def skip_subtree(node_id: str, reason: str) -> None:
        skipped.append({"id": node_id, "reason": reason})
        for child_id in children.get(node_id, []):
            skip_subtree(child_id, f"ancestor_skipped:{node_id}")

    def visit(node_id: str, depth: int) -> None:
        if depth >= 32:
            skip_subtree(node_id, "depth_cap")
            return
        classification = classify_deletion(node_id, force=force)
        if not classification.allowed:
            skip_subtree(node_id, classification.reason or "protected")
            return
        for child_id in children.get(node_id, []):
            if child_id in by_id:
                visit(child_id, depth + 1)
        reaped.append(node_id)

    if orphan:
        for child_id in children.get(root_id, []):
            skip_subtree(child_id, "orphan_requested")
    else:
        for child_id in children.get(root_id, []):
            visit(child_id, 1)
    reaped.append(root_id)
    return reaped, skipped


def _recovery_state_is_active(state: RecoveryState) -> bool:
    """Return True if *state* represents an in-progress recovery.

    Uses assert_never so mypy flags unhandled variants when the Literal expands.
    """
    if state == "rebind_starting":
        return True
    if state == "rebind_exiting":
        return True
    if state == "fallback_starting":
        return True
    if state == "fallback_ready":
        return True
    if state == "rebound":
        return False
    if state == "rebind_failed":
        return False
    assert_never(state)


_ACTIVE_RECOVERY_STATES: frozenset[str] = frozenset(
    s for s in ("rebind_starting", "rebind_exiting", "fallback_starting", "fallback_ready")
)


def _owner_root_is_dead(
    terminals: list[dict[str, Any]],
    terminal_id: str,
) -> bool:
    """Return True if the root of terminal_id's caller chain has a dead window.

    Uses window_liveness (tmux_backend) — the same primitive that
    fleet_service.py uses for its parent_dead/orphan projection.
    Fail-closed: returns False on any error or ambiguity.
    """
    by_id = {row["id"]: row for row in terminals}
    current = terminal_id
    seen: set[str] = set()
    while current in by_id:
        if current in seen:
            return False  # cycle — fail closed
        seen.add(current)
        parent = by_id[current].get("caller_id")
        if not parent or parent not in by_id:
            break  # current is the root
        current = parent

    root = by_id.get(current)
    if not root:
        return False

    # A supervisor mid-restart has its window briefly absent but is NOT dead.
    # If the root row carries an active recovery/respawn state, treat as alive.
    recovery = root.get("recovery_state")
    if recovery is not None and recovery in _ACTIVE_RECOVERY_STATES:
        return False  # restart in progress — fail closed

    tmux_session = root.get("tmux_session")
    tmux_window = root.get("tmux_window")
    if not tmux_session or not tmux_window:
        return False
    try:
        liveness = get_backend().window_liveness(tmux_session, tmux_window)
        return liveness == "gone"
    except Exception:
        return False  # fail closed


def _caller_owns_target(terminals: list[dict[str, Any]], caller_id: str, target_id: str) -> bool:
    if caller_id == target_id:
        return False
    by_id = {row["id"]: row for row in terminals}
    current = by_id.get(target_id)
    seen: set[str] = set()
    while current and current.get("caller_id"):
        parent_id = current["caller_id"]
        if parent_id == caller_id:
            return True
        if parent_id in seen:
            return False
        seen.add(parent_id)
        current = by_id.get(parent_id)
    return False


def _surviving_ancestor(by_id: dict[str, dict[str, Any]], node_id: str, reap_set: set[str]) -> str:
    current = by_id.get(node_id, {}).get("caller_id")
    seen: set[str] = set()
    while current:
        if current in seen:
            return ""
        seen.add(current)
        row = by_id.get(current)
        if row is None:
            return ""
        if current not in reap_set and row.get("recovery_state") != "fallback_ready":
            return current
        current = row.get("caller_id")
    return ""


def delete_terminal(
    terminal_id: str,
    registry: PluginRegistry | None = None,
    *,
    force: bool = False,
    orphan: bool = False,
    caller_id: str | None = None,
) -> dict[str, Any]:
    """Cascade-delete a terminal's managed descendant tree."""
    from cli_agent_orchestrator.services.rebind_lease import (
        acquire_rebind_lease,
        release_rebind_lease,
    )
    from cli_agent_orchestrator.services.session_lifecycle_lease import (
        acquire_session_lifecycle_exclusive,
        release_session_lifecycle_lease,
    )
    from cli_agent_orchestrator.services.terminal_guard_service import (
        TerminalProtectionError,
        require_delete_allowed,
    )

    root = get_terminal_metadata(terminal_id)
    if root is None:
        raise ValueError(f"Terminal '{terminal_id}' not found")
    require_delete_allowed(terminal_id, force=force)
    session_name = root["tmux_session"]

    quiesce_deferred_session_sync(session_name)
    lifecycle_lease = acquire_session_lifecycle_exclusive(session_name)
    if lifecycle_lease is None:
        raise RuntimeError("resume_in_progress")
    try:
        terminals = list_terminals_by_session(session_name)
        if caller_id is not None and not _caller_owns_target(terminals, caller_id, terminal_id):
            # Bypass: allow force-delete when the target's root owner is dead
            if force and _owner_root_is_dead(terminals, terminal_id):
                logger.info("dead-owner bypass: force-deleting %s (root owner gone)", terminal_id)
            else:
                raise TerminalProtectionError("cascade_outside_caller_subtree")
        order, skipped = _cascade_plan(
            terminals,
            terminal_id,
            orphan=orphan,
            force=force,
        )
        by_id = {row["id"]: row for row in terminals}
        reap_set = set(order)
        reaped: list[dict[str, str]] = []
        for index, node_id in enumerate(order):
            token = acquire_rebind_lease(node_id)
            if token is None:
                raise RuntimeError("rebind_in_progress")
            try:
                observation = status_monitor.get_boundary_observation(node_id)
                busy = observation.status == TerminalStatus.PROCESSING
                target_id = _surviving_ancestor(by_id, node_id, reap_set)
                result = _delete_terminal_under_lease(
                    node_id,
                    token,
                    registry=registry,
                    require_confirmed_death=True,
                    quarantine_session_uuid=by_id.get(node_id, {}).get("provider_session_id"),
                    reparent_target_id=target_id,
                )
            finally:
                release_rebind_lease(token)
            if result.get("rollback_kill_uncertain"):
                return {
                    "reaped": reaped,
                    "skipped": skipped,
                    "uncertain": [{"id": node_id, "reason": "rollback_kill_uncertain"}],
                    "unattempted": order[index + 1 :],
                }
            disposition = "killed_while_busy" if busy else "reaped"
            reaped.append({"id": node_id, "status": disposition})
            parent_writer = getattr(get_backend(), "set_window_parent", None)
            if callable(parent_writer):
                for child in terminals:
                    if child.get("caller_id") == node_id and child["id"] not in reap_set:
                        parent_writer(session_name, child["tmux_window"], target_id or None)
        return {
            "reaped": reaped,
            "skipped": skipped,
            "uncertain": [],
            "unattempted": [],
        }
    finally:
        release_session_lifecycle_lease(lifecycle_lease)


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
    persona_retention_intent=None,
    reparent_target_id: str | None = None,
) -> Dict:
    """Delete terminal and kill its tmux window."""
    # Layer C (F115): early auto_responder.clear_terminal at start of delete.
    # Bumps generation + wipes episode state so any in-flight _check_unknown
    # that already read metadata will fail the incarnation fence on _push.
    # The late clear (~:4299 under delivery lock) is kept as idempotent belt.
    from cli_agent_orchestrator.services.auto_responder import auto_responder

    auto_responder.clear_terminal(terminal_id)

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
            status_monitor.unregister(terminal_id)
        except Exception as exc:
            logger.warning(f"Failed to clear state detector for {terminal_id}: {exc}")

    persona_retention_error = None
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
            # Read the pane's live working directory BEFORE kill_window below
            # destroys the pane. Single read, reused for two purposes: the
            # scrollback snapshot below, and issue #100 Phase 1's worktree
            # cleanup (recognizing a worktree-backed terminal from its live
            # cwd alone -- there is no separate CAO-side record of which
            # terminals are worktree-backed). Best-effort: a read failure
            # means the snapshot's working_directory field is None and no
            # worktree cleanup runs below.
            live_working_directory = None
            try:
                live_working_directory = get_backend().get_pane_working_directory(
                    metadata["tmux_session"], metadata["tmux_window"]
                )
            except Exception as e:
                logger.warning(f"Failed to read working directory for {terminal_id}: {e}")

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
                    "working_directory": live_working_directory,
                    "allowed_tools": metadata.get("allowed_tools"),
                    "caller_id": metadata.get("caller_id"),
                }

                # F121: worktree branch integrity check at teardown
                _teardown_worktree_info = metadata.get("worktree_info")
                if _teardown_worktree_info and live_working_directory:
                    from dataclasses import asdict as _asdict

                    from cli_agent_orchestrator.services.worktree_service import (
                        verify_worktree_integrity,
                    )

                    _integrity = verify_worktree_integrity(
                        live_working_directory, _teardown_worktree_info
                    )
                    snapshot["worktree_branch_integrity"] = _asdict(_integrity)
                    if not _integrity.ok:
                        logger.warning(
                            "F121 branch integrity ESCAPE detected at teardown for "
                            "terminal %s: expected_branch=%s, actual_branch=%s, "
                            "expected_worktree_path=%s, actual_toplevel=%s, "
                            "cwd_escaped=%s, branch_escaped=%s, error=%s",
                            terminal_id,
                            _integrity.expected_branch,
                            _integrity.actual_branch,
                            _integrity.expected_worktree_path,
                            _integrity.actual_toplevel,
                            _integrity.cwd_escaped,
                            _integrity.branch_escaped,
                            _integrity.error,
                        )

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

        if persona_retention_intent is not None:
            if metadata is None:
                persona_retention_error = "retained_persona_terminal_missing"
            else:
                try:
                    death = get_backend().window_liveness(
                        metadata["tmux_session"], metadata["tmux_window"]
                    )
                except Exception:
                    death = "error"
                if death != "gone":
                    persona_retention_error = "retained_persona_process_not_dead"
                else:
                    from cli_agent_orchestrator.utils.persona_context import (
                        retain_codex_persona_home,
                    )

                    persona_retention_error = retain_codex_persona_home(
                        terminal_id, persona_retention_intent
                    )

        # issue #100 Phase 1: if this terminal was worktree-backed (its live
        # cwd matched the CAO-managed worktree path shape), remove the
        # worktree + branch now that the process using it is gone.
        # `remove_worktree` is itself best-effort/never-raises, matching
        # every other step in this teardown.
        #
        # The parsed terminal_id MUST match the terminal actually being
        # deleted here, not just "some" CAO worktree path. Without this
        # guard: a worktree-backed terminal A (cwd
        # .../.cao/worktrees/A) can spawn a non-worktree terminal B with
        # working_directory explicitly set to A's cwd (handoff/assign
        # both accept an explicit working_directory, and "here" -- the
        # caller's own directory -- is a common choice). Deleting B --
        # including handoff's automatic success teardown -- would then
        # read B's pane cwd (== A's worktree path), parse terminal_id
        # "A" out of it, and force-remove A's still-running worktree.
        # Mismatched parses now fall through as a no-op leak (Phase 3
        # territory) instead of destroying another terminal's checkout.
        if metadata is not None:
            parsed = worktree_service.parse_worktree_path(live_working_directory)
            if parsed is not None:
                worktree_repo_root, worktree_terminal_id = parsed
                if worktree_terminal_id == terminal_id:
                    worktree_service.remove_worktree(worktree_repo_root, worktree_terminal_id)

        # Cleanup provider state and database record
        provider_manager.cleanup_provider(terminal_id)
        from cli_agent_orchestrator.utils.persona_context import cleanup_persona

        cleanup_persona(terminal_id)
        with _memory_injected_lock:
            _memory_injected_terminals.discard(terminal_id)
        from cli_agent_orchestrator.services.inbox_service import (
            clear_terminal_delivery_state,
            get_delivery_lock,
        )
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        delivery_lock = get_delivery_lock(terminal_id)
        delivery_lock.acquire()
        try:
            stalled_callback_watchdog.clear_terminal(terminal_id)
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
            # F138 D11/D24: Force-reconcile by exact terminal_id + persisted generation.
            # No fallback lookup. Missing row or force failure = fail closed (prevent delete).
            _f138_delete_authorized = True
            _term_gen = metadata.get("lifecycle_generation") if metadata else None
            if _term_gen is not None:
                from cli_agent_orchestrator.clients.database import (
                    f138_force_reconcile_incarnation,
                    f138_get_incarnation_by_terminal_generation,
                )

                _inc_row = f138_get_incarnation_by_terminal_generation(terminal_id, _term_gen)
                if _inc_row is not None:
                    try:
                        _fr = f138_force_reconcile_incarnation(
                            _inc_row["id"], source="delete_terminal"
                        )
                        if _fr.outcome in (
                            "non_durable_invariant",
                            "non_durable_missing",
                        ):
                            # Non-durable: fail closed — prevent deletion
                            _f138_delete_authorized = False
                            logger.error(
                                "f138_delete_non_durable terminal=%s outcome=%s — "
                                "preventing delete",
                                terminal_id,
                                _fr.outcome,
                            )
                    except Exception:
                        # DB error: fail closed — prevent deletion
                        _f138_delete_authorized = False
                        logger.error(
                            "f138_delete_force_db_error terminal=%s — preventing delete",
                            terminal_id,
                            exc_info=True,
                        )
                # _inc_row is None with a known generation: process-less provider,
                # no incarnation row exists — deletion is safe

            if not _f138_delete_authorized:
                from cli_agent_orchestrator.clients.database import (
                    f138_emit_attention_message,
                    set_terminal_recovery_state,
                )

                try:
                    set_terminal_recovery_state(
                        terminal_id,
                        "rollback_kill_uncertain",
                        error="delete_non_durable_force",
                    )
                except Exception:
                    pass
                f138_emit_attention_message(
                    terminal_id,
                    f"[F138] Delete of {terminal_id} blocked: force-reconcile "
                    f"non-durable. Terminal retained for manual review.",
                )
                # Fake a "not deleted" result to preserve the terminal
                deletion = {
                    "terminal_deleted": False,
                    "intent_deleted": False,
                }
            else:
                deletion_kwargs: dict[str, Any] = {
                    "preserve_warm_intent": preserve_warm_intent,
                }
                if reparent_target_id is not None:
                    deletion_kwargs["reparent_target_id"] = reparent_target_id
                deletion = delete_terminal_and_warm_intent(terminal_id, **deletion_kwargs)
        finally:
            delivery_lock.release()
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
            "persona_retention_error": persona_retention_error,
        }

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise
