"""F506 read-time status fusion (AC3, AC4, AC16-AC22).

These pin fuse_status and its wiring into get_boundary_observation /
get_raw_status. The pane sampler and question_state singletons are driven
directly (their own units are tested elsewhere); here we assert the fusion
rules, the two BoundaryObservation fields, idempotence, and the pane-hold bound.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture(autouse=True)
def _wire_singletons(monkeypatch):
    """Point the module singletons fuse_status reads at fresh, test-owned ones."""
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    question = QuestionStateService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", question)
    return pane, question, clock


def _backend(event_inbox=False):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    return backend


def _seed_pane(pane, terminal_id, monitor, published, *, fingerprints, now_start=1000.0):
    """Drive `observe` N times with a controlled capture + published status."""
    captured = iter(fingerprints)

    def fake_capture(_tid):
        try:
            fp = next(captured)
        except StopIteration:
            fp = None
        return (fp, "tail") if fp is not None else None

    with (
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(monitor, "get_published_status", return_value=published),
    ):
        results = []
        for _ in fingerprints:
            results.append(pane.observe(terminal_id, monitor=monitor))
    return results


# ---- AC3 / AC4 -----------------------------------------------------------
@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac3_liveness_downgrade_fires(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    pane, _question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    # fp changed last sample (unchanged_count=1 < K=3), published COMPLETED.
    _seed_pane(pane, "t1", sm, TerminalStatus.COMPLETED, fingerprints=["a", "b"])
    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.PROCESSING
    assert obs.fusion_reason == "pane_delta"
    assert obs.fusion_changed is True


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac4_liveness_never_promotes(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    pane, _question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    # fp frozen for 100 samples: unchanged_count >> K, so rule 3a does NOT fire.
    _seed_pane(pane, "t1", sm, TerminalStatus.PROCESSING, fingerprints=["x"] * 100)
    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.PROCESSING
    assert obs.fusion_reason is None
    assert obs.fusion_changed is False


# ---- AC20 no-evidence ----------------------------------------------------
@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac20a_before_first_sample_no_downgrade(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    _pane, _question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    # No sample ever taken -> peek None -> rule 3a cannot fire.
    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.COMPLETED
    assert obs.fusion_reason is None
    assert sm.get_raw_status("t1") is TerminalStatus.COMPLETED


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac20c_marker_raise_is_sampler_independent(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    _pane, question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    question.push_marker("t1", "question_open")  # no pane sample at all
    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.WAITING_USER_ANSWER
    assert obs.fusion_reason == "question_marker"


# ---- rule 1 additive-only on WAITING (D5) --------------------------------
@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_rule1_waiting_never_lowered_by_pane(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    pane, _question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.WAITING_USER_ANSWER
    # A byte-stable pane satisfies K but must NOT clear WAITING.
    _seed_pane(pane, "t1", sm, TerminalStatus.WAITING_USER_ANSWER, fingerprints=["s"] * 5)
    status, reason = sm.fuse_status("t1", TerminalStatus.WAITING_USER_ANSWER)
    assert status is TerminalStatus.WAITING_USER_ANSWER
    assert reason is None  # no marker open


# ---- AC19 publish-time fields untouched ----------------------------------
@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac19_fusion_leaves_publish_time_fields(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    pane, _question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    sm._status_gen["t1"] = 7
    sm._last_ready_seq["t1"] = 3
    sm._last_non_ready_seq["t1"] = 2
    _seed_pane(pane, "t1", sm, TerminalStatus.COMPLETED, fingerprints=["a", "b"])
    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.PROCESSING  # fused
    assert obs.status_gen == 7
    assert obs.last_ready_seq == 3
    assert obs.last_non_ready_seq == 2


# ---- AC21 idempotence ----------------------------------------------------
@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac21_idempotence_status_and_reason(mock_backend, _wire_singletons):
    mock_backend.return_value = _backend()
    pane, _question, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    _seed_pane(pane, "t1", sm, TerminalStatus.COMPLETED, fingerprints=["a", "b"])
    first_status, first_reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)
    # Fuse the already-fused value: status AND reason re-derive identically.
    second_status, second_reason = sm.fuse_status("t1", first_status)
    assert first_status is TerminalStatus.PROCESSING
    assert second_status is TerminalStatus.PROCESSING
    assert first_reason == second_reason == "pane_delta"


def test_none_passthrough(_wire_singletons):
    sm = StatusMonitor()
    assert sm.fuse_status("t1", None) == (None, None)


# ---- AC22 pane-hold bound ------------------------------------------------
def test_ac22_hold_bound_expires_once_then_admits(_wire_singletons):
    pane, _question, clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED

    captured = {"fp": 0}

    def fake_capture(_tid):
        captured["fp"] += 1
        return (str(captured["fp"]), "tail")  # churns every sample

    warns = []
    with (
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.COMPLETED),
        patch("cli_agent_orchestrator.services.pane_liveness.logger") as log,
    ):
        log.warning.side_effect = lambda *a, **k: warns.append(a)
        # Before the bound: rule 3a downgrades (churning, published COMPLETED).
        pane.observe("t1", monitor=sm)
        status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)
        assert status is TerminalStatus.PROCESSING and reason == "pane_delta"
        # Advance past the 300s bound and sample again — flips pane_hold_expired.
        clock.advance(301.0)
        pane.observe("t1", monitor=sm)
        assert len(warns) == 1  # exactly one WARN
        status2, reason2 = sm.fuse_status("t1", TerminalStatus.COMPLETED)
        assert status2 is TerminalStatus.COMPLETED  # admitted
        assert reason2 == "pane_delta_expired"


def test_ac22_arm_c_processing_never_arms_bound(_wire_singletons):
    pane, _question, clock = _wire_singletons
    sm = StatusMonitor()

    captured = {"fp": 0}

    def fake_capture(_tid):
        captured["fp"] += 1
        return (str(captured["fp"]), "tail")  # churns every sample

    warns = []
    with (
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.PROCESSING),
        patch("cli_agent_orchestrator.services.pane_liveness.logger") as log,
    ):
        log.warning.side_effect = lambda *a, **k: warns.append(a)
        for _ in range(100):
            pane.observe("t1", monitor=sm)
            clock.advance(5.0)  # spans >300s of simulated time
        assert warns == []  # published PROCESSING never arms the bound
        state = pane._state["t1"]
        assert state.downgrade_since is None
        assert state.pane_hold_expired is False
