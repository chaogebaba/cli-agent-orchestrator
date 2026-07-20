"""Provider-blind classification law for composited terminal screens."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Sequence

from cli_agent_orchestrator.models.terminal import TerminalStatus

SignalClass = Literal["waiting", "error", "progress", "completion", "chrome", "none"]
TemporalPolicy = Literal["corroborable", "exempt", "none"]


def _row_hash(row: str) -> str:
    encoded = row.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class AnchorSpec:
    provider_signal: str
    temporal_policy: TemporalPolicy


@dataclass(frozen=True)
class ScreenSignal:
    """One provider-produced signal at a physical viewport row."""

    signal_class: SignalClass
    provider_signal: str
    row_index: int
    row_bytes: str | None = None
    temporal_policy: TemporalPolicy = "none"

    def __post_init__(self) -> None:
        if self.temporal_policy == "corroborable" and self.row_bytes is None:
            raise ValueError("corroborable screen signals require row_bytes")

    def __repr__(self) -> str:
        row = self.row_bytes
        redacted = (
            None if row is None else f"<sha256:{_row_hash(row)} len={len(row.encode('utf-8'))}>"
        )
        return (
            "ScreenSignal("
            f"signal_class={self.signal_class!r}, provider_signal={self.provider_signal!r}, "
            f"row_index={self.row_index!r}, row_bytes={redacted!r}, "
            f"temporal_policy={self.temporal_policy!r})"
        )


@dataclass(frozen=True)
class ScreenClassification:
    """One result of the shared screen law, including its deciding evidence."""

    status: TerminalStatus
    signal_class: SignalClass
    provider_signal: str | None
    row_index: int | None


@dataclass(frozen=True)
class ScreenClassificationResult:
    """Shared-law classification plus the provider's complete immutable evidence."""

    classification: ScreenClassification
    signals: tuple[ScreenSignal, ...]

    @property
    def status(self) -> TerminalStatus:
        return self.classification.status

    @property
    def signal_class(self) -> SignalClass:
        return self.classification.signal_class

    @property
    def provider_signal(self) -> str | None:
        return self.classification.provider_signal

    @property
    def row_index(self) -> int | None:
        return self.classification.row_index

    def __repr__(self) -> str:
        return (
            "ScreenClassificationResult("
            f"classification={self.classification!r}, signals={self.signals!r})"
        )


def screen_classification_result(
    signals: Sequence[ScreenSignal],
    prior_signals: Sequence[ScreenSignal] = (),
    anchor_spec: AnchorSpec | None = None,
) -> ScreenClassificationResult:
    """Apply the shared law while retaining every producer signal."""

    frozen = tuple(signals)
    return ScreenClassificationResult(
        classify_screen_signals(frozen, prior_signals, anchor_spec), frozen
    )


def _select(signals: Sequence[ScreenSignal]) -> ScreenSignal:
    """Choose the lowest row, then the lexically smallest durable signal name."""

    row = max(signal.row_index for signal in signals)
    return min(
        (signal for signal in signals if signal.row_index == row),
        key=lambda signal: signal.provider_signal,
    )


def classify_screen_signals(
    current_signals: Sequence[ScreenSignal],
    prior_signals: Sequence[ScreenSignal] = (),
    anchor_spec: AnchorSpec | None = None,
) -> ScreenClassification:
    """Apply the shared law after pure, prior-frame corroboration."""

    signals = tuple(current_signals)
    prior = tuple(prior_signals)
    demoted: set[int] = set()
    if anchor_spec is None:
        remaining = Counter(
            signal.row_bytes
            for signal in prior
            if signal.signal_class == "progress"
            and signal.temporal_policy == "corroborable"
            and signal.row_bytes is not None
        )
        candidates = [
            (position, signal)
            for position, signal in enumerate(signals)
            if signal.signal_class == "progress"
            and signal.temporal_policy == "corroborable"
            and signal.row_bytes is not None
        ]
    else:
        remaining = Counter(
            signal.row_bytes
            for signal in prior
            if signal.signal_class == "progress"
            and signal.provider_signal == anchor_spec.provider_signal
            and signal.temporal_policy == anchor_spec.temporal_policy
            and signal.row_bytes is not None
        )
        candidates = [
            (position, signal)
            for position, signal in enumerate(signals)
            if signal.signal_class == "progress"
            and signal.provider_signal == anchor_spec.provider_signal
            and signal.temporal_policy == anchor_spec.temporal_policy
            and signal.row_bytes is not None
        ]
    for position, signal in sorted(candidates, key=lambda item: (item[1].row_index, item[0])):
        if remaining[signal.row_bytes] > 0:
            remaining[signal.row_bytes] -= 1
            demoted.add(position)
    effective = tuple(signal for position, signal in enumerate(signals) if position not in demoted)

    for signal_class, status in (
        ("waiting", TerminalStatus.WAITING_USER_ANSWER),
        ("error", TerminalStatus.ERROR),
    ):
        candidates = [signal for signal in effective if signal.signal_class == signal_class]
        if candidates:
            selected = _select(candidates)
            return ScreenClassification(
                status, selected.signal_class, selected.provider_signal, selected.row_index
            )

    flow = [signal for signal in effective if signal.signal_class in {"progress", "completion"}]
    if flow:
        newest_row = max(signal.row_index for signal in flow)
        newest = [signal for signal in flow if signal.row_index == newest_row]
        # Equal-row cross-class tie: live progress wins. Same-class ties then use
        # the frozen lexical provider-signal rule.
        progress = [signal for signal in newest if signal.signal_class == "progress"]
        selected = _select(progress or newest)
        status = (
            TerminalStatus.PROCESSING
            if selected.signal_class == "progress"
            else TerminalStatus.COMPLETED
        )
        return ScreenClassification(
            status, selected.signal_class, selected.provider_signal, selected.row_index
        )

    chrome = [signal for signal in effective if signal.signal_class == "chrome"]
    if chrome:
        selected = _select(chrome)
        return ScreenClassification(
            TerminalStatus.IDLE,
            selected.signal_class,
            selected.provider_signal,
            selected.row_index,
        )

    return ScreenClassification(TerminalStatus.UNKNOWN, "none", None, None)


__all__ = [
    "AnchorSpec",
    "ScreenClassification",
    "ScreenClassificationResult",
    "ScreenSignal",
    "SignalClass",
    "TemporalPolicy",
    "classify_screen_signals",
    "screen_classification_result",
]
