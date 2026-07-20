"""Process-local ReceiverState observations and 0a read projection."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal, TypeAlias

from cli_agent_orchestrator.models.terminal import RecoveryState, TerminalStatus

from .classification import ScreenClassificationResult

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
FreshToken: TypeAlias = tuple[str, float]
ObservationOrigin: TypeAlias = Literal["incremental", "probe", "forced"]


@dataclass(frozen=True)
class ProbeGeometry:
    columns: int
    rows: int


@dataclass(frozen=True)
class ProbeLawSignal:
    signal_class: str
    provider_signal: str | None
    row_index: int | None


@dataclass(frozen=True)
class ProbeTemporalDemotion:
    frames: int
    multiset_sha256: str


@dataclass(frozen=True)
class ProbeEvidence:
    probed_at: str
    geometry: ProbeGeometry
    frame_rows_hash: str
    frame_source: FrameSource
    result_status: str
    law_signal: ProbeLawSignal
    identity_proof_failure: str | None = None
    temporal_demotion: ProbeTemporalDemotion | None = None
    transient_api_error: bool | None = None
    idle_reason: str | None = None
    injection_hazard: str | None = None
    probe_failure: str | None = None

    @classmethod
    def from_legacy_dict(cls, meta: dict[str, Any]) -> "ProbeEvidence":
        geometry = meta["geometry"]
        law = meta["law_signal"]
        temporal = meta.get("temporal_demotion")
        return cls(
            probed_at=str(meta["probed_at"]),
            geometry=ProbeGeometry(int(geometry["columns"]), int(geometry["rows"])),
            frame_rows_hash=str(meta["frame_rows_hash"]),
            frame_source=meta["frame_source"],
            result_status=str(meta["result_status"]),
            law_signal=ProbeLawSignal(
                str(law["class"]), law.get("provider_signal"), law.get("row_index")
            ),
            identity_proof_failure=meta.get("identity_proof_failure"),
            temporal_demotion=(
                ProbeTemporalDemotion(int(temporal["frames"]), str(temporal["multiset_sha256"]))
                if temporal is not None
                else None
            ),
            transient_api_error=meta.get("transient_api_error"),
            idle_reason=meta.get("idle_reason"),
            injection_hazard=meta.get("injection_hazard"),
            probe_failure=meta.get("probe_failure"),
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "probed_at": self.probed_at,
            "geometry": {"columns": self.geometry.columns, "rows": self.geometry.rows},
            "frame_rows_hash": self.frame_rows_hash,
            "frame_source": self.frame_source,
            "result_status": self.result_status,
            "law_signal": {
                "class": self.law_signal.signal_class,
                "provider_signal": self.law_signal.provider_signal,
                "row_index": self.law_signal.row_index,
            },
        }
        if self.identity_proof_failure is not None:
            result["identity_proof_failure"] = self.identity_proof_failure
        if self.probe_failure is not None:
            result["probe_failure"] = self.probe_failure
        if self.temporal_demotion is not None:
            result["temporal_demotion"] = {
                "frames": self.temporal_demotion.frames,
                "multiset_sha256": self.temporal_demotion.multiset_sha256,
            }
        if self.injection_hazard is not None:
            result["injection_hazard"] = self.injection_hazard
        if self.transient_api_error is not None:
            result["transient_api_error"] = self.transient_api_error
        if self.idle_reason is not None:
            result["idle_reason"] = self.idle_reason
        return result


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
    origin: ObservationOrigin = "incremental"
    raw_classification: ScreenClassificationResult | None = None
    probe_evidence: ProbeEvidence | None = None
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
        if self.origin == "forced" and self.raw_classification is not None:
            raise ValueError("forced observations cannot carry raw classification")
        if self.frame_source == "incremental" and self.probe_evidence is not None:
            raise ValueError("incremental observations cannot carry probe evidence")

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
    origin: ObservationOrigin
    raw_classification: ScreenClassificationResult | None
    probe_evidence: ProbeEvidence | None

    @property
    def key(self) -> ReceiverStateKey:
        return (self.terminal_id, self.lifecycle_generation, self.window_identity)


@dataclass
class _Slot:
    latest: ReceiverState | None = None
    latest_eligible_captured_at_mono: float | None = None
    fresh_token: FreshToken | None = None

    def publish(self, observation: ReceiverState, token: FreshToken | None = None) -> None:
        self.latest = observation
        self.fresh_token = token
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
        self._last_stamp: dict[str, float] = {}

    def mint_token(self, terminal_id: str, observation_epoch: str) -> FreshToken:
        """Mint a strictly increasing operation-owned token for one terminal."""

        with self._lock:
            previous = self._last_stamp.get(terminal_id, float("-inf"))
            stamp = max(time.monotonic(), previous + 1e-6)
            self._last_stamp[terminal_id] = stamp
            return (observation_epoch, stamp)

    def publish_observation(
        self, observation: ReceiverState, *, fresh_token: FreshToken | None = None
    ) -> None:
        """Publish to exactly one slot, preserving the other slot."""

        with self._lock:
            slots = self._entries.setdefault(observation.key, _Slots())
            if observation.frame_source == "incremental":
                slots.latest_incremental.publish(observation)
            else:
                slots.latest_fresh.publish(observation, fresh_token)

    def prior_classification(
        self, key: ReceiverStateKey, *, prefer_fresh: bool = False
    ) -> ScreenClassificationResult | None:
        """Return retained in-process evidence for seam-local reduction."""

        with self._lock:
            slots = self._entries.get(key)
            if slots is None:
                return None
            ordered = (
                (slots.latest_fresh, slots.latest_incremental)
                if prefer_fresh
                else (slots.latest_incremental, slots.latest_fresh)
            )
            for slot in ordered:
                if slot.latest is not None and slot.latest.raw_classification is not None:
                    return slot.latest.raw_classification
            return None

    def snapshot_view(
        self,
        key: ReceiverStateKey,
        *,
        require_fresh: bool,
        max_age_s: float,
        recovery_state: RecoveryState | None = None,
        now_mono: float | None = None,
        token: FreshToken | None = None,
    ) -> ObservationView | None:
        """Read only 0a's incremental latch, then apply the live overlay.

        ``latest_fresh`` is publication-only in Stage-0a, so a fresh-required
        request has no authorized read projection yet and returns ``None``.
        """

        if max_age_s < 0:
            raise ValueError("max_age_s must be non-negative")
        with self._lock:
            slots = self._entries.get(key)
            if slots is None:
                return None
            slot = slots.latest_fresh if require_fresh else slots.latest_incremental
            observation = slot.latest
            eligible_at = slot.latest_eligible_captured_at_mono
            stored_token = slot.fresh_token

        if observation is None or eligible_at is None:
            return None
        if require_fresh and (token is None or token != stored_token):
            return None
        if require_fresh and not observation.freshness_proof.identity_ok:
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
            origin=observation.origin,
            raw_classification=observation.raw_classification,
            probe_evidence=observation.probe_evidence,
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
    "FreshToken",
    "ObservationOrigin",
    "ObservationView",
    "PassOutcome",
    "PassSource",
    "ProbeEvidence",
    "ProbeGeometry",
    "ProbeLawSignal",
    "ProbeTemporalDemotion",
    "ReceiverState",
    "ReceiverStateKey",
    "ReceiverStateStore",
    "apply_recovery_overlay",
    "pass_outcome_for_source",
]
