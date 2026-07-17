"""Provider-blind classification law for composited terminal screens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from cli_agent_orchestrator.models.terminal import TerminalStatus

SignalClass = Literal["waiting", "error", "progress", "completion", "chrome", "none"]
TemporalPolicy = Literal["corroborable", "exempt", "none"]


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


def screen_classification_result(signals: Sequence[ScreenSignal]) -> ScreenClassificationResult:
    """Apply the shared law while retaining every producer signal."""

    frozen = tuple(signals)
    return ScreenClassificationResult(classify_screen_signals(frozen), frozen)


def _select(signals: Sequence[ScreenSignal]) -> ScreenSignal:
    """Choose the lowest row, then the lexically smallest durable signal name."""

    row = max(signal.row_index for signal in signals)
    return min(
        (signal for signal in signals if signal.row_index == row),
        key=lambda signal: signal.provider_signal,
    )


def classify_screen_signals(signals: Sequence[ScreenSignal]) -> ScreenClassification:
    """Apply the Wave 4 shared classification law without provider branches."""

    for signal_class, status in (
        ("waiting", TerminalStatus.WAITING_USER_ANSWER),
        ("error", TerminalStatus.ERROR),
    ):
        candidates = [signal for signal in signals if signal.signal_class == signal_class]
        if candidates:
            selected = _select(candidates)
            return ScreenClassification(
                status, selected.signal_class, selected.provider_signal, selected.row_index
            )

    flow = [signal for signal in signals if signal.signal_class in {"progress", "completion"}]
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

    chrome = [signal for signal in signals if signal.signal_class == "chrome"]
    if chrome:
        selected = _select(chrome)
        return ScreenClassification(
            TerminalStatus.IDLE,
            selected.signal_class,
            selected.provider_signal,
            selected.row_index,
        )

    return ScreenClassification(TerminalStatus.UNKNOWN, "none", None, None)
