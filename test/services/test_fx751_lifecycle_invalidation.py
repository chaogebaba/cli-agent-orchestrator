"""fx751 Slice A (AC-8, AC-9): lifecycle invalidation + generation compare-and-commit.

One idempotent invalidation path (clear_terminal, which unregister routes
through) evicts the reducer context and BUMPS the lifecycle generation, so an
in-flight pre-invalidation sample cannot repopulate the evicted context. The
two F899 evictions remain a floor this path preserves, and reset_buffer is NOT
on this path (D5: buffer reset is not cleanup).
"""

from __future__ import annotations

from unittest.mock import patch

from cli_agent_orchestrator.providers import status_contract as sc
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def _sample(term: str, gen: int) -> sc.StatusSample:
    return sc.StatusSample(
        terminal_id=term,
        lifecycle_generation=gen,
        sample_mode=sc.SampleMode.DIRECT_RENDERED,
        declared_modes=(sc.SampleMode.DIRECT_RENDERED,),
        filtered_fingerprint="fp",
        readiness=sc.ReadinessFact(value=sc.FactValue.PRESENT),
    )


def test_ac8_invalidation_bumps_generation_and_evicts_context():
    sm = StatusMonitor()
    tid = "t1"
    # seed a reducer context
    ctx = sc.ReducerContext(terminal_id=tid, lifecycle_generation=0, last_sequence=5)
    sm._fx751_reducer_ctx[tid] = ctx
    assert sm.fx751_lifecycle_generation(tid) == 0

    sm.clear_terminal(tid)

    assert tid not in sm._fx751_reducer_ctx  # evicted
    assert sm.fx751_lifecycle_generation(tid) == 1  # bumped


def test_ac8_commit_rejects_stale_generation_sample():
    """A sample captured before invalidation (old generation) cannot repopulate
    the evicted context — compare-and-commit rejects it."""
    sm = StatusMonitor()
    tid = "t1"
    # generation advances to 1 via an invalidation
    sm.clear_terminal(tid)
    assert sm.fx751_lifecycle_generation(tid) == 1

    stale = _sample(tid, gen=0)  # captured under the OLD generation
    cand = sc.reduce(stale, sm.fx751_reducer_context(tid))
    committed = sm.fx751_commit_candidate(tid, stale, cand)
    assert committed is False
    assert tid not in sm._fx751_reducer_ctx  # not repopulated

    fresh = _sample(tid, gen=1)  # current generation
    cand2 = sc.reduce(fresh, sm.fx751_reducer_context(tid))
    assert sm.fx751_commit_candidate(tid, fresh, cand2) is True
    assert tid in sm._fx751_reducer_ctx


def test_ac8_invalidation_is_idempotent():
    sm = StatusMonitor()
    tid = "t1"
    sm._fx751_reducer_ctx[tid] = sc.ReducerContext(terminal_id=tid)
    sm.clear_terminal(tid)
    g1 = sm.fx751_lifecycle_generation(tid)
    # a second invalidation on an already-clean terminal does not raise and
    # keeps advancing the generation monotonically (never resurrects context).
    sm.clear_terminal(tid)
    g2 = sm.fx751_lifecycle_generation(tid)
    assert g2 == g1 + 1
    assert tid not in sm._fx751_reducer_ctx


def test_ac8_preserves_f899_evictions():
    """The two F899 evictions are a floor this path preserves: _last_rederive_check
    pop and child_proc_probe.forget."""
    sm = StatusMonitor()
    tid = "t1"
    sm._last_rederive_check[tid] = 123.0
    with patch(
        "cli_agent_orchestrator.services.child_proc_probe.child_proc_probe.forget"
    ) as forget:
        sm.clear_terminal(tid)
    assert tid not in sm._last_rederive_check
    forget.assert_called_once_with(tid)


def test_ac8_unregister_routes_through_the_one_path():
    sm = StatusMonitor()
    tid = "t1"
    sm._fx751_reducer_ctx[tid] = sc.ReducerContext(terminal_id=tid)
    sm.unregister(tid)
    assert tid not in sm._fx751_reducer_ctx
    assert sm.fx751_lifecycle_generation(tid) == 1


def test_ac8_reset_buffer_is_not_on_the_invalidation_path():
    """reset_buffer must NOT evict the reducer context or bump the lifecycle
    generation — it is ordinary buffer maintenance, not a lifecycle transition
    (D5)."""
    sm = StatusMonitor()
    tid = "t1"
    ctx = sc.ReducerContext(terminal_id=tid, last_sequence=9)
    sm._fx751_reducer_ctx[tid] = ctx
    gen_before = sm.fx751_lifecycle_generation(tid)
    # reset_buffer reads terminal metadata; stub it so the call is exercised
    # without a real DB. The assertion is that the fx751 lifecycle state is
    # untouched regardless of what the buffer-reset internals do.
    with patch(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        return_value=None,
    ):
        try:
            sm.reset_buffer(tid)
        except Exception:
            pass
    assert sm._fx751_reducer_ctx.get(tid) is ctx  # untouched
    assert sm.fx751_lifecycle_generation(tid) == gen_before  # not bumped
