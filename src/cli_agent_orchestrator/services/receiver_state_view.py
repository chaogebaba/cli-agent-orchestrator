"""Service-owned activation, backend bypass, and recovery projection shim."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

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

    def get_published_status(self, terminal_id: str) -> TerminalStatus | None: ...

    def fuse_status(
        self, terminal_id: str, status: TerminalStatus | None
    ) -> tuple[TerminalStatus | None, str | None]: ...

    def probe_screen_status(self, terminal_id: str) -> "ProbeResult": ...

    def prove_terminal_identity(self, terminal_id: str, depth: str = "live") -> Any: ...

    def publish_native_poll(
        self, terminal_id: str, pane_id: str, fetch: Any, fetched_at_mono: float, proof: Any
    ) -> FreshToken: ...


@dataclass(frozen=True)
class NativeProbeResult:
    status: TerminalStatus
    meta: dict[str, Any]
    fresh_token: FreshToken


@dataclass(frozen=True)
class ResolvedRSAnswer:
    answer: TerminalStatus | None
    rs_sourced: bool


def activate_native_publisher() -> None:
    global _native_publisher_enabled
    with _native_publisher_lock:
        _native_publisher_enabled = True


def native_publisher_active() -> bool:
    with _native_publisher_lock:
        return _native_publisher_enabled


def _poll_native_once(
    terminal_id: str, monitor: _StatusMonitor
) -> tuple[FreshToken, Any, Any, float] | None:
    now = time.monotonic()
    last = _native_poll_last.get(terminal_id)
    if last is not None and now - last < NATIVE_POLL_COOLDOWN_S:
        return None
    _native_poll_last[terminal_id] = now
    try:
        from cli_agent_orchestrator.providers.manager import provider_manager

        backend = cast(Any, get_backend())
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

    return cast(_StatusMonitor, status_monitor)


def resolve_rs_answer(
    terminal_id: str,
    *,
    max_age_s: float,
    none_behavior: NoneBehavior,
    monitor: _StatusMonitor | None = None,
    require_fresh: bool = False,
    token: FreshToken | None = None,
) -> ResolvedRSAnswer:
    """Resolve the complete receiver-state answer independently of activation."""

    monitor = _monitor() if monitor is None else monitor

    def _fuse(answer: TerminalStatus | None) -> TerminalStatus | None:
        # F506 (gate r1 B2): fuse every resolve_rs_answer egress so both
        # view_from_legacy return paths carry the fused value at the
        # delivery.admission_status seam. Idempotent by re-derivation: the
        # getter-sourced answers (get_raw_status/get_status) are already fused
        # and a second pass reproduces them; the store-sourced latched_status is
        # fused here for the first time. None passes through (R4-S1).
        fused, _reason = monitor.fuse_status(terminal_id, answer)
        return fused

    try:
        metadata = get_terminal_metadata(terminal_id)
    except Exception:
        return ResolvedRSAnswer(_fuse(monitor.get_raw_status(terminal_id)), False)
    if metadata is None:
        status = None
        rs_sourced = False
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
        rs_sourced = view is not None
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
                    rs_sourced = True

    if status == TerminalStatus.PROCESSING and metadata is not None:
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
        return ResolvedRSAnswer(_fuse(status), rs_sourced)
    if none_behavior == "watchdog":
        try:
            monitor.probe_screen_status(terminal_id)
        except Exception:
            logger.debug("Receiver-state watchdog probe failed for %s", terminal_id, exc_info=True)
        return ResolvedRSAnswer(_fuse(monitor.get_status(terminal_id)), False)
    if none_behavior == "legacy":
        return ResolvedRSAnswer(_fuse(monitor.get_status(terminal_id)), False)
    return ResolvedRSAnswer(_fuse(None), False)


def _event_inbox_bypass(terminal_id: str) -> bool:
    try:
        return bool(get_backend().supports_event_inbox() and not native_publisher_active())
    except Exception:
        now_mono = time.monotonic()
        last_logged = _backend_failure_last_logged.get(terminal_id)
        if last_logged is None or now_mono - last_logged >= 60.0:
            _backend_failure_last_logged[terminal_id] = now_mono
            logger.warning(
                "Receiver-state backend check failed; using legacy status", exc_info=True
            )
        return True


def _event_inbox_comparator_bypass(terminal_id: str) -> bool:
    try:
        return bool(get_backend().supports_event_inbox())
    except Exception:
        now_mono = time.monotonic()
        last_logged = _backend_failure_last_logged.get(terminal_id)
        if last_logged is None or now_mono - last_logged >= 60.0:
            _backend_failure_last_logged[terminal_id] = now_mono
            logger.warning(
                "Receiver-state backend check failed; bypassing parity comparison",
                exc_info=True,
            )
        return True


def view_from_legacy(
    consumer_op: ConsumerOp,
    terminal_id: str,
    legacy_answer: TerminalStatus | None,
    *,
    max_age_s: float,
    none_behavior: NoneBehavior,
    monitor: _StatusMonitor | None = None,
    require_fresh: bool = False,
    token: FreshToken | None = None,
) -> TerminalStatus | None:
    """Apply phase-based authority and shadow comparison to one legacy answer."""

    monitor = _monitor() if monitor is None else monitor
    if _event_inbox_bypass(terminal_id):
        return legacy_answer

    from cli_agent_orchestrator.services import seam_parity

    state = seam_parity.parity_state(consumer_op)
    if state is None:
        if not receiver_state_active(consumer_op):
            return legacy_answer
        return resolve_rs_answer(
            terminal_id,
            max_age_s=max_age_s,
            none_behavior=none_behavior,
            monitor=monitor,
            require_fresh=require_fresh,
            token=token,
        ).answer
    if _event_inbox_comparator_bypass(terminal_id):
        if state.phase == "collecting":
            return legacy_answer
        return resolve_rs_answer(
            terminal_id,
            max_age_s=max_age_s,
            none_behavior=none_behavior,
            monitor=monitor,
            require_fresh=require_fresh,
            token=token,
        ).answer

    try:
        rs = resolve_rs_answer(
            terminal_id,
            max_age_s=max_age_s,
            none_behavior=none_behavior,
            monitor=monitor,
            require_fresh=require_fresh,
            token=token,
        )
    except Exception:
        logger.debug("Receiver-state parity resolver failed for %s", terminal_id, exc_info=True)
        rs = ResolvedRSAnswer(None, False)

    if state.phase == "collecting":
        try:
            seam_parity.record_comparison(
                consumer_op,
                "collecting",
                legacy_answer,
                rs.answer,
                rs_sourced=rs.rs_sourced,
            )
        except Exception:
            logger.debug("Receiver-state parity sample failed for %s", consumer_op, exc_info=True)
        return legacy_answer
    if state.phase == "confirming":
        try:
            seam_parity.record_comparison(
                consumer_op,
                "confirming",
                rs.answer,
                legacy_answer,
                rs_sourced=rs.rs_sourced,
            )
        except Exception:
            logger.debug(
                "Receiver-state confirmation sample failed for %s", consumer_op, exc_info=True
            )
        return rs.answer
    return rs.answer


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
    """Read the phase-authoritative answer and collect shadow parity when active."""

    monitor = _monitor() if monitor is None else monitor
    if _event_inbox_bypass(terminal_id):
        return monitor.get_status(terminal_id)

    from cli_agent_orchestrator.services import seam_parity

    state = seam_parity.parity_state(consumer_op)
    if state is None:
        if not receiver_state_active(consumer_op):
            return monitor.get_status(terminal_id)
        return resolve_rs_answer(
            terminal_id,
            max_age_s=max_age_s,
            none_behavior=none_behavior,
            monitor=monitor,
            require_fresh=require_fresh,
            token=token,
        ).answer
    if state.phase == "done":
        return resolve_rs_answer(
            terminal_id,
            max_age_s=max_age_s,
            none_behavior=none_behavior,
            monitor=monitor,
            require_fresh=require_fresh,
            token=token,
        ).answer
    legacy_answer = monitor.get_status(terminal_id)
    return view_from_legacy(
        consumer_op,
        terminal_id,
        legacy_answer,
        max_age_s=max_age_s,
        none_behavior=none_behavior,
        monitor=monitor,
        require_fresh=require_fresh,
        token=token,
    )


__all__ = [
    "NoneBehavior",
    "ResolvedRSAnswer",
    "activate_native_publisher",
    "native_publisher_active",
    "native_probe",
    "resolve_rs_answer",
    "snapshot_view",
    "view_from_legacy",
]
