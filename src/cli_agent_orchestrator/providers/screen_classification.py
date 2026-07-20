"""Compatibility imports for the kernel-owned screen-classification law."""

from cli_agent_orchestrator.kernel.receiver_state.classification import (
    AnchorSpec,
    ScreenClassification,
    ScreenClassificationResult,
    ScreenSignal,
    SignalClass,
    TemporalPolicy,
    classify_screen_signals,
    screen_classification_result,
)

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
