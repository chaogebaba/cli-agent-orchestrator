"""Provider-level in-place rebind and provider-reauth fleet recovery."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    get_terminal_metadata,
    has_unsettled_delivery_attempt,
    list_terminals_by_session,
    set_terminal_recovery_state,
    settle_terminal_fallback,
    settle_terminal_rebound,
)
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.manager import get_provider_class, provider_manager
from cli_agent_orchestrator.services.fork_context_service import pane_launch_epoch, pane_pid
from cli_agent_orchestrator.services.inbox_service import get_delivery_lock
from cli_agent_orchestrator.services.rebind_lease import (
    acquire_rebind_lease,
    rebind_lease_held,
    release_rebind_lease,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.stalled_callback_watchdog import stalled_callback_watchdog
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.skills import build_skill_catalog

RESULT_RETRYABLE = {
    "rebound": False,
    "skipped_busy": True,
    "capture_failed": True,
    "unresumable": False,
    "resume_failed": True,
}
MID_STATES = {"rebind_starting", "rebind_exiting", "fallback_starting"}


def _persist_recovery(terminal_id: str, state: str | None, error: str | None = None) -> None:
    if not set_terminal_recovery_state(terminal_id, state, error):
        raise RuntimeError(f"recovery_state_persist_failed:{state}")


def _result(terminal_id: str, status: str, *, error_code: str | None = None,
            interrupt: bool = False, fallback=None, retryable: bool | None = None) -> dict:
    if retryable is None:
        retryable = RESULT_RETRYABLE[status]
    return {
        "terminal_id": terminal_id,
        "status": status,
        "retryable": retryable,
        "error_code": error_code,
        "fallback": fallback,
        "interrupted_turn": bool(interrupt),
        "requires_supervisor_reconciliation": bool(interrupt),
    }


class DeliveryGuard:
    """A dedicated thread owns both acquire and release of a delivery lock."""

    def __init__(self, terminal_id: str, loop: asyncio.AbstractEventLoop):
        self.terminal_id = terminal_id
        self.lock = get_delivery_lock(terminal_id)
        self.loop = loop
        self.cancel = threading.Event()
        self.release = threading.Event()
        self.acquired = loop.create_future()
        self.done = loop.create_future()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = False

    @property
    def active(self) -> bool:
        return (
            self.started
            and self.acquired.done()
            and bool(self.acquired.result())
            and not self.done.done()
        )

    def _set(self, future, value):
        if not future.done():
            future.set_result(value)

    def _run(self):
        owned = False
        try:
            while not self.cancel.is_set():
                if self.lock.acquire(timeout=0.1):
                    owned = True
                    break
            self.loop.call_soon_threadsafe(self._set, self.acquired, owned)
            if owned:
                self.release.wait()
        finally:
            if owned:
                self.lock.release()
            self.loop.call_soon_threadsafe(self._set, self.done, True)

    async def acquire(self):
        self.thread.start()
        self.started = True
        try:
            if not await asyncio.shield(self.acquired):
                raise asyncio.CancelledError
        except BaseException:
            self.cancel.set()
            self.release.set()
            await asyncio.shield(self.done)
            raise

    async def close(self):
        if not self.started:
            return
        self.release.set()
        await asyncio.shield(self.done)


async def _wait_for_shell_baseline(
    metadata: dict, baseline: str, provider=None, provider_pane_pid: int | None = None,
) -> str:
    stable_since = None
    provider_seen_live = False
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        current = get_backend().get_pane_current_command(
            metadata["tmux_session"], metadata["tmux_window"]
        )
        if current == baseline:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 2.0:
                return "exit_confirmed"
        else:
            stable_since = None
            if provider is not None and provider_pane_pid is not None:
                try:
                    provider_seen_live = (
                        provider.provider_process_started_at(provider_pane_pid) is not None
                    )
                except Exception:
                    pass
        await asyncio.sleep(0.1)
    return "exit_failed" if provider_seen_live else "exit_uncertain"


async def _wait_for_backend_proof(
    terminal_id: str,
    metadata: dict,
    candidate,
    before_output_gen: int,
    guard: DeliveryGuard | None = None,
) -> None:
    """Prove the staged provider reached the existing backend event pipeline."""
    backend = get_backend()
    deadline = time.monotonic() + 15.0
    if not backend.supports_event_inbox():
        from cli_agent_orchestrator.services.fifo_reader import fifo_manager

        if not fifo_manager.has_reader(terminal_id):
            raise RuntimeError("fifo_reader_missing")
        while time.monotonic() < deadline:
            if status_monitor.get_fifo_frame_gen(terminal_id) > before_output_gen:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("fifo_post_resume_frame_missing")

    from cli_agent_orchestrator.services.herdr_inbox_registry import get_herdr_inbox_service

    svc = get_herdr_inbox_service()
    if svc is None:
        raise RuntimeError("herdr_inbox_unavailable")
    pane = backend.get_pane_id(
        terminal_id, metadata["tmux_session"], metadata["tmux_window"]
    )
    before_event_gen = svc.get_native_event_gen(terminal_id, pane)
    if guard is None:
        svc.register_terminal(terminal_id, pane, False)
    else:
        svc._register_terminal_under_guard(terminal_id, pane, False, guard)
    if svc._terminal_to_pane.get(terminal_id) != pane or svc._pane_to_terminal.get(pane) != terminal_id:
        raise RuntimeError("herdr_mapping_proof_failed")
    while time.monotonic() < deadline:
        if svc.get_native_event_gen(terminal_id, pane) > before_event_gen:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("herdr_native_event_missing")


def _launch_context(metadata: dict) -> str | None:
    profile = load_agent_profile(metadata["agent_profile"])
    prompt = build_skill_catalog(profile.skills)
    if profile.sessionBrief:
        from cli_agent_orchestrator.services.session_manifest_service import (
            build_session_manifest, render_session_brief,
        )
        brief = render_session_brief(build_session_manifest(metadata["tmux_session"], metadata["id"]))
        prompt = f"{prompt}\n\n{brief}" if prompt else brief
    return prompt or None


async def _fallback(metadata: dict, session_uuid: str, source_lease, lifecycle_lease) -> dict:
    from cli_agent_orchestrator.services.terminal_service import create_terminal

    set_terminal_recovery_state(metadata["id"], "fallback_starting")
    context = ForkContext(
        mode="resume", session_uuid=session_uuid, base_name="reauth-fallback",
        provider=metadata["provider"], initial_preamble="",
    )
    replacement = await create_terminal(
        provider=metadata["provider"], agent_profile=metadata["agent_profile"],
        session_name=metadata["tmux_session"], new_session=False,
        working_directory=get_backend().get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]),
        allowed_tools=metadata.get("allowed_tools"), caller_id=metadata.get("caller_id"),
        fork_context=context,
        fallback_source_terminal_id=metadata["id"],
        fallback_source_lease_token=source_lease,
        session_lifecycle_lease_token=lifecycle_lease,
    )
    moved = settle_terminal_fallback(metadata["id"], replacement.id)
    return {"status": "respawned", "new_terminal_id": replacement.id,
            "moved_pending_count": moved}


async def rebind_terminal(
    terminal_id: str,
    *,
    interrupt: bool = False,
    acknowledge_ownership: bool = False,
    reason: str = "provider-reauth",
    content_options: dict | None = None,
) -> dict:
    initial_metadata = get_terminal_metadata(terminal_id)
    if not initial_metadata:
        return _result(terminal_id, "unresumable", error_code="terminal_missing", interrupt=interrupt)
    from cli_agent_orchestrator.services.session_lifecycle_lease import (
        acquire_session_lifecycle_shared, release_session_lifecycle_lease,
    )
    lifecycle_lease = acquire_session_lifecycle_shared(initial_metadata["tmux_session"])
    if lifecycle_lease is None:
        return _result(terminal_id, "skipped_busy", error_code="rebind_in_progress", interrupt=interrupt)
    lease = acquire_rebind_lease(terminal_id)
    if lease is None:
        release_session_lifecycle_lease(lifecycle_lease)
        return _result(terminal_id, "skipped_busy", error_code="rebind_in_progress", interrupt=interrupt)
    guard = DeliveryGuard(terminal_id, asyncio.get_running_loop())
    watchdog_snapshot = None
    old_provider = candidate = None
    exited = False
    session_uuid = None
    metadata = None
    previous_state = None
    guard_released = False
    prepared_recovery = None
    phase = "p2"
    try:
        await guard.acquire()
        phase = "p3"
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return _result(terminal_id, "unresumable", error_code="terminal_missing", interrupt=interrupt)
        previous_state = metadata.get("recovery_state")
        if previous_state in MID_STATES:
            set_terminal_recovery_state(terminal_id, "rebind_failed", "abandoned_mid_rebind")
            previous_state = "rebind_failed"
        if (
            previous_state == "rebind_failed"
            and metadata.get("recovery_error") in {"exit_failed", "exit_uncertain"}
        ):
            if not acknowledge_ownership:
                return _result(
                    terminal_id, "resume_failed",
                    error_code=metadata["recovery_error"], interrupt=interrupt,
                    retryable=False,
                )
            _persist_recovery(terminal_id, "rebind_failed")
            metadata["recovery_error"] = None
        if previous_state == "fallback_ready":
            return _result(terminal_id, "unresumable", error_code="fallback_ready", interrupt=interrupt)
        from cli_agent_orchestrator.services.terminal_service import has_deferred_init
        if has_deferred_init(terminal_id) or has_unsettled_delivery_attempt(terminal_id):
            return _result(terminal_id, "skipped_busy", error_code="obligation_in_flight", interrupt=interrupt)
        raw = status_monitor.get_raw_status(terminal_id)
        allowed = {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
        if interrupt:
            allowed.add(TerminalStatus.PROCESSING)
        if raw not in allowed:
            return _result(terminal_id, "skipped_busy", error_code=f"status_{raw.value}", interrupt=interrupt)
        old_provider = provider_manager.get_provider(terminal_id)
        if not old_provider or not getattr(old_provider, "supports_reauth_rebind", False):
            return _result(terminal_id, "unresumable", error_code="provider_unsupported", interrupt=interrupt)
        baseline = metadata.get("shell_command")
        if not baseline:
            return _result(terminal_id, "unresumable", error_code="shell_baseline_missing", interrupt=interrupt)
        pid = pane_pid(metadata["tmux_session"], metadata["tmux_window"])
        cwd = get_backend().get_pane_working_directory(metadata["tmux_session"], metadata["tmux_window"])
        session_uuid = metadata.get("provider_session_id")
        phase = "p4"
        if not session_uuid:
            try:
                session_uuid = old_provider.capture_session_uuid(pid, pane_launch_epoch(pid), cwd)
            except Exception:
                return _result(terminal_id, "capture_failed", error_code="capture_failed", interrupt=interrupt)
        try:
            old_provider.validate_session_artifact(session_uuid, cwd)
        except Exception:
            return _result(terminal_id, "unresumable", error_code="session_artifact_invalid", interrupt=interrupt)
        phase = "p5"
        _persist_recovery(terminal_id, "rebind_starting")
        phase = "p6"
        try:
            watchdog_snapshot = stalled_callback_watchdog.pause_terminal(terminal_id)
        except Exception:
            restored = set_terminal_recovery_state(terminal_id, previous_state)
            if not restored:
                set_terminal_recovery_state(
                    terminal_id, "rebind_starting", "watchdog_pause_rollback_failed"
                )
            return _result(
                terminal_id, "resume_failed", error_code="watchdog_pause_failed",
                interrupt=interrupt,
            )
        phase = "p7_persist"
        _persist_recovery(terminal_id, "rebind_exiting")
        from cli_agent_orchestrator.services.terminal_service import exit_terminal_cli
        phase = "p7_send"
        exit_terminal_cli(terminal_id)
        phase = "p7_death"
        death = await _wait_for_shell_baseline(metadata, baseline, old_provider, pid)
        if death != "exit_confirmed":
            set_terminal_recovery_state(terminal_id, "rebind_failed", death)
            return _result(terminal_id, "resume_failed", error_code=death, interrupt=interrupt,
                           retryable=False)
        exited = True
        if reason == "content-flag":
            if metadata.get("provider") != "codex" or not isinstance(
                metadata.get("lifecycle_generation"), int
            ):
                raise RuntimeError("content_recovery_provider_or_generation_invalid")
            from cli_agent_orchestrator.clients.database import get_current_mailbox_terminal
            from cli_agent_orchestrator.services.wpd1_decontam import (
                prepare_content_recovery,
            )

            options = content_options or {}
            caller_mailbox_id = metadata.get("caller_mailbox_id")
            caller_terminal_id = (
                get_current_mailbox_terminal(caller_mailbox_id)
                if isinstance(caller_mailbox_id, str)
                else None
            )
            prepared_recovery = prepare_content_recovery(
                terminal_id=terminal_id,
                lifecycle_generation=metadata["lifecycle_generation"],
                session_uuid=session_uuid,
                invoker=options.get("invoker") or caller_terminal_id or "human-cli",
                caller_mailbox_id=caller_mailbox_id,
                caller_terminal_id=caller_terminal_id,
                gating_basis=options.get("gating_basis") or "supervisor-invoked",
                force=bool(options.get("force")),
                show=bool(options.get("show")),
                ad_hoc_spans=options.get("ad_hoc_spans", ()),
                use_cpa=bool(options.get("use_cpa", True)),
            )
        phase = "p8"
        status_monitor.reset_buffer(terminal_id)
        before_output_gen = status_monitor.get_fifo_frame_gen(terminal_id)
        context = ForkContext(mode="resume", session_uuid=session_uuid, base_name="reauth",
                              provider=metadata["provider"], initial_preamble="")
        candidate = provider_manager.construct_provider(
            metadata["provider"], terminal_id, metadata["tmux_session"],
            metadata["tmux_window"], metadata.get("agent_profile"),
            metadata.get("allowed_tools"), skill_prompt=_launch_context(metadata),
            fork_context=context,
        )
        phase = "p9"
        await candidate.initialize(
            coordinates=(metadata["tmux_session"], metadata["tmux_window"]),
            provider_override=candidate,
            raw_status=True,
        )
        phase = "p10"
        provider_manager.commit_provider(terminal_id, candidate, expected_current=old_provider)
        phase = "p11"
        await _wait_for_backend_proof(
            terminal_id, metadata, candidate, before_output_gen, guard
        )
        phase = "p12"
        raw = status_monitor.get_raw_status(terminal_id, provider_override=candidate)
        if raw not in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
            raise RuntimeError("candidate_not_ready")
        phase = "p13"
        if not settle_terminal_rebound(terminal_id, session_uuid, baseline):
            raise RuntimeError("identity_persist_failed")
        phase = "p14"
        try:
            stalled_callback_watchdog.resume_terminal(terminal_id, watchdog_snapshot)
            watchdog_snapshot = None
        except Exception:
            stalled_callback_watchdog.repair_terminal_after_resume_failure(
                terminal_id, watchdog_snapshot
            )
            watchdog_snapshot = None
            set_terminal_recovery_state(terminal_id, "rebind_failed", "watchdog_resume_failed")
            if prepared_recovery is not None:
                from cli_agent_orchestrator.services.terminal_service import exit_terminal_cli
                from cli_agent_orchestrator.services.wpd1_decontam import (
                    mark_recovery_failure,
                    post_initialize_failure,
                    public_scrub_summary,
                )

                candidate_death_confirmed = False
                try:
                    exit_terminal_cli(terminal_id)
                    candidate_pid = pane_pid(metadata["tmux_session"], metadata["tmux_window"])
                    candidate_death_confirmed = (
                        await _wait_for_shell_baseline(
                            metadata, baseline, candidate, candidate_pid
                        )
                        == "exit_confirmed"
                    )
                except Exception:
                    candidate_death_confirmed = False
                if candidate_death_confirmed:
                    post_initialize_failure(prepared_recovery, "settle")
                else:
                    mark_recovery_failure(
                        prepared_recovery, "settle", restored=False
                    )
            result = _result(
                terminal_id, "resume_failed", error_code="watchdog_resume_failed",
                interrupt=interrupt,
            )
            if prepared_recovery is not None:
                result["decontamination"] = public_scrub_summary(
                    prepared_recovery, show=bool((content_options or {}).get("show"))
                )
            return result
        phase = "p15"
        try:
            await guard.close()
            guard_released = True
        except Exception:
            result = _result(
                terminal_id, "rebound", error_code="delivery_guard_release_failed",
                interrupt=interrupt,
            )
            if prepared_recovery is not None:
                from cli_agent_orchestrator.services.wpd1_decontam import (
                    mark_recovery_complete,
                    public_scrub_summary,
                )

                mark_recovery_complete(prepared_recovery)
                result["decontamination"] = public_scrub_summary(
                    prepared_recovery, show=bool((content_options or {}).get("show"))
                )
            return result
        if prepared_recovery is not None:
            from cli_agent_orchestrator.services.wpd1_decontam import (
                mark_recovery_complete,
                public_scrub_summary,
            )

            try:
                mark_recovery_complete(prepared_recovery)
            except Exception:
                result = _result(
                    terminal_id,
                    "rebound",
                    error_code="incident_update_failed",
                    interrupt=interrupt,
                )
                result["decontamination"] = public_scrub_summary(
                    prepared_recovery, show=bool((content_options or {}).get("show"))
                )
                return result
        if old_provider is not candidate:
            old_provider.cleanup()
        result = _result(terminal_id, "rebound", interrupt=interrupt)
        if prepared_recovery is not None:
            from cli_agent_orchestrator.services.wpd1_decontam import public_scrub_summary

            result["decontamination"] = public_scrub_summary(
                prepared_recovery, show=bool((content_options or {}).get("show"))
            )
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if metadata and exited:
            set_terminal_recovery_state(terminal_id, "rebind_failed", str(exc))
            fallback = None
            candidate_death_confirmed = candidate is None
            ownership_error = None
            try:
                if candidate:
                    from cli_agent_orchestrator.services.terminal_service import exit_terminal_cli
                    exit_terminal_cli(terminal_id)
                    candidate_pid = pane_pid(metadata["tmux_session"], metadata["tmux_window"])
                    candidate_death = await _wait_for_shell_baseline(
                        metadata, baseline, candidate, candidate_pid
                    )
                    candidate_death_confirmed = candidate_death == "exit_confirmed"
                    if not candidate_death_confirmed:
                        ownership_error = candidate_death
                        set_terminal_recovery_state(terminal_id, "rebind_failed", candidate_death)
                if reason == "content-flag":
                    if prepared_recovery is not None:
                        from cli_agent_orchestrator.services.wpd1_decontam import (
                            post_initialize_failure,
                            public_scrub_summary,
                            restore_backup,
                        )

                        if candidate is None:
                            restore_backup(prepared_recovery)
                            from cli_agent_orchestrator.services.wpd1_decontam import (
                                mark_recovery_failure,
                            )

                            mark_recovery_failure(
                                prepared_recovery, "resume", restored=True
                            )
                        elif candidate_death_confirmed:
                            post_initialize_failure(
                                prepared_recovery,
                                "settle" if phase in {"p13", "p14", "p15"} else "resume",
                            )
                    fallback = None
                elif session_uuid and candidate_death_confirmed:
                    fallback = await _fallback(metadata, session_uuid, lease, lifecycle_lease)
            except Exception as fallback_exc:
                set_terminal_recovery_state(terminal_id, "rebind_failed", str(fallback_exc))
                fallback = {"status": "failed", "new_terminal_id": None}
            result = _result(
                terminal_id, "resume_failed",
                error_code=(
                    ownership_error
                    or (
                        f"{getattr(exc, 'stage', 'resume')}:{getattr(exc, 'code', type(exc).__name__)}"
                        if reason == "content-flag"
                        else "resume_failed"
                    )
                ),
                interrupt=interrupt, fallback=fallback,
                retryable=False if ownership_error else True,
            )
            if prepared_recovery is not None:
                from cli_agent_orchestrator.services.wpd1_decontam import public_scrub_summary

                result["decontamination"] = public_scrub_summary(
                    prepared_recovery, show=bool((content_options or {}).get("show"))
                )
            return result
        if metadata and phase in {"p7_send", "p7_death"}:
            set_terminal_recovery_state(terminal_id, "rebind_failed", "exit_uncertain")
            return _result(
                terminal_id, "resume_failed", error_code="exit_uncertain",
                interrupt=interrupt, retryable=False,
            )
        if metadata:
            set_terminal_recovery_state(terminal_id, previous_state)
        request_phase = "p7" if phase.startswith("p7_") else phase
        return _result(
            terminal_id, "resume_failed", error_code=f"{request_phase}_request_failed",
            interrupt=interrupt,
        )
    finally:
        if watchdog_snapshot is not None:
            try:
                stalled_callback_watchdog.resume_terminal(terminal_id, watchdog_snapshot)
            except Exception:
                set_terminal_recovery_state(terminal_id, "rebind_failed", "watchdog_resume_failed")
        try:
            if not guard_released:
                await guard.close()
        finally:
            try:
                release_rebind_lease(lease)
            finally:
                try:
                    if prepared_recovery is not None:
                        from cli_agent_orchestrator.services.wpd1_decontam import (
                            release_prepared_recovery,
                        )

                        release_prepared_recovery(prepared_recovery)
                finally:
                    release_session_lifecycle_lease(lifecycle_lease)


async def recover_provider_reauth(
    session_name: str, provider: str = "codex", terminal_ids: list[str] | None = None,
    interrupt: bool = False, acknowledge_ownership: bool = False,
    *,
    reason: str = "provider-reauth",
    content_options: dict | None = None,
) -> dict:
    if acknowledge_ownership and (terminal_ids is None or len(terminal_ids) != 1):
        raise ValueError("acknowledge_ownership requires exactly one --terminal selector")
    started = datetime.now(timezone.utc).isoformat()
    rows = list_terminals_by_session(session_name)
    selected = []
    for row in rows:
        if row["provider"] != provider or (terminal_ids and row["id"] not in terminal_ids):
            continue
        state = row.get("recovery_state")
        if state in MID_STATES and not rebind_lease_held(row["id"]):
            set_terminal_recovery_state(row["id"], "rebind_failed", "abandoned_mid_rebind")
            state = "rebind_failed"
        if state in {None, "rebound", "rebind_failed"}:
            selected.append(row["id"])
    selected.sort()
    results = []
    for terminal_id in selected:
        if reason == "content-flag":
            results.append(
                await rebind_terminal(
                    terminal_id,
                    interrupt=interrupt,
                    acknowledge_ownership=acknowledge_ownership,
                    reason=reason,
                    content_options=content_options,
                )
            )
        else:
            results.append(
                await rebind_terminal(
                    terminal_id,
                    interrupt=interrupt,
                    acknowledge_ownership=acknowledge_ownership,
                )
            )
    manifest = None
    manifest_error = None
    try:
        from cli_agent_orchestrator.services.session_manifest_service import build_session_manifest
        manifest = build_session_manifest(session_name)
    except Exception as exc:
        manifest_error = str(exc)
    return {
        "schema_version": "cao.session-recover/v1", "session": session_name,
        "reason": reason, "provider": provider, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(), "results": results,
        "manifest": manifest, "manifest_error": manifest_error,
    }
