"""F792 (#649): a claude_code seat idle-waiting on background AGENTS is not busy.

The four acceptance criteria from the issue, plus three killed mutants:

* AC1 — a claude_code pane with the "Waiting for N background agent…" line and
  NO spinner classifies not-busy (idle/completed), never PROCESSING; the typed
  WAITING_ON_SUBAGENTS condition is detected and NEVER delivered to the seat
  inbox (drain-class decline, cut c/3).
* AC2 — the SAME pane with a live box spinner classifies PROCESSING.
* AC3 — hook-truth precedence: with a Stop-hook turn-end recorded, fuse_status'
  pane-delta rule cannot upgrade an idle/completed seat to PROCESSING.
* AC4 — the fleet STATUS cell (both TUIs) renders `· waiting`; the fleet payload
  carries the condition.

Mutants:
* drop the marker — SUBAGENT_WAIT_PATTERN never matches the agent line → the
  seat reads PROCESSING (the bug). Killed by AC1.
* invert the precedence — fuse_status ignores turn_ended → pane delta flips to
  PROCESSING. Killed by AC3.
* deliver the condition — WAITING_ON_SUBAGENTS maps inbox=True → it would be
  pushed to the seat. Killed by the delivery-decline arm of AC1.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.delivery_ledger import busy_class_declines_inbox
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import (
    BACKGROUND_WAIT_PATTERN,
    SUBAGENT_WAIT_PATTERN,
    ClaudeCodeProvider,
)
from cli_agent_orchestrator.providers.condition import (
    ConditionKind,
    classify_condition,
)
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor
from cli_agent_orchestrator.services.turn_state import TurnStateService
from cli_agent_orchestrator.tui.status_cell import status_cell

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "conditions" / "claude-code-waiting-on-subagents-1.txt"
)
_BOX = "─" * 40


def _p() -> ClaudeCodeProvider:
    return ClaudeCodeProvider("test792", "test-session", "window-0")


# ══ AC1: agent-wait pane is not-busy + condition detected + never delivered ══
def test_ac1_subagent_wait_pane_is_not_working():
    """A pane with the 'Waiting for N background agent…' line and no live spinner
    classifies NOT-busy (never PROCESSING)."""
    pane = _FIXTURE.read_text()
    status = _p().get_status(pane)
    assert status is not TerminalStatus.PROCESSING
    assert status in (TerminalStatus.COMPLETED, TerminalStatus.IDLE)


def test_ac1_subagent_wait_condition_detected_and_declines_inbox():
    """The typed WAITING_ON_SUBAGENTS condition is detected AND is never pushed
    to the supervisor inbox (drain-class decline — cut c/3)."""
    cond = classify_condition(_FIXTURE.read_text(), "claude_code")
    assert cond is not None
    assert cond.kind is ConditionKind.WAITING_ON_SUBAGENTS
    assert cond.subtype == "background_agents"
    # NEVER delivered to the seat inbox.
    assert busy_class_declines_inbox("WAITING_ON_SUBAGENTS") is True


def test_ac1_workflow_wait_still_processing_gh392():
    """GH #392 preserved: a 'dynamic workflow to finish' line stays PROCESSING —
    a backgrounded workflow keeps the seat's own turn open."""
    frame = (
        "● Workflow started in the background.\n"
        "✻ Waiting for 1 dynamic workflow to finish\n" + _BOX + "\n❯ \n" + _BOX + "\n"
    )
    assert _p().get_status(frame) is TerminalStatus.PROCESSING
    # And it is NOT the subagent-wait condition.
    assert classify_condition(frame, "claude_code") is None


def test_ac1_mutant_drop_marker_reads_processing():
    """MUTANT (drop the marker): if SUBAGENT_WAIT_PATTERN did not carve the agent
    line out of BACKGROUND_WAIT_PATTERN, the agent line would match the (old,
    unnarrowed) background-wait predicate and read PROCESSING — the bug. This
    pins the split: the agent line matches SUBAGENT, NOT BACKGROUND."""
    agent_line = "✻ Waiting for 1 background agent to finish"
    assert SUBAGENT_WAIT_PATTERN.search(agent_line) is not None
    assert BACKGROUND_WAIT_PATTERN.search(agent_line) is None  # narrowed away
    # the workflow line is the mirror: BACKGROUND yes, SUBAGENT no
    wf = "✻ Waiting for 1 dynamic workflow to finish"
    assert BACKGROUND_WAIT_PATTERN.search(wf) is not None
    assert SUBAGENT_WAIT_PATTERN.search(wf) is None


# ══ AC2: the same pane WITH a live spinner is PROCESSING ═════════════════════
def test_ac2_spinner_pane_is_processing():
    """A live box spinner (the seat's own turn) reads PROCESSING even with the
    agent-wait line elsewhere — a genuinely working seat is never mislabelled."""
    frame = "● Working on it.\n" + _BOX + "\n✻ Flambéing… (53s · ↓ 2.2k tokens)\n" + _BOX + "\n❯ \n"
    assert _p().get_status(frame) is TerminalStatus.PROCESSING


