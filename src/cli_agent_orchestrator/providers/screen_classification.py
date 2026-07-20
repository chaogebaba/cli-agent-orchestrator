"""Compatibility imports for the kernel-owned screen-classification law."""

from cli_agent_orchestrator.kernel.receiver_state.classification import (
    ScreenClassification,
    ScreenClassificationResult,
    ScreenSignal,
    SignalClass,
    TemporalPolicy,
    classify_screen_signals,
    screen_classification_result,
)

__all__ = [
    "ScreenClassification",
    "ScreenClassificationResult",
    "ScreenSignal",
    "SignalClass",
    "TemporalPolicy",
    "classify_screen_signals",
    "screen_classification_result",
]
