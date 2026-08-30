"""F506 pane sampler unit tests (AC1, AC6, AC17, AC20, AC22 mechanics)."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult


def _cap(fp="fp", tail="tail", *, busy_marker=None, children_count=0, marker_rows=()):
    """Build a _CaptureResult the way the (F568) _capture contract now returns."""
    return _CaptureResult(
        fingerprint=fp,
        filtered_tail=tail,
        busy_marker=busy_marker,
        children_count=children_count,
        marker_rows=marker_rows,
    )


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
    src = Path("src/cli_agent_orchestrator/services/stalled_callback_watchdog.py").read_text(
        encoding="utf-8"
    )
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
    with patch.object(pane, "_capture", return_value=_cap()):
        pane.observe("t1")
    assert pane.peek("t1") is not None
    pane._clock.advance(11.0)
    assert pane.peek("t1") is None  # stale > 10s constant


def test_peek_returns_the_retained_filtered_tail_without_capturing():
    pane = _pane()
    monitor = MagicMock()
    monitor.get_published_status.return_value = TerminalStatus.PROCESSING
    with patch.object(pane, "_capture", return_value=_cap("fp", "retained pane tail")) as capture:
        pane.observe("t1", monitor=monitor)
    capture.reset_mock()

    observation = pane.peek("t1")

    assert observation is not None
    assert observation.filtered_tail == "retained pane tail"
    capture.assert_not_called()


def test_debounce_counts_identical_samples():
    pane = _pane()
    monitor = MagicMock()
    monitor.get_published_status.return_value = TerminalStatus.PROCESSING
    with patch.object(pane, "_capture", return_value=_cap("same")):
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
    with patch.object(pane, "_capture", return_value=_cap("frozen")):
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
    tail = result.filtered_tail
    assert "rotating" not in tail
    assert "stable" in tail


# =====================================================================
# F568 #425 D12d — sampled facts, veto SET-edge, AC-7 episode clock
# =====================================================================


def _observe_with(pane, tid, monitor, *, fp, busy_marker, children_count=0, marker_rows=()):
    """Drive one observe() with a controlled _CaptureResult."""
    with patch.object(
        pane,
        "_capture",
        return_value=_cap(
            fp, busy_marker=busy_marker, children_count=children_count, marker_rows=marker_rows
        ),
    ):
        return pane.observe(tid, monitor=monitor)


def _mon(published):
    m = MagicMock()
    m.get_published_status.return_value = published
    return m


# ---- sampled facts land on the observation (AC-6 side) ------------------
def test_sampled_facts_exposed_on_observation():
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    obs = _observe_with(
        pane,
        "t1",
        mon,
        fp="a",
        busy_marker=False,
        children_count=2,
        marker_rows=("row1", "row2"),
    )
    assert obs is not None
    assert obs.busy_marker is False
    assert obs.children_count == 2
    assert obs.marker_rows == ("row1", "row2")
    # peek exposes the SAME stored facts without capturing.
    peeked = pane.peek("t1")
    assert peeked.busy_marker is False
    assert peeked.children_count == 2
    assert peeked.marker_rows == ("row1", "row2")


# ---- veto SET-edge: children>0 and busy_marker False both suppress -------
def test_children_veto_clears_hold_bound():
    """children>0 ⇒ rule 3a does NOT withhold ⇒ downgrade_since stays cleared."""
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    for i in range(3):
        _observe_with(pane, "t1", mon, fp=str(i), busy_marker=None, children_count=1)
        pane._clock.advance(5.0)
    assert pane._state["t1"].downgrade_since is None
    assert pane._state["t1"].pane_hold_expired is False


def test_marker_veto_clears_hold_bound():
    """busy_marker is False ⇒ rule 3a does NOT withhold ⇒ bound stays cleared."""
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    for i in range(3):
        _observe_with(pane, "t1", mon, fp=str(i), busy_marker=False, children_count=0)
        pane._clock.advance(5.0)
    assert pane._state["t1"].downgrade_since is None
    assert pane._state["t1"].pane_hold_expired is False


def test_busy_marker_true_still_arms_hold_bound():
    """busy_marker True (spinner live) does NOT veto — rule 3a withholds, so the
    hold bound arms exactly as legacy."""
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    _observe_with(pane, "t1", mon, fp="a", busy_marker=True, children_count=0)
    assert pane._state["t1"].downgrade_since is not None


# ---- AC-7 veto-defect episode clock (simulated time) --------------------
def _veto_sample(pane, tid, mon, fp, warns, marker_rows=()):
    with (
        patch.object(
            pane,
            "_capture",
            return_value=_cap(fp, busy_marker=False, children_count=0, marker_rows=marker_rows),
        ),
        patch("cli_agent_orchestrator.services.pane_liveness.logger") as log,
    ):
        log.warning.side_effect = lambda *a, **k: warns.append(a)
        return pane.observe(tid, monitor=mon)


def test_ac7_open_edge_warns_once():
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    # fp_changed + IDLE + no marker + children==0 + busy_marker False = open edge.
    _veto_sample(pane, "t1", mon, "a", warns, marker_rows=("diag",))
    assert len(warns) == 1
    assert pane._state["t1"].veto_episode_open is True


def test_ac7_no_repeat_within_same_episode():
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    _veto_sample(pane, "t1", mon, "a", warns)  # open
    pane._clock.advance(1.0)
    _veto_sample(pane, "t1", mon, "b", warns)  # same episode, still veto
    pane._clock.advance(1.0)
    _veto_sample(pane, "t1", mon, "c", warns)
    assert len(warns) == 1  # only the open edge warned


def test_ac7_close_then_reopen_within_bound_one_line_total():
    """Open, close (a non-veto sample), reopen WITHIN the bound ⇒ the
    cross-episode limiter suppresses the second WARN ⇒ one line total."""
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    _veto_sample(pane, "t1", mon, "a", warns)  # open + warn (1)
    pane._clock.advance(10.0)
    # Close edge: a spinner-live sample (busy_marker True) — not a veto.
    _observe_with(pane, "t1", mon, fp="b", busy_marker=True)
    assert pane._state["t1"].veto_episode_open is False
    pane._clock.advance(10.0)
    _veto_sample(pane, "t1", mon, "c", warns)  # reopen within bound => limiter
    assert len(warns) == 1


def test_ac7_reopen_after_bound_two_lines():
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    _veto_sample(pane, "t1", mon, "a", warns)  # open + warn (1)
    # Close edge.
    _observe_with(pane, "t1", mon, fp="b", busy_marker=True)
    pane._clock.advance(301.0)  # past pane_delta_max_hold_s
    _veto_sample(pane, "t1", mon, "c", warns)  # reopen after bound => warn (2)
    assert len(warns) == 2


def test_ac7_children_present_never_opens_episode():
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    with (
        patch.object(pane, "_capture", return_value=_cap("a", busy_marker=False, children_count=1)),
        patch("cli_agent_orchestrator.services.pane_liveness.logger") as log,
    ):
        log.warning.side_effect = lambda *a, **k: warns.append(a)
        pane.observe("t1", monitor=mon)
    assert warns == []
    assert pane._state["t1"].veto_episode_open is False


def test_ac7_open_marker_never_opens_episode():
    import cli_agent_orchestrator.services.question_state as qs

    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    fake_q = MagicMock()
    fake_q.is_open.return_value = True
    with (
        patch.object(qs, "question_state", fake_q),
        patch.object(pane, "_capture", return_value=_cap("a", busy_marker=False, children_count=0)),
        patch("cli_agent_orchestrator.services.pane_liveness.logger") as log,
    ):
        log.warning.side_effect = lambda *a, **k: warns.append(a)
        pane.observe("t1", monitor=mon)
    assert warns == []
    assert pane._state["t1"].veto_episode_open is False


def test_ac7_redaction_row_truncated_to_120_printable():
    """A 400-char row with escape bytes is emitted <=120 printable chars.

    Exercises the _capture marker_rows sanitiser directly (the real path that
    populates the diagnostic), independent of the box anchoring.
    """
    from cli_agent_orchestrator.services.pane_liveness import _sanitize_marker_row

    raw = ("A" * 400) + "\x1b[31m" + ("B" * 50)
    out = _sanitize_marker_row(raw)
    assert len(out) <= 120
    assert "\x1b" not in out
    assert all(c.isprintable() for c in out)


def test_ac7_non_usable_sample_neither_opens_nor_closes():
    """A capture outage (observe -> None) must not touch the veto episode."""
    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    warns = []
    _veto_sample(pane, "t1", mon, "a", warns)  # open
    assert pane._state["t1"].veto_episode_open is True
    with patch.object(pane, "_capture", return_value=None):
        assert pane.observe("t1", monitor=mon) is None
    # Episode state untouched by the non-usable sample.
    assert pane._state["t1"].veto_episode_open is True


# ---- AC-F568-6 end-to-end veto from the committed idle-churn fixture -----
def test_ac6_idle_subagent_churn_fixture_samples_busy_marker_false():
    """Replay the byte-exact idle-subagent-churn fixture through the REAL
    _capture path (real ClaudeCodeProvider.rule3a_busy_marker): busy_marker is
    sampled False, so rule 3a never withholds — downgrade_since stays None even
    across churn spanning > 2 * pane_delta_max_hold_s."""
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    fixture = (
        Path(__file__).parent.parent
        / "providers"
        / "fixtures"
        / "f568"
        / "idle-subagent-churn-a.txt"
    ).read_text(encoding="utf-8")

    pane = _pane()
    mon = _mon(TerminalStatus.IDLE)
    provider = ClaudeCodeProvider("t1", "s", "w")
    backend = MagicMock()
    # Append a per-tick nonce so the fingerprint changes every sample (churn)
    # while the spinner-bearing region above the composer stays absent.
    meta = {"id": "t1", "tmux_session": "s", "tmux_window": "w"}

    counter = {"n": 0}

    def churning_history(*_a, **_k):
        counter["n"] += 1
        return fixture + f"\n<scroll {counter['n']}>"

    backend.get_history.side_effect = churning_history

    with (
        patch("cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=meta),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        for _ in range(130):  # > 2 * 300s / 5s tick
            obs = pane.observe("t1", monitor=mon)
            assert obs is not None
            assert obs.busy_marker is False  # sampled from the real provider
            pane._clock.advance(5.0)

    st = pane._state["t1"]
    assert st.downgrade_since is None
    assert st.pane_hold_expired is False
