"""F702 (#557) D2: one immutable snapshot of the fleet endpoint.

`FleetState` is a frozen dataclass built from the `GET /sessions/{s}/fleet`
payload (``services/fleet_service.py:236-281``) plus exactly three fetcher
fields — ``fetched_at``, ``stale_for``, ``last_error`` — which are connection
metadata, never terminal state. No per-terminal value is derived across
fetches: widgets read the current snapshot only.

Unknown keys are tolerated. Any key the server adds that this module does not
name lands verbatim in ``TerminalState.extra`` / ``FleetState.extra`` rather
than raising, so a server-side addition never breaks the renderer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["FleetState", "TerminalState"]

_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})

# Per-terminal keys projected by build_fleet() (fleet_service.py:236-272).
_TERMINAL_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "profile",
        "provider",
        "window_index",
        "window_name",
        "parent_id",
        "depth",
        "orphan",
        "status",
        "condition",
        "fusion_changed",
        "fusion_reason",
        "delegating",
        "children_count",
        "init_state",
        "init_health",
        "since_last_input",
        "lifecycle",
        "resolved_model",
        "reparented_from",
        "config_stale",
        "wedge_suspect",
    }
)

# Top-level keys of the fleet payload (fleet_service.py:277-281).
_FLEET_KEYS: frozenset[str] = frozenset({"session_name", "terminals", "wake_exhaustion_alarms"})


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass(frozen=True, slots=True)
class TerminalState:
    """One row of ``terminals[]``.

    Every key ``build_fleet()`` emits is typed here; anything else is kept in
    ``extra``.
    """

    id: str
    profile: str | None = None
    provider: str | None = None
    window_index: int | None = None
    window_name: str | None = None
    parent_id: str | None = None
    depth: int = 0
    orphan: bool = False
    status: str = ""
    condition: str | None = None
    fusion_changed: bool = False
    fusion_reason: str | None = None
    delegating: bool = False
    children_count: int = 0
    init_state: str | None = None
    init_health: str | None = None
    since_last_input: float | None = None
    lifecycle: str = "ephemeral"
    resolved_model: str | None = None
    reparented_from: str | None = None
    config_stale: bool = False
    wedge_suspect: bool = False
    extra: Mapping[str, Any] = field(default=_EMPTY_MAP)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TerminalState":
        extra = {k: v for k, v in raw.items() if k not in _TERMINAL_KEYS}
        return cls(
            id=_as_str(raw.get("id")),
            profile=_as_opt_str(raw.get("profile")),
            provider=_as_opt_str(raw.get("provider")),
            window_index=_as_opt_int(raw.get("window_index")),
            window_name=_as_opt_str(raw.get("window_name")),
            parent_id=_as_opt_str(raw.get("parent_id")),
            depth=_as_int(raw.get("depth")),
            orphan=bool(raw.get("orphan")),
            status=_as_str(raw.get("status")),
            condition=_as_opt_str(raw.get("condition")),
            fusion_changed=bool(raw.get("fusion_changed")),
            fusion_reason=_as_opt_str(raw.get("fusion_reason")),
            delegating=bool(raw.get("delegating")),
            children_count=_as_int(raw.get("children_count")),
            init_state=_as_opt_str(raw.get("init_state")),
            init_health=_as_opt_str(raw.get("init_health")),
            since_last_input=_as_opt_float(raw.get("since_last_input")),
            lifecycle=_as_str(raw.get("lifecycle")) or "ephemeral",
            resolved_model=_as_opt_str(raw.get("resolved_model")),
            reparented_from=_as_opt_str(raw.get("reparented_from")),
            config_stale=bool(raw.get("config_stale")),
            wedge_suspect=bool(raw.get("wedge_suspect")),
            extra=MappingProxyType(extra) if extra else _EMPTY_MAP,
        )


@dataclass(frozen=True, slots=True)
class FleetState:
    """Immutable fleet snapshot plus the three fetcher fields (D2)."""

    session_name: str = ""
    terminals: tuple[TerminalState, ...] = ()
    wake_exhaustion_alarms: tuple[Mapping[str, Any], ...] = ()
    extra: Mapping[str, Any] = field(default=_EMPTY_MAP)
    # --- fetcher fields: connection metadata, never terminal state (BL4) ---
    fetched_at: float | None = None
    stale_for: float = 0.0
    last_error: str | None = None

    @classmethod
    def empty(cls) -> "FleetState":
        """The never-fetched value (D5's seventh fixture)."""
        return cls(
            session_name="",
            terminals=(),
            wake_exhaustion_alarms=(),
            fetched_at=None,
            stale_for=0.0,
            last_error=None,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], fetched_at: float) -> "FleetState":
        """Build a snapshot from one successful fetch.

        A successful fetch clears ``last_error`` and zeroes ``stale_for``.
        """
        rows = raw.get("terminals")
        terminals: tuple[TerminalState, ...] = ()
        if isinstance(rows, (list, tuple)):
            terminals = tuple(
                TerminalState.from_dict(row) for row in rows if isinstance(row, Mapping)
            )
        alarms_raw = raw.get("wake_exhaustion_alarms")
        alarms: tuple[Mapping[str, Any], ...] = ()
        if isinstance(alarms_raw, (list, tuple)):
            alarms = tuple(MappingProxyType(dict(a)) for a in alarms_raw if isinstance(a, Mapping))
        extra = {k: v for k, v in raw.items() if k not in _FLEET_KEYS}
        return cls(
            session_name=_as_str(raw.get("session_name")),
            terminals=terminals,
            wake_exhaustion_alarms=alarms,
            extra=MappingProxyType(extra) if extra else _EMPTY_MAP,
            fetched_at=fetched_at,
            stale_for=0.0,
            last_error=None,
        )

    def with_failure(self, msg: str, *, now: float | None = None) -> "FleetState":
        """Copy of this snapshot with the failure recorded.

        Rows are kept (a fetch failure is staleness, not an error row — #441).
        ``stale_for`` is recomputed from ``fetched_at``; a snapshot that never
        fetched successfully has no reference point, so its ``stale_for``
        stays at its current value.
        """
        wall = time.time() if now is None else now
        stale_for = self.stale_for if self.fetched_at is None else max(0.0, wall - self.fetched_at)
        return replace(self, last_error=msg, stale_for=stale_for)
