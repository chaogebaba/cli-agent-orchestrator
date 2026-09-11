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


class _ReducerProvider:
    """A migrated provider whose derive_status runs the REAL reducer over the
    monitor-built typed sample, recording a chosen verdict as facts. This proves
    the monitor constructs a typed StatusSample with real provenance and that the
    reducer's Candidate is what governs — not a mocked _rederive helper."""

    def __init__(self, verdict: TerminalStatus) -> None:
        self.fx751_status_migrated = True
        self._verdict = verdict

    def derive_status(self, sample, context):  # type: ignore[no-untyped-def]
        from cli_agent_orchestrator.providers import status_contract as sc

        faceted = sc.apply_verdict_to_sample(sample, self._verdict)
        return sc.reduce(faceted, context)


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
    """AC-5a: a migrated provider whose typed derive_status reduces to
    PROCESSING holds PROCESSING under the fx751 reason — never ``pane_delta``
    and never ``child_proc_live``. The monitor builds the typed sample and the
    reducer decides (no _rederive helper)."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    _seed_pane(pane, sm, "t1", TerminalStatus.COMPLETED, fingerprints=["a", "b"])

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_ReducerProvider(TerminalStatus.PROCESSING),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)

    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_working"
    assert reason is not None and "pane_delta" not in reason
    assert reason != "child_proc_live"


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5a_migrated_no_fresh_evidence_holds_published(mock_backend, _wire_singletons):
    """AC-5a: when the reducer withholds a lowering (no confirmed evidence), a
    migrated lane HOLDS the published status under ``fx751_migrated`` — never a
    stale-buffer idle and never the expired-admit arm. Here the reducer sees a
    single readiness sample (target IDLE) with a PROCESSING last_status, so it
    returns awaiting_confirm/PROCESSING → the gate holds published."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    _seed_pane(pane, sm, "t1", TerminalStatus.PROCESSING, fingerprints=["a", "b"])

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_ReducerProvider(TerminalStatus.IDLE),
    ):
        # first tick: the reducer arms a pending lower and HOLDS PROCESSING
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_migrated"
    assert reason is not None and "pane_delta" not in reason


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_ac5a_migrated_two_samples_confirm_idle_via_reducer(mock_backend, _wire_singletons):
    """A true end lowers only after the reducer's D4 two-sample confirmation,
    driven through the live monitor path (context persisted+committed between
    ticks). The SECOND distinct sample admits IDLE under ``fx751_reducer``."""
    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_ReducerProvider(TerminalStatus.IDLE),
    ):
        # tick 1: fingerprint "a" → arm pending lower, hold PROCESSING
        _seed_pane(pane, sm, "t1", TerminalStatus.PROCESSING, fingerprints=["a"])
        sm._observation_seq["t1"] = 1
        s1, r1 = sm.fuse_status("t1", TerminalStatus.PROCESSING)
        assert s1 is TerminalStatus.PROCESSING and r1 == "fx751_migrated"
        # tick 2: distinct fingerprint "b" → second consecutive agreeing sample
        _seed_pane(pane, sm, "t1", TerminalStatus.PROCESSING, fingerprints=["b"])
        sm._observation_seq["t1"] = 2
        s2, r2 = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    assert s2 is TerminalStatus.IDLE
    assert r2 == "fx751_reducer"


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_b1_real_pi_derive_status_governs_fleet_projection(mock_backend, _wire_singletons):
    """B1 (verdict M1): drive the REAL PiCliProvider.derive_status(sample,
    context) through the monitor fusion path with a REAL captured working frame,
    and assert the reducer's output is what the fused observation carries — not a
    helper. The frame (fixture working-1.txt) is one pi classifies as PROCESSING;
    the fused status MUST be PROCESSING/fx751_working and the boundary
    observation MUST carry it. Catches an apply_verdict_to_sample no-op (which
    would drop the facts → UNKNOWN)."""
    import pathlib

    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider
    from cli_agent_orchestrator.utils.text import strip_terminal_escapes

    mock_backend.return_value = MagicMock()
    pane, _q, _clock = _wire_singletons
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.COMPLETED
    sm._observation_seq["t1"] = 1

    provider = PiCliProvider.__new__(PiCliProvider)
    provider.terminal_id = "t1"
    provider._task_dispatched = True
    provider._tui_processing_seen = False

    fixture = (
        pathlib.Path(__file__).resolve().parents[1]
        / "providers"
        / "fixtures"
        / "status_truth"
        / "pi_cli"
        / "working-1.txt"
    )
    working_frame = strip_terminal_escapes(fixture.read_text())
    # sanity: the real classifier reads this frame as PROCESSING
    assert provider._classify_verdict(working_frame) is TerminalStatus.PROCESSING

    def fake_capture(_tid):
        return _CaptureResult(
            fingerprint="fp-working",
            filtered_tail=working_frame,
            busy_marker=None,
            children_count=0,
            marker_rows=(),
        )

    with (
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.COMPLETED),
    ):
        pane.observe("t1", monitor=sm)

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=provider,
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)
        obs = sm.get_boundary_observation("t1")

    # the REAL provider classified the real frame; the reducer's projection is
    # what governs the fused observation (unconditional — no expected-branch).
    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_working"
    assert obs.status is TerminalStatus.PROCESSING


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
