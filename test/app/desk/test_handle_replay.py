"""AC-2 (D1): replaying a request_id retrieves the same DeskQuery record and
dispatches no second job; the 120-second deadline produces a typed failure, not
an open pending state.

Mutant: make a repeated handle admit a new query -> test_replay_* RED.
"""

from __future__ import annotations

from cli_agent_orchestrator.clients.database import DeskQueryModel
from cli_agent_orchestrator.services import desk_service as ds
from cli_agent_orchestrator.services.desk_reconciler import reconcile_once
from test.app.desk.conftest import ready_boundary


def _ready(rig, cid="conv1"):
    rig.seed_live_conversation(cid)
    reconcile_once(ready_boundary)
    return cid


def test_replay_retrieves_same_record_no_second_job(desk_rig):
    cid = _ready(desk_rig)
    first = ds.admit_query(cid, "q")
    assert first.kind == "admitted"

    replay = ds.admit_query(cid, "q", request_id=first.request_id)
    assert replay.kind == "replayed"
    assert replay.replayed is True
    assert replay.request_id == first.request_id

    # No second job: exactly one DeskQuery row exists.
    with desk_rig.SessionLocal() as s:
        assert s.query(DeskQueryModel).count() == 1


def test_job_deadline_produces_typed_failure_not_open_pending(desk_rig):
    cid = _ready(desk_rig)
    admit = ds.admit_query(cid, "q")

    # Before the 120 s deadline: still pending, sweep does nothing.
    desk_rig.clock.advance(119)
    assert ds.expire_overdue(cid) == 0
    assert ds.wait_for(admit.request_id).kind == "pending"

    # Past the 120 s deadline: sweep turns it into a typed failure.
    desk_rig.clock.advance(2)  # t = 121s
    assert ds.expire_overdue(cid) == 1
    wait = ds.wait_for(admit.request_id)
    assert wait.kind == "failed"
    assert wait.outcome == "DEADLINE_EXCEEDED"

    # The record is terminal (EXPIRED), never an open pending state.
    with desk_rig.SessionLocal() as s:
        row = s.query(DeskQueryModel).filter_by(request_id=admit.request_id).one()
        assert row.state == "EXPIRED"


def test_replay_after_completion_returns_same_answer(desk_rig):
    cid = _ready(desk_rig)
    admit = ds.admit_query(cid, "q")
    ds.complete_query(admit.request_id, ["answer", "cite: a.py:1"])
    replay = ds.admit_query(cid, "q", request_id=admit.request_id)
    assert replay.kind == "replayed"
    assert ds.wait_for(replay.request_id).answer_lines == ("answer", "cite: a.py:1")
