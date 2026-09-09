"""AC-3 (D1): an inline desk answer produces zero inbox messages for that
request; the completion is addressed to the query record.

Mutant: route the completion through the inbox path -> test_no_inbox_* RED
(an inbox row would appear / the answer would not be on the record).
"""

from __future__ import annotations

from test.app.desk.conftest import ready_boundary

from sqlalchemy import text

from cli_agent_orchestrator.clients.database import DeskQueryModel
from cli_agent_orchestrator.services import desk_service as ds
from cli_agent_orchestrator.services.desk_reconciler import reconcile_once


def _inbox_count(rig) -> int:
    with rig.engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "inbox_messages" not in tables:
            return 0
        return conn.execute(text("SELECT COUNT(*) FROM inbox_messages")).scalar() or 0


def test_no_inbox_message_for_inline_answer(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    reconcile_once(ready_boundary)

    before = _inbox_count(desk_rig)
    admit = ds.admit_query(cid, "q")
    ds.complete_query(admit.request_id, ["answer", "cite: a.py:1"])
    after = _inbox_count(desk_rig)

    # Zero inbox messages produced by the inline completion.
    assert after == before

    # The completion is addressed to the query RECORD: the answer lives there.
    with desk_rig.SessionLocal() as s:
        row = s.query(DeskQueryModel).filter_by(request_id=admit.request_id).one()
        assert row.state == "COMPLETED"
        assert row.answer_lines == ["answer", "cite: a.py:1"]

    # And the handle read returns that same answer inline.
    assert ds.wait_for(admit.request_id).answer_lines == ("answer", "cite: a.py:1")
