"""Fault classes for DST liveness harness (D14).

Four real incident-derived faults, each with inject and heal:
1. user_draft_present — delivery_service.py:434 check
2. receiver_no_boundary — boundary_pull_service no notify_boundary fires
3. escalation — age past escalate_after_s triggers _escalate
4. connection_refusal — no_registry_records / not_registered_fallback

No invented fault classes — an unmodelled real fault is a coverage hole,
an invented one is a false failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class FaultKind(Enum):
    USER_DRAFT_PRESENT = auto()
    RECEIVER_NO_BOUNDARY = auto()
    ESCALATION = auto()
    CONNECTION_REFUSAL = auto()


@dataclass
class Fault:
    """A single injectable fault with metadata."""

    kind: FaultKind
    target_terminal_id: str
    injected: bool = False
    healed: bool = False
    inject_at: float = 0.0  # virtual monotonic time of injection
    heal_at: float | None = None  # virtual time of healing (set on heal)
    metadata: dict[str, object] = field(default_factory=dict)

    def inject(self, now: float) -> None:
        """Mark this fault as active."""
        self.injected = True
        self.healed = False
        self.inject_at = now

    def heal(self, now: float) -> None:
        """Heal this fault (D15 phase 2)."""
        self.healed = True
        self.heal_at = now


class FaultSet:
    """Manages the set of active faults for a simulation run.

    D15 phase semantics:
    - During CHAOS: faults may be injected
    - During HEAL: all faults are cleared
    - During REQUIRE_PROGRESS: no faults may be injected (Do-NOT #6)
    """

    def __init__(self) -> None:
        self._faults: list[Fault] = []
        self._phase: str = "CHAOS"  # CHAOS | HEAL | REQUIRE_PROGRESS

    @property
    def phase(self) -> str:
        return self._phase

    def set_phase(self, phase: str) -> None:
        assert phase in ("CHAOS", "HEAL", "REQUIRE_PROGRESS")
        self._phase = phase

    def inject(self, fault: Fault, now: float) -> None:
        """Inject a fault (only allowed during CHAOS phase)."""
        if self._phase != "CHAOS":
            raise RuntimeError(
                f"Cannot inject fault during {self._phase} phase (Do-NOT #6)"
            )
        fault.inject(now)
        self._faults.append(fault)

    def heal_all(self, now: float) -> None:
        """Heal every injected fault (D15 phase 2 transition)."""
        for f in self._faults:
            if f.injected and not f.healed:
                f.heal(now)
        self._phase = "HEAL"

    def active_faults(self) -> list[Fault]:
        """Return currently active (injected but not healed) faults."""
        return [f for f in self._faults if f.injected and not f.healed]

    def is_fault_active(self, kind: FaultKind, terminal_id: str | None = None) -> bool:
        """Check if a specific fault kind is currently active."""
        for f in self._faults:
            if f.injected and not f.healed and f.kind == kind:
                if terminal_id is None or f.target_terminal_id == terminal_id:
                    return True
        return False

    @property
    def all_faults(self) -> list[Fault]:
        return list(self._faults)
