"""Process-local ReceiverState observations and 0a read projection."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Literal, TypeAlias

from cli_agent_orchestrator.models.terminal import RecoveryState, TerminalStatus

FrameSource: TypeAlias = Literal["incremental", "fresh_capture"]
PassSource: TypeAlias = Literal["inline", "forced"]
PassOutcome: TypeAlias = Literal[
    "accepted",
    "no_change",
    "stale_seq",
    "unknown_suppressed",
    "sticky_rejected",
    "forced",
    "probe",
    "aborted",
]
FreshnessKind: TypeAlias = Literal["not_probed", "identity_ok", "identity_failed", "probe_failed"]
ReceiverStateKey: TypeAlias = tuple[str, int, str]

_INELIGIBLE_OUTCOMES = frozenset({"stale_seq", "aborted"})
_FRESHNESS_KINDS = frozenset({"not_probed", "identity_ok", "identity_failed", "probe_failed"})
_PASS_OUTCOMES = frozenset(
    {
        "accepted",
        "no_change",
        "stale_seq",
        "unknown_suppressed",
        "sticky_rejected",
        "forced",
        "probe",
        "aborted",
    }
)


@dataclass(frozen=True)
class FreshnessProof:
    """Closed identity/probe result attached to one observation."""

    kind: FreshnessKind
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _FRESHNESS_KINDS:
            raise ValueError(f"unknown freshness proof kind: {self.kind}")

    @property
    def identity_ok(self) -> bool:
        return self.kind == "identity_ok"


@dataclass(frozen=True)
class ReceiverState:
    """One immutable monitor or fresh-probe observation.

    For ``fresh_capture`` observations, ``latched_status`` is the probe-settled
    status. It is intentionally not a mutation of the monitor's status latch.
    """

    terminal_id: str
    lifecycle_generation: int
    window_identity: str
    observation_epoch: str
    observation_sequence: int
    provider: str
    frame_source: FrameSource
    captured_at_mono: float
    frame_hash: str | None
    latched_status: TerminalStatus
    pass_outcome: PassOutcome
    freshness_proof: FreshnessProof
    freshness_eligible: bool = field(init=False)

    def __post_init__(self) -> None:
        if self.pass_outcome not in _PASS_OUTCOMES:
            raise ValueError(f"unknown pass outcome: {self.pass_outcome}")
        object.__setattr__(
            self,
            "freshness_eligible",
            self.pass_outcome not in _INELIGIBLE_OUTCOMES,
        )
        if self.frame_source == "fresh_capture" and self.pass_outcome != "probe":
            raise ValueError("fresh_capture observations require pass_outcome='probe'")
        if self.frame_source == "incremental" and self.pass_outcome == "probe":
            raise ValueError("incremental observations cannot use pass_outcome='probe'")

    @property
    def key(self) -> ReceiverStateKey:
        """Return the terminal generation and tmux-window identity key."""

        return (self.terminal_id, self.lifecycle_generation, self.window_identity)


@dataclass(frozen=True)
class ObservationView:
    """Narrow consumer projection of the latest eligible incremental slot."""

    terminal_id: str
    lifecycle_generation: int
    window_identity: str
    observation_epoch: str
    observation_sequence: int
    provider: str
    frame_source: FrameSource
    captured_at_mono: float
    frame_hash: str | None
    latched_status: TerminalStatus
    pass_outcome: PassOutcome
    freshness_proof: FreshnessProof
    freshness_eligible: bool

    @property
    def key(self) -> ReceiverStateKey:
        return (self.terminal_id, self.lifecycle_generation, self.window_identity)


@dataclass
class _Slot:
    latest: ReceiverState | None = None
    latest_eligible_captured_at_mono: float | None = None

    def publish(self, observation: ReceiverState) -> None:
        self.latest = observation
        if observation.freshness_eligible:
            self.latest_eligible_captured_at_mono = observation.captured_at_mono


@dataclass
class _Slots:
    latest_incremental: _Slot = field(default_factory=_Slot)
    latest_fresh: _Slot = field(default_factory=_Slot)


def pass_outcome_for_source(pass_source: PassSource, settled_outcome: PassOutcome) -> PassOutcome:
    """Map a successful forced pass to its explicit provenance outcome."""

    if settled_outcome == "aborted":
        return "aborted"
    if pass_source == "forced":
        return "forced"
    return settled_outcome


def apply_recovery_overlay(
    status: TerminalStatus, recovery_state: RecoveryState | None
) -> TerminalStatus:
    """Apply the legacy live recovery-state ERROR projection."""

    if recovery_state not in (None, "rebound"):
        return TerminalStatus.ERROR
    return status


class ReceiverStateStore:
    """Synchronous two-slot ReceiverState store guarded by one reentrant lock."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[ReceiverStateKey, _Slots] = {}

    def publish_observation(self, observation: ReceiverState) -> None:
        """Publish to exactly one slot, preserving the other slot."""

        with self._lock:
            slots = self._entries.setdefault(observation.key, _Slots())
            if observation.frame_source == "incremental":
                slots.latest_incremental.publish(observation)
            else:
                slots.latest_fresh.publish(observation)

    def snapshot_view(
        self,
        key: ReceiverStateKey,
        *,
        require_fresh: bool,
        max_age_s: float,
        recovery_state: RecoveryState | None = None,
        now_mono: float | None = None,
    ) -> ObservationView | None:
        """Read only 0a's incremental latch, then apply the live overlay.

        ``latest_fresh`` is publication-only in Stage-0a, so a fresh-required
        request has no authorized read projection yet and returns ``None``.
        """

        if max_age_s < 0:
            raise ValueError("max_age_s must be non-negative")
        if require_fresh:
            return None

        with self._lock:
            slots = self._entries.get(key)
            if slots is None:
                return None
            observation = slots.latest_incremental.latest
            eligible_at = slots.latest_incremental.latest_eligible_captured_at_mono

        if observation is None or eligible_at is None:
            return None
        observed_now = time.monotonic() if now_mono is None else now_mono
        if observed_now - eligible_at > max_age_s:
            return None

        return ObservationView(
            terminal_id=observation.terminal_id,
            lifecycle_generation=observation.lifecycle_generation,
            window_identity=observation.window_identity,
            observation_epoch=observation.observation_epoch,
            observation_sequence=observation.observation_sequence,
            provider=observation.provider,
            frame_source=observation.frame_source,
            captured_at_mono=observation.captured_at_mono,
            frame_hash=observation.frame_hash,
            latched_status=apply_recovery_overlay(observation.latched_status, recovery_state),
            pass_outcome=observation.pass_outcome,
            freshness_proof=observation.freshness_proof,
            freshness_eligible=observation.freshness_eligible,
        )

    def invalidate(self, key: ReceiverStateKey) -> bool:
        """Drop one exact terminal-generation-window incarnation."""

        with self._lock:
            return self._entries.pop(key, None) is not None

    def invalidate_terminal(self, terminal_id: str) -> int:
        """Drop every generation/window entry owned by a deleted terminal."""

        with self._lock:
            keys = [key for key in self._entries if key[0] == terminal_id]
            for key in keys:
                del self._entries[key]
            return len(keys)


__all__ = [
    "FrameSource",
    "FreshnessKind",
    "FreshnessProof",
    "ObservationView",
    "PassOutcome",
    "PassSource",
    "ReceiverState",
    "ReceiverStateKey",
    "ReceiverStateStore",
    "apply_recovery_overlay",
    "pass_outcome_for_source",
]
