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

logger = logging.getLogger(__name__)


@dataclass
class _RungEjectionState:
    """Per-(terminal, rung) ejection state."""

    consecutive_refusals: int = 0
    ejected: bool = False
    ejection_count: int = 0
    ejected_at: float | None = None
    ejection_duration_s: float = 0.0


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
