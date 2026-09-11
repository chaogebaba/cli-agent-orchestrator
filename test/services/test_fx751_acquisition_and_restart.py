"""fx751 Slice A — AC-6 (acquisition cardinality), AC-9 (restart), AC-10 (parity).

AC-6: repeatedly reading a published snapshot triggers ZERO captures, ZERO
process scans and ZERO re-derivations on a migrated lane — the single scheduled
sampler owns capture, getters never do.

AC-9: a fresh StatusMonitor (a server restart) starts every terminal UNKNOWN
and resamples; no cached readiness / pre-restart PROCESSING survives in-process.

AC-10: the migration registry and provider flags agree, and legacy providers
are explicitly marked unmigrated (no shadow flag).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import status_contract as sc
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    return pane, clock


def _migrated_provider():
    p = MagicMock()
    p.fx751_status_migrated = True
    return p


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac6_repeated_reads_trigger_zero_captures(mock_backend, _wire):
    """N getter reads => zero pane captures and zero re-derivations. peek()
    reads the retained tail; a capture is a real subprocess fork the getter
    must never do."""
    mock_backend.return_value = MagicMock()
    pane, _clock = _wire
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING

    with (
        patch.object(pane, "_capture") as capture,
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=_migrated_provider(),
        ),
        patch.object(sm, "_rederive_from_pane_sample", return_value=None) as rederive,
    ):
        for _ in range(25):
            sm.get_boundary_observation("t1")

    assert capture.call_count == 0  # AC-6: zero captures across N reads
    # re-derivation is rate-limited AND capture-free; but even so, a getter must
    # not force one — the migrated fusion only consults the RETAINED peek. When
    # peek() is None (no sample seeded here) the rederive branch is not entered.
    assert rederive.call_count == 0


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac9_fresh_monitor_starts_unknown(mock_backend, _wire):
    """A server restart is a fresh StatusMonitor: no _last_status, so every
    terminal reads UNKNOWN — no cached readiness or pre-restart PROCESSING."""
    mock_backend.return_value = MagicMock()
    sm = StatusMonitor()
    obs = sm.get_boundary_observation("never-seen")
    assert obs.status is TerminalStatus.UNKNOWN


def test_ac9_restart_does_not_retain_reducer_context():
    """The reducer context lives in-process (D-A1); a fresh monitor holds none,
    so a pre-restart PROCESSING claim cannot be resurrected as current truth."""
    sm1 = StatusMonitor()
    sm1._fx751_reducer_ctx["t1"] = sc.ReducerContext(
        terminal_id="t1", last_status=TerminalStatus.PROCESSING
    )
    # a "restart" is a brand-new instance
    sm2 = StatusMonitor()
    assert "t1" not in sm2._fx751_reducer_ctx
    assert sm2.fx751_reducer_context("t1").last_status is TerminalStatus.UNKNOWN


def test_ac10_registry_and_flags_agree_and_no_shadow_flag():
    from cli_agent_orchestrator.providers.base import BaseProvider
    from cli_agent_orchestrator.providers.codex import CodexProvider
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider

    # migrated providers are explicit in both the registry and the class flag
    assert sc.is_migrated("pi_cli") and PiCliProvider.fx751_status_migrated
    assert sc.is_migrated("codex") and CodexProvider.fx751_status_migrated
    # unmigrated providers keep their legacy adapter, explicitly not migrated
    for legacy in ("grok_cli", "claude_code", "kiro_cli"):
        assert not sc.is_migrated(legacy)
    # default is unmigrated — a new provider is never silently switched
    assert BaseProvider.fx751_status_migrated is False
    assert sc.is_migrated("some_unknown_provider") is False
    # no shadow flag/phase introduced (F883 #738): the registry is a 2-value
    # MigrationState, not a tri-state with a shadow mode.
    assert set(sc.MigrationState) == {sc.MigrationState.MIGRATED, sc.MigrationState.LEGACY}
