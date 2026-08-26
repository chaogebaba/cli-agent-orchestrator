"""F506 pane sampler unit tests (AC1, AC6, AC17, AC20, AC22 mechanics)."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _pane():
    return PaneLivenessService(_clock=_Clock())


# ---- AC1: single-sampler grep ------------------------------------------
def test_ac1_watchdog_has_no_get_history_in_refresh():
    """refresh_screen_fingerprints must contain NO backend.get_history call —
    pane_liveness.observe is the only liveness capture site (D1)."""
    src = Path(
        "src/cli_agent_orchestrator/services/stalled_callback_watchdog.py"
    ).read_text(encoding="utf-8")
    # Isolate the refresh_screen_fingerprints function body.
    start = src.index("def refresh_screen_fingerprints")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    assert "get_history" not in body


def test_ac1_pane_liveness_is_a_get_history_caller():
    src = Path("src/cli_agent_orchestrator/services/pane_liveness.py").read_text(encoding="utf-8")
    assert "get_history" in src


# ---- observe / peek basics ----------------------------------------------
def test_observe_none_when_capture_raises():
    pane = _pane()
    with patch.object(pane, "_capture", return_value=None):
        assert pane.observe("t1") is None
    assert pane.peek("t1") is None


def test_peek_none_before_first_sample():
    assert _pane().peek("never") is None


def test_peek_stale_after_10s():
    pane = _pane()
    with patch.object(pane, "_capture", return_value=("fp", "tail")):
        pane.observe("t1")
    assert pane.peek("t1") is not None
    pane._clock.advance(11.0)
    assert pane.peek("t1") is None  # stale > 10s constant


def test_debounce_counts_identical_samples():
    pane = _pane()
    monitor = MagicMock()
    monitor.get_published_status.return_value = TerminalStatus.PROCESSING
    with patch.object(pane, "_capture", return_value=("same", "tail")):
        pane.observe("t1", monitor=monitor)
        pane.observe("t1", monitor=monitor)
        obs = pane.observe("t1", monitor=monitor)
    assert obs.unchanged_count == 3
    assert obs.fp_changed is False


# ---- AC17 episode-free sampling -----------------------------------------
def test_ac17_observe_works_without_any_episode():
    """observe samples purely from metadata + backend — no episode needed."""
    pane = _pane()
    backend = MagicMock()
    backend.get_history.return_value = "pane content"
    meta = {"id": "t1", "tmux_session": "s", "tmux_window": "w"}
    with (
        patch("cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=meta),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=None,
        ),
    ):
        obs = pane.observe("t1")
    assert obs is not None
    assert obs.unchanged_count == 1


# ---- AC6: wedge readability without a second capture --------------------
def test_ac6_unchanged_for_s_readable_via_peek():
    pane = _pane()
    monitor = MagicMock()
    monitor.get_published_status.return_value = TerminalStatus.PROCESSING
    with patch.object(pane, "_capture", return_value=("frozen", "tail")):
        pane.observe("t1", monitor=monitor)
        pane._clock.advance(30.0)
        pane.observe("t1", monitor=monitor)
    obs = pane.peek("t1")  # no capture happens in peek
    assert obs is not None
    assert obs.unchanged_for_s == pytest.approx(30.0, abs=0.01)


# ---- capture pipeline applies liveness_exclude_patterns -----------------
def test_capture_filters_exclude_patterns():
    pane = _pane()
    backend = MagicMock()
    backend.get_history.return_value = "stable\nrotating prompt line\nmore"
    provider = MagicMock()
    provider.liveness_exclude_patterns = [r"rotating"]
    meta = {"id": "t1", "tmux_session": "s", "tmux_window": "w"}
    with (
        patch("cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=meta),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        result = pane._capture("t1")
    assert result is not None
    _fp, tail = result
    assert "rotating" not in tail
    assert "stable" in tail