# ══ AC3: hook-truth precedence — turn_ended pins non-busy over pane delta ════
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _seed_churning_pane(pane_svc, terminal_id, monitor, published):
    """Drive observe twice with a CHANGING fingerprint (unchanged_count < K), so
    rule 3a is eligible and would return PROCESSING via pane_delta absent a veto.
    busy_marker=None (the neutral case that reaches the pane_delta arm)."""
    fps = iter(["a", "b"])

    def fake_capture(_tid):
        try:
            fp = next(fps)
        except StopIteration:
            fp = None
        if fp is None:
            return None
        return _CaptureResult(
            fingerprint=fp,
            filtered_tail="tail",
            busy_marker=None,
            children_count=0,
            marker_rows=(),
        )

    with (
        patch.object(pane_svc, "_capture", side_effect=fake_capture),
        patch.object(monitor, "get_published_status", return_value=published),
    ):
        for _ in range(2):
            pane_svc.observe(terminal_id, monitor=monitor)


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac3_turn_ended_pins_non_busy_over_pane_delta(mock_backend, monkeypatch):
    """With a Stop-hook turn-end recorded, the pane-delta rule cannot upgrade an
    idle/completed seat to PROCESSING — it admits the published status tagged
    ``turn_ended`` (AC3)."""
    mock_backend.return_value = MagicMock()
    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    turn = TurnStateService(_clock=clock)
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.turn_state as tsmod

    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(tsmod, "turn_state", turn)

    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    turn.mark("t1", "turn_ended")  # the Stop hook fired
    _seed_churning_pane(pane, "t1", sm, TerminalStatus.COMPLETED)

    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.COMPLETED  # NOT upgraded to PROCESSING
    assert obs.fusion_reason == "turn_ended"


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac3_mutant_invert_precedence_flips_to_processing(mock_backend, monkeypatch):
    """MUTANT (invert the precedence): with NO turn-end recorded, the same
    churning pane DOES upgrade to PROCESSING via pane_delta — proving the veto in
    the real test is what holds the line, not an unrelated no-op."""
    mock_backend.return_value = MagicMock()
    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    turn = TurnStateService(_clock=clock)  # empty: is_turn_ended -> False
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.turn_state as tsmod

    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(tsmod, "turn_state", turn)

    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    _seed_churning_pane(pane, "t1", sm, TerminalStatus.COMPLETED)

    obs = sm.get_boundary_observation("t1")
    assert obs.status is TerminalStatus.PROCESSING  # no veto → the defect
    assert obs.fusion_reason == "pane_delta"


# ══ AC4: both TUIs render `· waiting`; the payload carries the condition ═════
def test_ac4_status_cell_renders_waiting():
    """The fleet STATUS cell (tui/status_cell.py) renders `· waiting` for a seat
    carrying the WAITING_ON_SUBAGENTS condition — calm, not a `⚠` headline."""
    cell = status_cell({"status": "completed", "condition": "WAITING_ON_SUBAGENTS"})
    assert cell.plain == "· waiting"
    assert "⚠" not in cell.plain


def test_ac4_cli_fleet_render_waiting():
    """The CLI `cao agents` fleet listing renders `· waiting` in the STATUS
    cell for a seat carrying the condition (the OTHER TUI)."""
    from cli_agent_orchestrator.cli.commands import agents as ag

    row = {
        "window_index": 0,
        "id": "t792",
        "profile": "chao_supervisor",
        "status": "completed",
        "condition": "WAITING_ON_SUBAGENTS",
        "delegating": False,
        "children_count": 0,
        "fusion_changed": False,
        "orphan": False,
        "reparented_from": None,
        "parent_id": None,
        "window_name": "w",
        "since_last_input": 3.0,
    }
    captured = []
    with patch.object(
        ag.click, "echo", side_effect=lambda *a, **k: captured.append(a[0] if a else "")
    ):
        ag._render_fleet({"session_name": "s", "terminals": [row]})
    assert any("· waiting" in line for line in captured)


def test_ac4_mutant_deliver_condition_would_push_to_seat():
    """MUTANT (deliver the condition): if WAITING_ON_SUBAGENTS were mapped
    inbox=True it would be pushed to the seat. The real map declines it; this
    pins that the decline predicate is what keeps it off the seat."""
    from cli_agent_orchestrator.clients.delivery_ledger import surfaces_for_kind

    surf = surfaces_for_kind("WAITING_ON_SUBAGENTS")
    assert surf.inbox is False  # real: declined
    # A mutant map entry with inbox=True would make busy_class_declines_inbox
    # False, re-opening the seat push:
    assert busy_class_declines_inbox("WAITING_ON_SUBAGENTS") is (
        surf.fleet and surf.bus and not surf.inbox
    )
