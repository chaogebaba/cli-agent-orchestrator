"""fx751 Slice A (AC-5a/AC-5b): the fusion migration gate.

A MIGRATED provider (pi_cli, codex) is decided by the typed reducer from the
fresh pane sample and NEVER reaches the legacy pane-delta arms — so it never
carries a ``pane_delta*`` reason nor a ``child_proc_live`` return (AC-5a). An
UNMIGRATED provider still reaches those arms unchanged (AC-5b), and the
expired-admit code is NOT deleted (this slice; AC-21 owns removal).

These tests drive the REAL ``StatusMonitor.fuse_status`` with a controlled
provider and pane sample — no stub returning a canned status.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _wire_singletons(monkeypatch):
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    question = QuestionStateService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", question)
    return pane, question, clock


def _fake_provider(migrated: bool) -> MagicMock:
    p = MagicMock()
    p.fx751_status_migrated = migrated
    return p


def _seed_pane(pane, monitor, terminal_id, published, *, fingerprints, expired=False):
    """Drive observe() so peek() returns a usable, unstable (churning) sample."""
    captured = iter(fingerprints)

    def fake_capture(_tid):
        try:
            fp = next(captured)
        except StopIteration:
            return None
        if fp is None:
            return None
        return _CaptureResult(
            fingerprint=fp,
            filtered_tail="tail-text",
            busy_marker=None,
            children_count=0,
            marker_rows=(),
        )

    with (
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(monitor, "get_published_status", return_value=published),
    ):
        for _ in fingerprints:
            pane.observe(terminal_id, monitor=monitor)


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5a_migrated_provider_never_carries_pane_delta_reason(mock_backend, _wire_singletons):
    """AC-5a: a migrated provider whose fresh sample re-derives PROCESSING
    holds PROCESSING under the fx751 reason — never ``pane_delta`` and never
    ``child_proc_live``."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    _seed_pane(pane, sm, "t1", TerminalStatus.COMPLETED, fingerprints=["a", "b"])

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=_fake_provider(migrated=True),
        ),
        patch.object(sm, "_rederive_from_pane_sample", return_value=TerminalStatus.PROCESSING),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)

    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_working"
    assert reason is not None and "pane_delta" not in reason
    assert reason != "child_proc_live"


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5a_migrated_no_fresh_evidence_holds_published(mock_backend, _wire_singletons):
    """AC-5a: with no fresh lowering evidence, a migrated lane HOLDS the
    published status under ``fx751_migrated`` — it never lowers into idle on a
    stale buffer and never reaches the expired-admit arm."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    _seed_pane(pane, sm, "t1", TerminalStatus.PROCESSING, fingerprints=["a", "b"])

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=_fake_provider(migrated=True),
        ),
        patch.object(sm, "_rederive_from_pane_sample", return_value=None),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_migrated"
    assert reason is not None and "pane_delta" not in reason


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5a_migrated_fresh_idle_admits_reducer_verdict(mock_backend, _wire_singletons):
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    _seed_pane(pane, sm, "t1", TerminalStatus.PROCESSING, fingerprints=["a", "b"])

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=_fake_provider(migrated=True),
        ),
        patch.object(sm, "_rederive_from_pane_sample", return_value=TerminalStatus.IDLE),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    assert status is TerminalStatus.IDLE
    assert reason == "fx751_reducer"


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5b_unmigrated_provider_still_reaches_pane_delta_arm(mock_backend, _wire_singletons):
    """AC-5b: an unmigrated provider still reaches the legacy pane-delta arm —
    a churning pane holds PROCESSING under ``pane_delta``. Proves the gate did
    NOT short-circuit legacy providers and the arm is still in force."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    # churning fingerprints -> unstable sample -> rule 3a eligible
    _seed_pane(pane, sm, "t1", TerminalStatus.COMPLETED, fingerprints=["a", "b"])

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_fake_provider(migrated=False),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)

    assert reason == "pane_delta"
    assert status is TerminalStatus.PROCESSING


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5a_unresolvable_provider_falls_through(mock_backend, _wire_singletons):
    """A provider that cannot be resolved is treated as unmigrated (fall
    through to legacy) — the gate never fabricates a migrated decision."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    _seed_pane(pane, sm, "t1", TerminalStatus.COMPLETED, fingerprints=["a", "b"])

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=None,
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)
    # legacy arm reached
    assert reason == "pane_delta"


def test_ac5a_registry_agrees_with_provider_flags():
    """AC-10: the migration registry and the provider class flags must agree —
    no divergent authority."""
    from cli_agent_orchestrator.providers import status_contract as sc
    from cli_agent_orchestrator.providers.codex import CodexProvider
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider

    assert PiCliProvider.fx751_status_migrated is True
    assert CodexProvider.fx751_status_migrated is True
    assert sc.is_migrated("pi_cli") is True
    assert sc.is_migrated("codex") is True
    assert sc.is_migrated("grok_cli") is False
    assert sc.is_migrated("claude_code") is False
    assert sc.is_migrated("kiro_cli") is False
