"""F506 §1 regression at the admission seam (AC5, AC13, AC18).

deliver_pending admits on the value view_from_legacy returns for
"delivery.admission_status". These tests pin that seam directly with the REAL
status_monitor + question_state + pane_liveness (fusion live), which is what
proves the §1 shape: a marker-open, byte-stable claude pane resolves to
WAITING_USER_ANSWER at the admission seam (so delivery withholds), and with the
marker absent the same inputs resolve to the published IDLE (so delivery would
proceed) — the executable F507-before-F506 dependency.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import receiver_state_view
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def wired(monkeypatch):
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    question = QuestionStateService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", question)
    sm = StatusMonitor()
    return sm, pane, question, clock


def _seed_stable_pane(pane, sm, terminal_id, published, k=5):
    """K identical samples — the byte-stable dialog the §1 bug relies on."""
    with (
        patch.object(pane, "_capture", return_value=_CaptureResult("stable", "tail", None, 0, ())),
        patch.object(sm, "get_published_status", return_value=published),
    ):
        for _ in range(k):
            pane.observe(terminal_id, monitor=sm)


def test_ac5_marker_open_bytestable_resolves_waiting(wired):
    """§1: claude buffer misses the WAITING regex (published IDLE), pane byte-
    stable for K+ samples, marker OPEN ⇒ admission seam yields WAITING."""
    sm, pane, question, _clock = wired
    sm._last_status["t1"] = TerminalStatus.IDLE
    _seed_stable_pane(pane, sm, "t1", TerminalStatus.IDLE)
    question.push_marker("t1", "question_open")

    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.WAITING_USER_ANSWER
    assert obs.fusion_reason == "question_marker"

    # The seam admission reads (receiver-state inactive -> get_status path).
    with patch.object(receiver_state_view, "_event_inbox_bypass", return_value=True):
        seam = receiver_state_view.view_from_legacy(
            "delivery.admission_status",
            "t1",
            obs.status,
            max_age_s=5.0,
            none_behavior="none",
            monitor=sm,
        )
    # bypass returns the (already-fused) legacy answer -> WAITING -> withhold.
    assert seam is TerminalStatus.WAITING_USER_ANSWER


def test_ac5_marker_absent_same_fixture_resolves_idle(wired):
    """Same fixture, marker ABSENT ⇒ the seam yields the published IDLE — the
    F507-before-F506 dependency as an executable assertion."""
    sm, pane, _question, _clock = wired
    sm._last_status["t1"] = TerminalStatus.IDLE
    # Byte-stable at K+ so rule 3a's debounce is satisfied (would not downgrade),
    # published IDLE, no marker -> stays IDLE.
    _seed_stable_pane(pane, sm, "t1", TerminalStatus.IDLE)

    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.IDLE
    assert obs.fusion_reason is None


def test_ac13_force_status_waiting_not_cleared_by_stable_pane(wired):
    """AC13: a provider/auto-responder WAITING is additive-only — a byte-stable
    pane must NOT clear it (D5, the auto_responder.force_status site)."""
    sm, pane, question, _clock = wired
    sm._last_status["t1"] = TerminalStatus.WAITING_USER_ANSWER
    _seed_stable_pane(pane, sm, "t1", TerminalStatus.WAITING_USER_ANSWER)
    assert question.is_open("t1") is False

    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.WAITING_USER_ANSWER


def test_ac18_parity_zero_mismatch_on_fusion_only_difference(wired):
    """AC18: raw IDLE, fused PROCESSING, receiver-state IDLE ⇒ ZERO parity
    mismatch for delivery.admission_status (both arms fused = same-sided)."""
    sm, pane, _question, _clock = wired
    sm._last_status["t1"] = TerminalStatus.IDLE
    # fp changed last sample -> rule 3a downgrades IDLE -> PROCESSING (fused).
    with (
        patch.object(
            pane,
            "_capture",
            side_effect=[
                _CaptureResult("a", "t", None, 0, ()),
                _CaptureResult("b", "t", None, 0, ()),
            ],
        ),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.IDLE),
    ):
        pane.observe("t1", monitor=sm)
        pane.observe("t1", monitor=sm)

    # Receiver-state store answers IDLE (unfused source); resolve_rs_answer fuses
    # it the same way the legacy arm is fused, so record_comparison sees
    # fused-vs-fused = no mismatch.
    store = MagicMock()
    from types import SimpleNamespace

    store.snapshot_view.return_value = SimpleNamespace(
        latched_status=TerminalStatus.IDLE, origin="incremental"
    )
    sm._receiver_state_store = store  # type: ignore[attr-defined]

    recorded = []

    class _State:
        phase = "collecting"

    with (
        patch.object(receiver_state_view, "_event_inbox_bypass", return_value=False),
        patch.object(receiver_state_view, "_event_inbox_comparator_bypass", return_value=False),
        patch("cli_agent_orchestrator.services.seam_parity.parity_state", return_value=_State()),
        patch(
            "cli_agent_orchestrator.services.seam_parity.record_comparison",
            side_effect=lambda *a, **k: recorded.append((a, k)),
        ),
        patch(
            "cli_agent_orchestrator.services.receiver_state_view.get_terminal_metadata",
            return_value={"id": "t1", "lifecycle_generation": 1, "tmux_window": "w"},
        ),
    ):
        # legacy arm is the fused boundary status (PROCESSING).
        legacy = sm.get_boundary_observation("t1").status
        receiver_state_view.view_from_legacy(
            "delivery.admission_status",
            "t1",
            legacy,
            max_age_s=5.0,
            none_behavior="none",
            monitor=sm,
        )
    assert recorded, "record_comparison should have been called in collecting phase"
    args, kwargs = recorded[0]
    # collecting records (legacy_answer, rs.answer); both fused to PROCESSING.
    legacy_answer, rs_answer = args[2], args[3]
    assert legacy_answer is TerminalStatus.PROCESSING
    assert rs_answer is TerminalStatus.PROCESSING  # fused same-sided => no mismatch
