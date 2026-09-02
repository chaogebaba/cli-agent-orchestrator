"""F702 (#557) J1: FleetState — the immutable snapshot (D2)."""

import dataclasses

import pytest

from cli_agent_orchestrator.tui.fleet_state import FleetState, TerminalState

RAW = {
    "session_name": "orch",
    "terminals": [
        {
            "id": "t1",
            "profile": "chao_supervisor",
            "provider": "claude_code",
            "window_index": 0,
            "window_name": "supervisor",
            "parent_id": None,
            "depth": 0,
            "orphan": False,
            "status": "idle",
            "condition": None,
            "fusion_changed": False,
            "fusion_reason": None,
            "delegating": True,
            "children_count": 3,
            "init_state": "ready",
            "init_health": "healthy",
            "since_last_input": 12.5,
            "lifecycle": "durable",
            "resolved_model": "opus",
            "reparented_from": None,
            "config_stale": False,
            "wedge_suspect": False,
        }
    ],
    "wake_exhaustion_alarms": [{"mailbox_id": 7, "wake_streak": 4}],
}


def test_from_dict_types_every_terminal_key() -> None:
    state = FleetState.from_dict(RAW, fetched_at=100.0)
    assert state.session_name == "orch"
    assert len(state.terminals) == 1
    row = state.terminals[0]
    assert (row.id, row.profile, row.provider) == ("t1", "chao_supervisor", "claude_code")
    assert (row.window_index, row.window_name) == (0, "supervisor")
    assert (row.parent_id, row.depth, row.orphan) == (None, 0, False)
    assert (row.status, row.condition) == ("idle", None)
    assert (row.fusion_changed, row.fusion_reason) == (False, None)
    assert (row.delegating, row.children_count) == (True, 3)
    assert (row.init_state, row.init_health) == ("ready", "healthy")
    assert row.since_last_input == 12.5
    assert (row.lifecycle, row.resolved_model, row.reparented_from) == ("durable", "opus", None)
    assert (row.config_stale, row.wedge_suspect) == (False, False)
    assert state.wake_exhaustion_alarms[0]["wake_streak"] == 4


def test_from_dict_sets_the_three_fetcher_fields() -> None:
    state = FleetState.from_dict(RAW, fetched_at=100.0)
    assert state.fetched_at == 100.0
    assert state.stale_for == 0.0
    assert state.last_error is None


def test_from_dict_tolerates_unknown_keys() -> None:
    raw = {
        "session_name": "orch",
        "terminals": [{"id": "t1", "brand_new_key": "v"}],
        "wake_exhaustion_alarms": [],
        "future_top_level": 1,
    }
    state = FleetState.from_dict(raw, fetched_at=1.0)
    assert state.terminals[0].extra["brand_new_key"] == "v"
    assert state.extra["future_top_level"] == 1


def test_from_dict_tolerates_missing_keys() -> None:
    state = FleetState.from_dict({"terminals": [{"id": "t1"}]}, fetched_at=1.0)
    row = state.terminals[0]
    assert row.status == ""
    assert row.lifecycle == "ephemeral"
    assert row.children_count == 0
    assert row.since_last_input is None
    assert state.wake_exhaustion_alarms == ()


def test_empty_is_the_never_fetched_value() -> None:
    state = FleetState.empty()
    assert state.terminals == ()
    assert state.wake_exhaustion_alarms == ()
    assert state.stale_for == 0.0
    assert state.last_error is None
    assert state.fetched_at is None


def test_state_is_frozen() -> None:
    state = FleetState.empty()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.stale_for = 5.0  # type: ignore[misc]
    row = TerminalState.from_dict({"id": "t1"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.status = "busy"  # type: ignore[misc]


def test_with_failure_keeps_rows_and_grows_stale_for() -> None:
    state = FleetState.from_dict(RAW, fetched_at=100.0)
    first = state.with_failure("timed out", now=103.0)
    second = first.with_failure("timed out", now=107.0)
    third = second.with_failure("connection refused", now=115.0)

    # Rows survive: a fetch failure is staleness, not an error row (#441).
    assert [r.id for r in third.terminals] == ["t1"]
    assert (first.stale_for, second.stale_for, third.stale_for) == (3.0, 7.0, 15.0)
    assert third.last_error == "connection refused"
    # The original snapshot is untouched.
    assert state.last_error is None and state.stale_for == 0.0


def test_success_after_failure_clears_the_error() -> None:
    stale = FleetState.from_dict(RAW, fetched_at=100.0).with_failure("boom", now=140.0)
    assert stale.stale_for == 40.0
    fresh = FleetState.from_dict(RAW, fetched_at=141.0)
    assert fresh.last_error is None
    assert fresh.stale_for == 0.0


def test_with_failure_on_never_fetched_state_has_no_reference_point() -> None:
    state = FleetState.empty().with_failure("connection refused", now=50.0)
    assert state.last_error == "connection refused"
    assert state.stale_for == 0.0
    assert state.fetched_at is None
