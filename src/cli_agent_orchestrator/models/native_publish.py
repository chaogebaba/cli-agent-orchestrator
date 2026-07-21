"""Leaf DTOs shared by the native status publisher and dispatch seams."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NativePublishRequest:
    terminal_id: str
    pane_id: str
    generation: int
    agent_status: str
    received_at_mono: float


@dataclass(frozen=True)
class DispatchTxn:
    terminal_id: str
    dispatch_gen: int
    begun_at_mono: float


@dataclass(frozen=True)
class SettlementFence:
    native_event_gen: int
    dispatch_gen: int


__all__ = ["DispatchTxn", "NativePublishRequest", "SettlementFence"]
