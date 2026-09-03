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
from typing import Any, Mapping, Sequence

__all__ = ["FleetState", "StatusClock", "StatusSpell", "TerminalState"]

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


def _as_window_index(value: Any) -> int | None:
    """``window_index`` accepting the two shapes the server actually emits.

    F702 parity: the tmux client stringifies the index
    (``clients/tmux.py:1423`` — ``{"index": str(window.index)}``) and
    ``build_fleet`` passes that value through untouched, so a live payload
    carries ``"window_index": "2"`` while the test fixtures carry ``2``. The
    stricter :func:`_as_opt_int` mapped every live value to ``None``, which is
    why the WIN column rendered ``?`` for every row against a real server. The
    retiring stdlib script never saw this because it only ever called ``str()``
    on the raw value (``fleet-tui.py:353``).

    Accepted: an ``int`` (never a ``bool``), or a string of an integer with
    surrounding whitespace tolerated. Anything else is ``None`` — the honest
    "no window" answer that renders as ``?``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


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
            window_index=_as_window_index(raw.get("window_index")),
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


# ─── Time in the current status (F702 #557 "elapsed" round) ───────────────────
#
# "How long has this worker been working?" needs the moment a terminal entered
# the status it is in now. **The server does not publish it.** ``build_fleet()``
# (``services/fleet_service.py:250-300``) emits no transition timestamp, and the
# nearest key, ``since_last_input``, comes from the row's ``last_active``, which
# ``clients/database.py:4842`` writes *only on input delivery* — so it misses a
# seat a human drove by typing into the pane, which is why F467 marks it
# unreliable and the retiring script refuses it outright
# (``scripts/fleet-tui.py:21-22``).
#
# The field the server should grow is ``status_since``: the UTC stamp at which
# ``StatusMonitor`` last changed a terminal's fused status, published next to
# ``status``. :class:`StatusClock` reads it the moment it appears — until then it
# watches the fetches go by and times the transitions itself.


@dataclass(frozen=True, slots=True)
class StatusSpell:
    """One unbroken run of one terminal in one status.

    Attributes:
        status: the status the terminal has been in since :attr:`since`.
        since: clock reading of the first fetch that observed this spell.
        exact: ``True`` when the *transition into* this status was witnessed —
            the clock had already seen the same terminal in a different status,
            so ``since`` is right to within one poll interval. ``False`` when the
            terminal was already in this status the first time it was ever seen,
            which makes any elapsed time computed from it a lower bound.
    """

    status: str
    since: float
    exact: bool


class StatusClock:
    """Time-in-current-status for every terminal, tracked across fetches.

    Deliberately the only thing in this module that remembers anything between
    snapshots. It is safe to do so here, and it is not a route back to #439's
    sticky latch, because of three properties the tests pin:

    * it never reports or influences a *status* — only how long one has lasted,
      and the status it answers about is always the one in the current snapshot;
    * a status change replaces the spell outright, so a stale reading cannot
      survive a transition;
    * a terminal that leaves the fleet is forgotten entirely.

    The clock keys on ``status`` alone, as the fused status is what the STATUS
    column shows. A ``condition`` (F611 #467) coming or going is not a
    transition: a seat that has been ``completed`` for an hour has been
    completed for an hour whether or not its provider also flagged ``BUSY``
    somewhere in the middle.
    """

    __slots__ = ("_spells",)

    def __init__(self) -> None:
        self._spells: dict[str, StatusSpell] = {}

    def observe(self, terminals: "Sequence[TerminalState]", now: float) -> None:
        """Fold one snapshot in: open, keep or drop each terminal's spell."""
        present = set()
        for term in terminals:
            present.add(term.id)
            current = self._spells.get(term.id)
            if current is not None and current.status == term.status:
                continue
            # A terminal already known in another status means the transition
            # itself was seen, which is what makes the new spell exact.
            self._spells[term.id] = StatusSpell(term.status, now, current is not None)
        for gone in set(self._spells) - present:
            del self._spells[gone]

    def spell(self, terminal_id: str) -> StatusSpell | None:
        """The open spell of one terminal, or ``None`` if it has none yet."""
        return self._spells.get(terminal_id)

    def elapsed(self, terminal_id: str, now: float) -> float | None:
        """Seconds the terminal has been in its current status, or ``None``.

        Never negative: a clock that jumps backwards reads as zero rather than
        rendering a negative age.
        """
        current = self._spells.get(terminal_id)
        if current is None:
            return None
        return max(0.0, now - current.since)

    def tracked(self) -> "frozenset[str]":
        """The terminal ids with an open spell — the whole of this clock's memory."""
        return frozenset(self._spells)
