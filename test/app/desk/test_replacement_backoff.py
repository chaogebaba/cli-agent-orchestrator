"""AC-13 (D6): automatic replacement runs once per failure episode, then follows
retry-after or a five-minute backoff and never overlaps a live incarnation;
BUSY work is not reaped.

Mutant: retry on every tool call -> test_no_retry_within_backoff RED (a second
tick within the backoff window would attempt again).
"""

from __future__ import annotations

from test.app.desk.conftest import degraded_boundary, ready_boundary

from cli_agent_orchestrator.clients.database import DeskBindingModel
from cli_agent_orchestrator.services.desk_reconciler import (
    REPLACEMENT_BACKOFF_SECONDS,
    CreateOutcome,
    CreateStatus,
    reconcile_once,
)


class CountingBoundary:
    """Records every boundary invocation so we can assert one-per-episode."""

    def __init__(self, outcome_fn):
        self.calls = 0
        self._fn = outcome_fn

    def __call__(self, cid, inc):
        self.calls += 1
        return self._fn(cid, inc)


def test_one_attempt_per_episode_then_backoff(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    boundary = CountingBoundary(degraded_boundary("capped"))

    # First tick: one automatic replacement attempt -> DEGRADED with a deadline.
    reconcile_once(boundary)
    assert boundary.calls == 1
    with desk_rig.SessionLocal() as s:
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.state == "DEGRADED"
        assert b.retry_deadline is not None
        assert b.replacement_attempts == 1


def test_no_retry_within_backoff(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    boundary = CountingBoundary(degraded_boundary("capped"))
    reconcile_once(boundary)
    assert boundary.calls == 1

    # Several ticks WITHIN the backoff window must not attempt again.
    desk_rig.clock.advance(REPLACEMENT_BACKOFF_SECONDS - 10)
    reconcile_once(boundary)
    reconcile_once(boundary)
    assert boundary.calls == 1  # no retry on every tick


def test_retry_after_backoff_elapses(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    boundary = CountingBoundary(degraded_boundary("capped"))
    reconcile_once(boundary)
    assert boundary.calls == 1

    # Past the backoff deadline: one further attempt is allowed.
    desk_rig.clock.advance(REPLACEMENT_BACKOFF_SECONDS + 1)
    reconcile_once(boundary)
    assert boundary.calls == 2


def test_provider_retry_after_overrides_default_backoff(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    boundary = CountingBoundary(degraded_boundary("capped", retry_after=30))
    reconcile_once(boundary)

    # Within the 30 s provider retry-after: no attempt.
    desk_rig.clock.advance(20)
    reconcile_once(boundary)
    assert boundary.calls == 1
    # After it: one attempt.
    desk_rig.clock.advance(11)
    reconcile_once(boundary)
    assert boundary.calls == 2


def test_recovery_to_ready_closes_episode(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    reconcile_once(degraded_boundary("capped"))
    desk_rig.clock.advance(REPLACEMENT_BACKOFF_SECONDS + 1)
    reconcile_once(ready_boundary)
    with desk_rig.SessionLocal() as s:
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.state == "READY"
        assert b.replacement_attempts == 0  # episode closed
        assert b.retry_deadline is None
