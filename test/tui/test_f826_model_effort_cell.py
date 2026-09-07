"""F826 (#683) — TUI cell/legend/detail rendering (tui/model_effort_cell.py)."""

from __future__ import annotations

import time

from cli_agent_orchestrator.tui.fleet_state import TerminalState
from cli_agent_orchestrator.tui.model_effort_cell import (
    LEGEND,
    observation_cell,
    observation_detail,
)


def _obs(value, marker, **extra):
    d = {
        "value": value,
        "marker": marker,
        "kind": None,
        "event_time": None,
        "source": None,
        "validity": None,
        "configured": None,
    }
    d.update(extra)
    return d


def test_cell_renders_value_and_marker():
    assert observation_cell("opus", _obs("Opus", "L", configured="opus")) == "Opus [L]"


def test_cell_configured_fallback_when_no_observation():
    assert observation_cell("kimi", None) == "kimi [C]"


def test_cell_unknown_when_nothing():
    assert observation_cell(None, None) == "- [?]"


def test_cell_conflict_marker_on_genuine_disagreement():
    cell = observation_cell("opus", _obs("sonnet", "L", configured="opus"))
    assert cell == "sonnet [L!]"


def test_cell_no_false_conflict_display_name_vs_id():
    """A display_name that contains the configured id is not a conflict."""
    cell = observation_cell("opus", _obs("Opus 4.6", "L", configured="opus"))
    assert cell == "Opus 4.6 [L]"  # no !


def test_cell_no_conflict_for_auto_alias():
    cell = observation_cell("opus", _obs("auto", "R", configured="opus"))
    assert cell == "auto [R]"  # alias is not a conflict


def test_cell_stale_marker():
    assert observation_cell(None, _obs("gpt", "S")) == "gpt [S]"


def test_detail_shows_kind_source_and_configured():
    now = time.time_ns()
    obs = _obs(
        "Opus", "L", kind="selected", source="claude_statusline", event_time=now, configured="opus"
    )
    detail = observation_detail("model", "opus", obs, now_ns=now)
    assert "model:" in detail
    assert "selected" in detail
    assert "claude_statusline" in detail


def test_detail_shows_validity_reason():
    obs = _obs(None, "?", validity="runtime effort unavailable from this provider")
    detail = observation_detail("effort", None, obs)
    assert "runtime effort unavailable from this provider" in detail


def test_detail_conflict_shows_configured():
    obs = _obs("sonnet", "L", configured="opus")
    detail = observation_detail("model", "opus", obs)
    assert "configured opus" in detail


def test_legend_names_all_markers():
    for m in ("[L]", "[R]", "[S]", "[C]", "[?]"):
        assert m.strip("[]") in LEGEND or m in LEGEND


def test_terminalstate_from_dict_typed_obs_not_in_extra():
    t = TerminalState.from_dict(
        {
            "id": "x",
            "model_obs": {"value": "Opus", "marker": "L"},
            "effort_obs": None,
            "new_future_key": 1,
        }
    )
    assert t.model_obs is not None
    assert t.model_obs["value"] == "Opus"
    assert t.effort_obs is None
    assert "model_obs" not in t.extra
    assert "new_future_key" in t.extra  # unknown keys still tolerated
