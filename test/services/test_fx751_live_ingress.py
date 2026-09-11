"""fx751 Slice A r2 — the reducer is wired into the LIVE status path (B1/B2/S1).

These tests exercise the production ingress ``StatusMonitor.fuse_status`` →
``_fx751_migrated_fusion``, which now builds a typed ``StatusSample`` with real
provenance, calls the migrated provider's typed ``derive_status(sample,
context)``, and COMMITS the candidate via the generation compare-and-commit
transaction. They are the regressions the r1 verdict (M1/M2/S1) required:

* B1 — the real typed provider method decides, and its output is what the fused
  observation carries (see also test_fx751_fusion_migration).
* B2 — the live path routes through ``fx751_commit_candidate``; if the commit is
  neutered (no-op), the wrong status is published, so this test FAILS.
* S1 — a production-level generation race: an observation whose sample generation
  is stale is rejected at ingress and cannot lower the lane.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import status_contract as sc
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult
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


class _ReducerProvider:
    """Migrated provider whose typed derive_status runs the REAL reducer over
    the monitor-built sample, recording a chosen verdict as facts."""

    def __init__(self, verdict: TerminalStatus) -> None:
        self.fx751_status_migrated = True
        self._verdict = verdict

    def derive_status(self, sample, context):  # type: ignore[no-untyped-def]
        faceted = sc.apply_verdict_to_sample(sample, self._verdict)
        return sc.reduce(faceted, context)


def _seed(pane, sm, tid, fp):
    def fake_capture(_tid):
        return _CaptureResult(
            fingerprint=fp,
            filtered_tail="frame-text",
            busy_marker=None,
            children_count=0,
            marker_rows=(),
        )

    with (
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.PROCESSING),
    ):
        pane.observe(tid, monitor=sm)


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_b2_live_path_routes_through_commit_helper(mock_backend, _wire):
    """B2: the live migrated path COMMITS the candidate. When the commit is
    forced to REJECT (as the generation guard would on a raced invalidation),
    the lane HOLDS the published status rather than admitting the reducer's
    lowering. The mutant that makes fx751_commit_candidate a no-op (always
    admit) flips this to a lowered status → this test fails."""
    mock_backend.return_value = MagicMock()
    pane, _clock = _wire
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    sm._observation_seq["t1"] = 1
    _seed(pane, sm, "t1", "fp-a")

    # A reducer that WANTS to lower to IDLE (event-confirmed so it would admit in
    # one sample) — so only the commit gate stands between it and a lowered
    # publish. Force the commit to reject.
    class _EventIdleProvider:
        fx751_status_migrated = True

        def derive_status(self, sample, context):  # type: ignore[no-untyped-def]
            # mark the sample as event-confirmed so the reducer admits IDLE
            s2 = dataclasses.replace(sample, native_coverage=True, native_end_event=True)
            faceted = sc.apply_verdict_to_sample(s2, TerminalStatus.IDLE)
            return sc.reduce(faceted, context)

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=_EventIdleProvider(),
        ),
        patch.object(sm, "fx751_commit_candidate", return_value=False) as commit,
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    # commit WAS consulted (the live path routes through it) ...
    assert commit.called, "live migrated path must call fx751_commit_candidate"
    # ... and its rejection held the published status (no lowering admitted).
    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_migrated"


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_b2_committed_candidate_governs_when_accepted(mock_backend, _wire):
    """The complement: when the commit ACCEPTS, the reducer's lowered verdict is
    what the lane publishes (event-confirmed IDLE)."""
    mock_backend.return_value = MagicMock()
    pane, _clock = _wire
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    sm._observation_seq["t1"] = 1
    _seed(pane, sm, "t1", "fp-a")

    class _EventIdleProvider:
        fx751_status_migrated = True

        def derive_status(self, sample, context):  # type: ignore[no-untyped-def]
            s2 = dataclasses.replace(sample, native_coverage=True, native_end_event=True)
            faceted = sc.apply_verdict_to_sample(s2, TerminalStatus.IDLE)
            return sc.reduce(faceted, context)

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_EventIdleProvider(),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    assert status is TerminalStatus.IDLE
    assert reason == "fx751_reducer"
    # the committed context is retained for the terminal
    assert "t1" in sm._fx751_reducer_ctx


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_s1_generation_race_older_generation_rejected_at_ingress(mock_backend, _wire):
    """S1 (production race): an invalidation between capture and commit advances
    the lifecycle generation; a candidate whose sample carries the OLD generation
    is rejected at the live ingress and cannot repopulate/lower the lane.

    Simulated at the real seam: the provider's derive_status stamps its candidate
    with a STALE lifecycle generation (as an in-flight pre-invalidation sample
    would), and clear_terminal has advanced the current generation. The commit
    guard on the live path must reject it."""
    mock_backend.return_value = MagicMock()
    pane, _clock = _wire
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    sm._observation_seq["t1"] = 1
    _seed(pane, sm, "t1", "fp-a")

    # advance the generation as an invalidation would (teardown/rebind), so the
    # sample the live path builds carries gen=1, but our racing provider returns
    # a candidate whose next_context is stamped with the OLD gen 0.
    sm.clear_terminal("t1")  # bumps _fx751_lifecycle_gen to 1, evicts context
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    sm._observation_seq["t1"] = 1
    _seed(pane, sm, "t1", "fp-b")

    class _StaleGenProvider:
        """Simulates the production race: while THIS observation is being derived
        (sample already stamped with the current gen g), a concurrent
        invalidation (teardown/rebind on another thread) advances the generation
        to g+1. When the monitor then commits, the commit guard sees the sample's
        now-stale gen g against current g+1 and rejects."""

        fx751_status_migrated = True

        def __init__(self, monitor):  # type: ignore[no-untyped-def]
            self._sm = monitor

        def derive_status(self, sample, context):  # type: ignore[no-untyped-def]
            # concurrent invalidation lands mid-derive → generation advances
            self._sm.clear_terminal("t1")
            faceted = sc.apply_verdict_to_sample(
                dataclasses.replace(sample, native_coverage=True, native_end_event=True),
                TerminalStatus.IDLE,
            )
            return sc.reduce(faceted, context)

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_StaleGenProvider(sm),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    # the stale-generation candidate is rejected → the lane is NOT lowered
    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_migrated"
    # and the stale context did not repopulate the evicted map
    assert sm._fx751_reducer_ctx.get("t1") is None


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_b1_no_synthesized_freshness_on_live_path(mock_backend, _wire):
    """B1/B2 (verdict): the live path must NOT fabricate age=0/native_end_event.
    A migrated provider that reduces a readiness-only sample (no event) gets a
    HELD status on the first sample — proving the reducer's D4 two-sample gate is
    actually in force at ingress (a fabricated event would lower in one sample)."""
    mock_backend.return_value = MagicMock()
    pane, _clock = _wire
    sm = StatusMonitor()
    sm._last_status["t1"] = TerminalStatus.PROCESSING
    sm._observation_seq["t1"] = 1
    _seed(pane, sm, "t1", "fp-a")

    with patch(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        return_value=_ReducerProvider(TerminalStatus.IDLE),
    ):
        status, reason = sm.fuse_status("t1", TerminalStatus.PROCESSING)

    # one readiness sample, no event → the reducer holds, does NOT lower.
    assert status is TerminalStatus.PROCESSING
    assert reason == "fx751_migrated"
