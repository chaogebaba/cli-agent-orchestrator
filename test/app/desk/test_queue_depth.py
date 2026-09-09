"""AC-19 (D1, B4): with four queries queued on a desk, the fifth desk() call
returns BUSY_QUEUE_FULL inline, enqueuing nothing and starting no 120-second
deadline; the four queued queries still complete or fail on their own deadlines.

Mutant: admit a fifth query past the depth -> test_fifth_* RED.
"""

from __future__ import annotations

from test.app.desk.conftest import ready_boundary

from cli_agent_orchestrator.clients.database import DeskBindingModel, DeskQueryModel
from cli_agent_orchestrator.services import desk_service as ds
from cli_agent_orchestrator.services.desk_reconciler import reconcile_once


def _ready(rig, cid="conv1"):
    rig.seed_live_conversation(cid)
    reconcile_once(ready_boundary)
    return cid


def test_fifth_call_refused_inline(desk_rig):
    cid = _ready(desk_rig)
    handles = [ds.admit_query(cid, f"q{i}") for i in range(4)]
    assert all(h.kind == "admitted" for h in handles)

    fifth = ds.admit_query(cid, "q5")
    assert fifth.kind == "queue_full"
    assert fifth.request_id is None  # no handle

    # Enqueued nothing: still exactly four query rows.
    with desk_rig.SessionLocal() as s:
        assert s.query(DeskQueryModel).count() == 4
        # queue_depth on the binding is exactly 4, not 5.
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.queue_depth == 4


def test_four_queued_still_complete_on_their_deadlines(desk_rig):
    cid = _ready(desk_rig)
    handles = [ds.admit_query(cid, f"q{i}") for i in range(4)]
    ds.admit_query(cid, "q5")  # refused

    # Complete one; that frees a slot so a new admit succeeds.
    ds.complete_query(handles[0].request_id, ["a", "cite:x"])
    assert ds.wait_for(handles[0].request_id).kind == "answer"

    sixth = ds.admit_query(cid, "q6")
    assert sixth.kind == "admitted"

    # The remaining originals still expire on their own 120 s deadline.
    desk_rig.clock.advance(121)
    expired = ds.expire_overdue(cid)
    # handles[1..3] + sixth are still active at expiry time -> all expire.
    assert expired == 4


def test_queue_full_starts_no_deadline(desk_rig):
    cid = _ready(desk_rig)
    for i in range(4):
        ds.admit_query(cid, f"q{i}")
    before = _row_count(desk_rig)
    ds.admit_query(cid, "q5")
    ds.admit_query(cid, "q6")
    # No new rows, hence no new deadlines started, on repeated refusals.
    assert _row_count(desk_rig) == before


def _row_count(rig) -> int:
    with rig.SessionLocal() as s:
        return s.query(DeskQueryModel).count()
