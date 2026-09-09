"""AC-1 (D1): a warm desk call returns a cited answer within the 20 s wait; the
answer is at most 8 lines and 2048 UTF-8 bytes including citations; a call that
does not finish in the wait returns one PENDING line plus a handle.

Mutants (blueprint names two):
  M1  raise the answer budget past 2048 bytes  -> test_over_budget_answer_* RED
  M2  drop the PENDING branch so an unfinished call blocks -> test_pending_* RED
"""

from __future__ import annotations

from test.app.desk.conftest import ready_boundary

import pytest

from cli_agent_orchestrator.services import desk_service as ds
from cli_agent_orchestrator.services.desk_reconciler import reconcile_once


def _make_ready_desk(rig, cid="conv1"):
    rig.seed_live_conversation(cid)
    reconcile_once(ready_boundary)
    return cid


def test_within_budget_answer_returned_verbatim(desk_rig):
    cid = _make_ready_desk(desk_rig)
    admit = ds.admit_query(cid, "where is X defined?")
    assert admit.kind == "admitted"
    ds.complete_query(admit.request_id, ["X is at src/foo.py:42", "cite: src/foo.py:42"])
    wait = ds.wait_for(admit.request_id)
    assert wait.kind == "answer"
    assert wait.answer_lines == ("X is at src/foo.py:42", "cite: src/foo.py:42")
    assert wait.artifact_ref is None


def test_answer_at_most_eight_lines(desk_rig):
    cid = _make_ready_desk(desk_rig)
    admit = ds.admit_query(cid, "q")
    # 8 lines is the boundary — allowed; 9 lines is over budget.
    eight = [f"line{i}" for i in range(8)]
    ds.complete_query(admit.request_id, eight)
    wait = ds.wait_for(admit.request_id)
    assert wait.kind == "answer"
    assert wait.answer_lines is not None and len(wait.answer_lines) == 8


def test_over_budget_answer_becomes_compact_status_plus_artifact(desk_rig):
    """M1 target: a 9-line (or >2048-byte) answer must NOT be returned verbatim;
    it becomes a compact status plus an artifact pointer (never a cut-off
    quotation)."""
    cid = _make_ready_desk(desk_rig)
    admit = ds.admit_query(cid, "q")
    nine = [f"line{i}" for i in range(9)]
    ds.complete_query(admit.request_id, nine)
    wait = ds.wait_for(admit.request_id)
    assert wait.kind == "answer"
    assert wait.artifact_ref is not None
    assert wait.answer_lines is not None and len(wait.answer_lines) == 1
    assert "over budget" in wait.answer_lines[0]


def test_over_budget_by_bytes(desk_rig):
    """M1 target on the byte axis: <= 8 lines but > 2048 bytes is over budget."""
    cid = _make_ready_desk(desk_rig)
    admit = ds.admit_query(cid, "q")
    big = ["x" * 3000]  # one line, 3000 bytes
    ds.complete_query(admit.request_id, big)
    wait = ds.wait_for(admit.request_id)
    assert wait.artifact_ref is not None
    assert wait.answer_lines is not None and len(wait.answer_lines) == 1


def test_pending_returns_one_line_plus_handle(desk_rig):
    """M2 target: an unfinished call returns a PENDING line carrying the handle,
    not a block/answer."""
    cid = _make_ready_desk(desk_rig)
    admit = ds.admit_query(cid, "q")
    wait = ds.wait_for(admit.request_id)  # not completed yet
    assert wait.kind == "pending"
    assert wait.pending_line is not None
    assert admit.request_id in wait.pending_line
    assert wait.answer_lines is None
