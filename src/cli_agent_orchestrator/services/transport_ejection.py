"""F203 D9-D12/N2: Transport ejection — counted failure with backoff and active re-probe.

Envoy outlier-detection shape: N consecutive refusals of class `no_registry_records`
(rung1) or `not_registered_fallback` (fallback ring) → state EJECTED with exactly one
WARN, not a per-attempt silent defer/INFO no-op.

D10/N2: Ejection duration = base_ejection_s * consecutive_ejection_count, capped at
min(base * count, escalate_after_s).

D11: Active probing readmits — a cheap re-probe (registry re-read) un-ejects rung1.

D12: Never eject the last transport — rung2 (composer injection) is the floor.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _RungEjectionState:
    """Per-(terminal, rung) ejection state."""

    consecutive_refusals: int = 0
    ejected: bool = False
    ejection_count: int = 0
    ejected_at: float | None = None
    ejection_duration_s: float = 0.0
    # F810 (#667) D3: True once a `native_unreachable` fleet condition has been
    # emitted for the CURRENT ejection episode. Reset on readmit so a fresh
    # episode emits exactly one more. Prevents the per-attempt condition spam
    # the ejection WARN was designed to avoid (182 attempts in the evidence pack).
    native_unreachable_emitted: bool = False


class TransportEjectionService:
    """F203 D9-D12: Counted-failure ejection with backoff and re-probe.

    Tracks per-(terminal_id, rung) consecutive refusals. After N refusals,
    marks the rung as EJECTED with one WARN. Ejection expires after a
    backoff-scaled duration; active re-probe can readmit early.
    """

    # D9: ejection threshold
    EJECTION_THRESHOLD = 3

    def __init__(self) -> None:
        # Key: (terminal_id, rung_name)
        self._states: dict[tuple[str, str], _RungEjectionState] = {}

    def record_refusal(
        self,
        terminal_id: str,
        rung: str,
        reason: str,
    ) -> bool:
        """Record a refusal for (terminal, rung). Returns True if now ejected.

        D9: Emits exactly one WARN on the threshold crossing.
        """
        key = (terminal_id, rung)
        state = self._states.setdefault(key, _RungEjectionState())

        if state.ejected:
            # Already ejected — don't double-count
            return True

        state.consecutive_refusals += 1

        if state.consecutive_refusals >= self.EJECTION_THRESHOLD:
            state.ejected = True
            state.ejection_count += 1
            state.ejected_at = time.monotonic()

            # N2: duration = base_ejection_s * consecutive_ejection_count,
            # capped at escalate_after_s
            from cli_agent_orchestrator.services.config_service import ConfigService

            base_ejection_s = float(ConfigService.get("delivery.base_ejection_s", 30.0))
            escalate_after_s = float(ConfigService.get("delivery.escalate_after_s", 120.0))
            state.ejection_duration_s = min(
                base_ejection_s * state.ejection_count,
                escalate_after_s,
            )

            # D9: exactly one WARN
            logger.warning(
                "f203_transport_ejected terminal=%s rung=%s reason=%s "
                "consecutive=%d ejection_count=%d duration=%.0fs",
                terminal_id,
                rung,
                reason,
                state.consecutive_refusals,
                state.ejection_count,
                state.ejection_duration_s,
            )
            return True

        return False

    def emit_native_unreachable(
        self,
        terminal_id: str,
        rung: str,
        *,
        fleet_sink: "Callable[[str, str | None], None] | None" = None,
    ) -> bool:
        """F810 (#667) D3: emit ONE ``native_unreachable`` fleet condition/episode.

        Called from the delivery path when native delivery refused with
        ``socket_unpublished`` AND the fallback rung is not registered — the
        exact silent-ejection combination the evidence pack shows (182 deferred
        attempts, no operator-visible signal). Emits at most once per ejection
        episode for ``(terminal_id, rung)``: the per-episode flag is set here and
        cleared on readmit, so a re-ejection after a readmit emits again but a
        run of refusals inside one episode does not.

        The condition rides the SAME fleet channel as F611 CAPPED conditions —
        ``status_monitor._condition_fleet_sink(terminal_id, label)`` — so the
        fleet row / ``cao fleet`` render it without opening the seat. ``retry_after``
        (the ejection's current backoff duration, seconds) is appended to the
        label so the operator sees when the transport will re-probe. The closed
        ``ConditionKind`` taxonomy is deliberately NOT extended (its closedness
        is load-bearing for the capped-lane policy, F681 D3); this is a
        transport-observability label on the same surface, not a new operating
        state.

        ``fleet_sink`` is injectable for tests; production resolves the
        status_monitor singleton lazily. Returns True iff a condition was emitted
        on this call (False when already emitted this episode, or on sink error).
        """
        key = (terminal_id, rung)
        state = self._states.get(key)
        if state is None or not state.ejected:
            # Only meaningful while ejected — an un-ejected rung is reachable.
            return False
        if state.native_unreachable_emitted:
            return False

        retry_after = int(state.ejection_duration_s)
        label = f"native_unreachable retry_after={retry_after}s"

        sink = fleet_sink
        if sink is None:
            try:
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                sink = status_monitor._condition_fleet_sink
            except Exception:
                return False
        try:
            sink(terminal_id, label)
        except Exception:
            # A sink failure must not break delivery; leave the flag unset so a
            # later attempt in the same episode can retry the emit.
            return False

        state.native_unreachable_emitted = True
        logger.warning(
            "f810_native_unreachable terminal=%s rung=%s retry_after=%ds",
            terminal_id,
            rung,
            retry_after,
        )
        return True

    def is_ejected(self, terminal_id: str, rung: str) -> bool:
        """Check if (terminal, rung) is currently ejected.

        Checks expiry: if the ejection has expired, auto-readmit.
        """
        key = (terminal_id, rung)
        state = self._states.get(key)
        if state is None or not state.ejected:
            return False

        # Check expiry
        if state.ejected_at is not None:
            elapsed = time.monotonic() - state.ejected_at
            if elapsed >= state.ejection_duration_s:
                # Expired — auto-readmit
                self._readmit(terminal_id, rung, reason="expiry")
                return False

        return True

    def readmit(self, terminal_id: str, rung: str) -> None:
        """D11: Active re-probe readmits — un-eject the rung and clear counters."""
        self._readmit(terminal_id, rung, reason="active_reprobe")

    def _readmit(self, terminal_id: str, rung: str, reason: str) -> None:
        """Internal readmission — clears ejection state."""
        key = (terminal_id, rung)
        state = self._states.get(key)
        if state is None:
            return
        if state.ejected:
            logger.info(
                "f203_transport_readmitted terminal=%s rung=%s reason=%s",
                terminal_id,
                rung,
                reason,
            )
        state.ejected = False
        state.consecutive_refusals = 0
        state.ejected_at = None
        state.ejection_duration_s = 0.0
        # F810 (#667) D3: a readmission ends the episode, so the next one is
        # allowed to emit its own single native_unreachable condition.
        state.native_unreachable_emitted = False

    def get_state(self, terminal_id: str, rung: str) -> _RungEjectionState | None:
        """Introspection for testing."""
        return self._states.get((terminal_id, rung))

    def clear(self, terminal_id: str) -> None:
        """Clear all ejection state for a terminal."""
        keys = [k for k in self._states if k[0] == terminal_id]
        for k in keys:
            del self._states[k]


# Module-level singleton
transport_ejection_service = TransportEjectionService()
