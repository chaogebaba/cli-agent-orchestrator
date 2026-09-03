"""Port Protocols — the contract lanes B and C build against (WP-ARCH phase 1).

These are shape tests, not behaviour tests.  Their value is that a rename or a
dropped method in ``core/ports.py`` fails HERE, in one place, instead of as a
mysterious ``AttributeError`` inside a producer three files away.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from cli_agent_orchestrator.core import ports


def test_clock_is_satisfied_structurally() -> None:
    """A plain class with ``now()`` is a ``Clock`` — no inheritance from core."""

    class FrozenClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 2, tzinfo=UTC)

    assert isinstance(FrozenClock(), ports.Clock)


def test_a_class_missing_the_method_is_not_a_clock() -> None:
    class NotAClock:
        pass

    assert not isinstance(NotAClock(), ports.Clock)


def test_event_store_surface() -> None:
    """The five methods every consumer in phases 1-2 calls."""
    expected = {"append", "read", "get", "high_water", "prune"}
    assert expected <= set(dir(ports.EventStore))


def test_finding_store_surface() -> None:
    assert {"record", "list_findings", "resolve"} <= set(dir(ports.FindingStore))


def test_state_store_surface() -> None:
    """The projector's port, including the two column-only liveness updates."""
    expected = {"get", "upsert", "touch_probe", "touch_source_probe", "all_terminals"}
    assert expected <= set(dir(ports.StateStore))


_AC6_COLUMNS = (
    "terminal_id",
    "state",
    "since",
    "last_event_seq",
    "degraded_reason",
    "prior_state",
    "last_probe_at",
    "last_source_probe_at",
    "pane_pid",
    "pane_present",
    "miss_count",
)


def test_state_projection_carries_the_ac6_columns() -> None:
    """Every column AC6 names is on the projection Protocol."""
    assert set(_AC6_COLUMNS) <= set(dir(ports.StateProjection))


def test_state_projection_members_are_read_only() -> None:
    """Read-only properties, so an implementation may be a frozen dataclass.

    A plain annotation makes mypy treat the member as settable and locks out
    frozen implementations; a read-only property is still satisfied by a plain
    attribute, so this is the more permissive form.
    """
    for name in _AC6_COLUMNS:
        member = getattr(ports.StateProjection, name)
        assert isinstance(member, property), name
        assert member.fset is None, name


def test_a_frozen_dataclass_satisfies_the_projection_protocol() -> None:
    """The point of the change, asserted rather than argued."""
    from dataclasses import dataclass
    from datetime import UTC, datetime

    from cli_agent_orchestrator.core.states import DegradedReason, WorkerState

    @dataclass(frozen=True)
    class FrozenRow:
        terminal_id: str
        state: WorkerState
        since: datetime
        last_event_seq: int
        degraded_reason: DegradedReason | None
        prior_state: WorkerState | None
        last_probe_at: datetime | None
        last_source_probe_at: datetime | None
        pane_pid: int | None
        pane_present: bool
        miss_count: int

    row: ports.StateProjection = FrozenRow(
        terminal_id="t-1",
        state=WorkerState.IDLE,
        since=datetime(2026, 9, 2, tzinfo=UTC),
        last_event_seq=3,
        degraded_reason=None,
        prior_state=None,
        last_probe_at=None,
        last_source_probe_at=None,
        pane_pid=None,
        pane_present=True,
        miss_count=0,
    )
    assert row.terminal_id == "t-1"


def test_event_source_declares_authority() -> None:
    """Source-level precedence needs the adapter to DECLARE, not the projector to guess."""
    assert {"name", "is_authoritative", "start", "stop"} <= set(dir(ports.EventSource))
    assert inspect.iscoroutinefunction(ports.EventSource.start)
    assert inspect.iscoroutinefunction(ports.EventSource.stop)


def test_check_runner_surface() -> None:
    assert "on_append" in dir(ports.CheckRunner)


def test_later_phase_ports_exist_as_stubs() -> None:
    """Phases 3, 4 and 5 have their shape reserved so the contracts are final now."""
    assert "enqueue" in dir(ports.QueueStore)
    assert "start_run" in dir(ports.GateStore)
    assert {"name", "structured_events", "event_source"} <= set(dir(ports.ProviderAdapter))
