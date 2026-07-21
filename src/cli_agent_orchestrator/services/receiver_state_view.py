"""Service-owned activation, backend bypass, and recovery projection shim."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.kernel.receiver_state.store import FreshToken, ReceiverStateStore
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.seam_activation import ConsumerOp, receiver_state_active

logger = logging.getLogger(__name__)
_backend_failure_last_logged: dict[str, float] = {}
_native_publisher_lock = threading.Lock()
_native_publisher_enabled = False
_native_poll_last: dict[str, float] = {}
NATIVE_POLL_COOLDOWN_S = 5.0

NoneBehavior = Literal["none", "legacy", "watchdog"]

if TYPE_CHECKING:
    from cli_agent_orchestrator.services.status_monitor import ProbeResult


class _StatusMonitor(Protocol):
    @property
    def receiver_state_store(self) -> ReceiverStateStore: ...

    def get_status(self, terminal_id: str) -> TerminalStatus: ...

    def get_raw_status(self, terminal_id: str, provider_override: Any = None) -> TerminalStatus: ...

    def probe_screen_status(self, terminal_id: str) -> "ProbeResult": ...

    def prove_terminal_identity(self, terminal_id: str, depth: str = "live"): ...

    def publish_native_poll(
        self, terminal_id: str, pane_id: str, fetch, fetched_at_mono: float, proof
    ): ...


@dataclass(frozen=True)
class NativeProbeResult:
    status: TerminalStatus
    meta: dict[str, Any]
    fresh_token: FreshToken


def activate_native_publisher() -> None:
    global _native_publisher_enabled
    with _native_publisher_lock:
        _native_publisher_enabled = True


def native_publisher_active() -> bool:
    with _native_publisher_lock:
        return _native_publisher_enabled


def _poll_native_once(terminal_id: str, monitor: _StatusMonitor):
    now = time.monotonic()
    last = _native_poll_last.get(terminal_id)
    if last is not None and now - last < NATIVE_POLL_COOLDOWN_S:
        return None
    _native_poll_last[terminal_id] = now
    try:
        from cli_agent_orchestrator.providers.manager import provider_manager

        backend = get_backend()
        provider = provider_manager.get_provider(terminal_id)
        metadata = get_terminal_metadata(terminal_id)
        if (
            provider is None
            or metadata is None
            or provider.capabilities.native_status_source != "herdr"
        ):
            return None
        pane_id = backend.get_pane_id(
            terminal_id, metadata["tmux_session"], metadata["tmux_window"]
        )
        proof = monitor.prove_terminal_identity(terminal_id, depth="live")
        fetch = backend.fetch_native_status(metadata["tmux_session"], metadata["tmux_window"])
        fetched_at = time.monotonic()
        token = monitor.publish_native_poll(terminal_id, pane_id, fetch, fetched_at, proof)
        return token, fetch, proof, fetched_at
    except Exception:
        logger.debug("native poll failed for %s", terminal_id, exc_info=True)
        return None


def native_probe(
    terminal_id: str, monitor: _StatusMonitor | None = None
) -> NativeProbeResult | None:
    """Run one operation-owned native poll and adapt it to delivery evidence."""

    monitor = _monitor() if monitor is None else monitor
    result = _poll_native_once(terminal_id, monitor)
    if result is None:
        return None
    token, fetch, proof, fetched_at = result
    metadata = get_terminal_metadata(terminal_id)
    if metadata is None:
        return None
    view = monitor.receiver_state_store.snapshot_view(
        (terminal_id, int(metadata["lifecycle_generation"]), str(metadata["tmux_window"])),
        require_fresh=True,
        max_age_s=2.0,
        recovery_state=metadata.get("recovery_state"),
        token=token,
    )
    status = view.latched_status if view is not None else TerminalStatus.UNKNOWN
    generation = view.native_evidence.native_event_gen if view and view.native_evidence else 0
    meta: dict[str, Any] = {
        "frame_source": "native",
        "probed_at": fetched_at,
        "agent_status": fetch.agent_status,
        "result_status": status.value,
        "native_event_gen": generation,
    }
    if fetch.failure_cause is not None:
        meta["probe_failure"] = fetch.failure_cause
    if proof.failure is not None:
        meta["identity_proof_failure"] = proof.failure
    return NativeProbeResult(status, meta, token)


def _monitor() -> _StatusMonitor:
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    return status_monitor


def snapshot_view(
    consumer_op: ConsumerOp,
    terminal_id: str,
    *,
    max_age_s: float,
    none_behavior: NoneBehavior,
    monitor: _StatusMonitor | None = None,
    require_fresh: bool = False,
    token: FreshToken | None = None,
) -> TerminalStatus | None:
    """Read one activated receiver view or preserve the row's legacy behavior."""

    monitor = _monitor() if monitor is None else monitor
    try:
        if get_backend().supports_event_inbox() and not native_publisher_active():
            return monitor.get_status(terminal_id)
    except Exception:
        now_mono = time.monotonic()
        last_logged = _backend_failure_last_logged.get(terminal_id)
        if last_logged is None or now_mono - last_logged >= 60.0:
            _backend_failure_last_logged[terminal_id] = now_mono
            logger.warning(
                "Receiver-state backend check failed; using legacy status", exc_info=True
            )
        return monitor.get_status(terminal_id)

    if not receiver_state_active(consumer_op):
        return monitor.get_status(terminal_id)

    try:
        metadata = get_terminal_metadata(terminal_id)
    except Exception:
        return monitor.get_raw_status(terminal_id)
    if metadata is None:
        status = None
    else:
        view = monitor.receiver_state_store.snapshot_view(
            (
                terminal_id,
                int(metadata["lifecycle_generation"]),
                str(metadata["tmux_window"]),
            ),
            require_fresh=require_fresh,
            max_age_s=max_age_s,
            recovery_state=metadata.get("recovery_state"),
            token=token,
        )
        status = None if view is None else view.latched_status
        if (
            view is not None
            and view.origin in {"native", "native_poll"}
            and status == TerminalStatus.UNKNOWN
        ):
            status = None

    if status is None:
        try:
            event_deployment = get_backend().supports_event_inbox()
        except Exception:
            event_deployment = False
        if event_deployment:
            poll_result = _poll_native_once(terminal_id, monitor)
            if poll_result is not None and metadata is not None:
                poll_token = poll_result[0]
                refreshed = monitor.receiver_state_store.snapshot_view(
                    (
                        terminal_id,
                        int(metadata["lifecycle_generation"]),
                        str(metadata["tmux_window"]),
                    ),
                    require_fresh=require_fresh,
                    max_age_s=max_age_s,
                    recovery_state=metadata.get("recovery_state"),
                    token=poll_token if require_fresh else None,
                )
                if refreshed is not None and refreshed.latched_status != TerminalStatus.UNKNOWN:
                    status = refreshed.latched_status

    if status == TerminalStatus.PROCESSING:
        # Legacy get_raw_status re-checks the live buffer and may advance a
        # stuck PROCESSING latch. Preserve that side effect for flipped reads,
        # then prefer the newly published receiver observation.
        raw_status = monitor.get_raw_status(terminal_id)
        try:
            refreshed = monitor.receiver_state_store.snapshot_view(
                (
                    terminal_id,
                    int(metadata["lifecycle_generation"]),
                    str(metadata["tmux_window"]),
                ),
                require_fresh=require_fresh,
                max_age_s=max_age_s,
                recovery_state=metadata.get("recovery_state"),
                token=token,
            )
        except Exception:
            refreshed = None
        if raw_status not in (TerminalStatus.PROCESSING, TerminalStatus.UNKNOWN):
            status = raw_status
        elif refreshed is not None:
            status = refreshed.latched_status

    if status is not None:
        return status
    if none_behavior == "watchdog":
        try:
            monitor.probe_screen_status(terminal_id)
        except Exception:
            logger.debug("Receiver-state watchdog probe failed for %s", terminal_id, exc_info=True)
        return monitor.get_status(terminal_id)
    if none_behavior == "legacy":
        return monitor.get_status(terminal_id)
    return None


__all__ = [
    "NoneBehavior",
    "activate_native_publisher",
    "native_publisher_active",
    "native_probe",
    "snapshot_view",
]
