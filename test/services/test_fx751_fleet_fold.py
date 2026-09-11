"""fx751 Slice A (AC-7, option B): fleet overlay + condition fold into publication.

The invariant AC-7 protects: ONE accepted observation ID, no post-hoc overlay,
no separate condition read. Option B delivers that at the fleet seam —
overlaid_observation mints/reuses the one observation_epoch and folds the three
ERROR overlays + the single condition read into one BoundaryObservation.

Constraint tests (per supervisor decision):
  (1) the observation_epoch is minted once and identical across the consumers
      that read it for the same tick (fleet, API, MCP fleet, TUI all cite the
      accepted observation via get_boundary_observation/overlaid_observation);
  (2) the fold does NOT change what the delivery-gating consumers see — they
      read get_boundary_observation (the UN-overlaid observation) exactly as
      before; overlaid_observation is a fleet-egress projection, not their input.
"""

from __future__ import annotations

from unittest.mock import patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def test_ac7_overlaid_reuses_the_one_observation_epoch():
    """The overlaid observation carries the SAME observation_epoch as the base
    accepted observation — the one ID every consumer cites for the tick."""
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.IDLE
    base = sm.get_boundary_observation("t1")
    overlaid = sm.overlaid_observation("t1")
    # same accepted observation ID across the fleet-egress projection and the
    # base observation the API / MCP / TUI read.
    assert overlaid.observation_epoch == base.observation_epoch
    # a second read in the same tick is still the same id (minted once).
    assert sm.get_boundary_observation("t1").observation_epoch == base.observation_epoch


def test_ac7_recovery_overlay_folds_error_and_names_it():
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    obs = sm.overlaid_observation("t1", recovery_override=True)
    assert obs.status is TerminalStatus.ERROR
    assert obs.health_overlay == "recovery_state"


def test_ac7_window_absent_overlay_folds_error():
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    obs = sm.overlaid_observation("t1", window_absent=True)
    assert obs.status is TerminalStatus.ERROR
    assert obs.health_overlay == "window_absent"


def test_ac7_init_health_and_terminal_error_fold_error():
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    obs = sm.overlaid_observation("t1", terminal_error="deferred_init_internal")
    assert obs.status is TerminalStatus.ERROR
    assert obs.health_overlay == "init_health_failed"
    assert obs.terminal_error == "deferred_init_internal"


def test_ac7_no_overlay_preserves_base_status_and_folds_condition():
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.IDLE
    with patch.object(sm, "get_condition", return_value="capped") as gc:
        obs = sm.overlaid_observation("t1")
    assert obs.status is TerminalStatus.IDLE
    assert obs.condition == "capped"
    # the condition is read ONCE, with the overlaid status (F752)
    gc.assert_called_once_with("t1", TerminalStatus.IDLE)


def test_ac7_condition_read_uses_overlaid_status_not_base():
    """When an overlay forces ERROR, the single folded condition read must use
    the OVERLAID status (ERROR), not the base status — a BUSY-class label must
    not ride the overlaid row (F752). Pins that the fold reads condition against
    the final status."""
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    with patch.object(sm, "get_condition", return_value=None) as gc:
        sm.overlaid_observation("t1", recovery_override=True)
    gc.assert_called_once_with("t1", TerminalStatus.ERROR)


def test_ac7_overlay_precedence_matches_legacy_order():
    """recovery > window-absent > init-health, each forcing ERROR — the same
    order fleet applied post-hoc before the fold."""
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    # recovery wins over the others
    obs = sm.overlaid_observation(
        "t1", recovery_override=True, window_absent=True, terminal_error="x"
    )
    assert obs.health_overlay == "recovery_state"


def test_ac7_delivery_gating_consumers_see_unoverlaid_observation():
    """AC-7 constraint (2): the delivery-gating consumers (watchdog gate,
    inbox_service) read get_boundary_observation — the UN-overlaid observation —
    which is unchanged by the fold. A recovery/window/init overlay that would
    force ERROR at the FLEET egress must NOT appear on the base observation
    those consumers read."""
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    base = sm.get_boundary_observation("t1")
    # the fleet egress overlays to ERROR ...
    overlaid = sm.overlaid_observation("t1", recovery_override=True)
    assert overlaid.status is TerminalStatus.ERROR
    # ... but the base observation the delivery gate reads is untouched.
    base_again = sm.get_boundary_observation("t1")
    assert base_again.status is TerminalStatus.COMPLETED
    assert base_again.status is base.status
    assert base_again.health_overlay is None
    assert base_again.condition is None
