"""Service-owned activation, backend bypass, and recovery projection shim."""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.kernel.receiver_state.store import ReceiverStateStore
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.seam_activation import ConsumerOp, receiver_state_active

logger = logging.getLogger(__name__)

NoneBehavior = Literal["none", "legacy", "watchdog"]


class _StatusMonitor(Protocol):
    @property
    def receiver_state_store(self) -> ReceiverStateStore: ...

    def get_status(self, terminal_id: str) -> TerminalStatus: ...

    def get_raw_status(self, terminal_id: str, provider_override: Any = None) -> TerminalStatus: ...

    def probe_screen_status(self, terminal_id: str) -> tuple[TerminalStatus, object]: ...


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
) -> TerminalStatus | None:
    """Read one activated receiver view or preserve the row's legacy behavior."""

    monitor = _monitor() if monitor is None else monitor
    try:
        if get_backend().supports_event_inbox():
            return monitor.get_status(terminal_id)
    except Exception:
        logger.warning("Receiver-state backend check failed; using legacy status", exc_info=True)
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
            require_fresh=False,
            max_age_s=max_age_s,
            recovery_state=metadata.get("recovery_state"),
        )
        status = None if view is None else view.latched_status

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


__all__ = ["NoneBehavior", "snapshot_view"]
