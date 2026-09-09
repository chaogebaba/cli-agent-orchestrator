"""AC-10 (D5): retries, pings, spawns and compaction do not inflate completed
retrievals; NOT FOUND counts as a completed retrieval; UNKNOWN totals and
telemetry gaps stay visible beside the routed share.

Mutant: count an admitted query as completed -> test_admitted_not_completed RED.
"""

from __future__ import annotations

from cli_agent_orchestrator.services import desk_service as ds
from cli_agent_orchestrator.services.desk_reconciler import reconcile_once
from test.app.desk.conftest import ready_boundary


def _ready(rig, cid="conv1"):
    rig.seed_live_conversation(cid)
    reconcile_once(ready_boundary)
    return cid


def test_admitted_not_completed(desk_rig):
    """An admitted-but-not-completed query is NOT a completed retrieval."""
    cid = _ready(desk_rig)
    ds.admit_query(cid, "q")
    usage = ds.project_usage(cid)
    assert usage.admitted == 1
    assert usage.completed == 0
    assert usage.cited_answers == 0


def test_retry_does_not_inflate_completed(desk_rig):
    cid = _ready(desk_rig)
    admit = ds.admit_query(cid, "q")
    ds.complete_query(admit.request_id, ["a", "cite:x"])
    # Replay the same handle several times (a retry): completed stays 1.
    for _ in range(5):
        ds.admit_query(cid, "q", request_id=admit.request_id)
    usage = ds.project_usage(cid)
    assert usage.completed == 1
    assert usage.cited_answers == 1


def test_not_found_is_completed_but_not_cited(desk_rig):
    cid = _ready(desk_rig)
    admit = ds.admit_query(cid, "q")
    ds.complete_query(admit.request_id, ["nothing matched"], outcome="NOT_FOUND")
    usage = ds.project_usage(cid)
    assert usage.completed == 1  # a completed retrieval with its own outcome
    assert usage.cited_answers == 0  # NOT_FOUND is not a cited answer


def test_unknown_and_gaps_visible_beside_routed_share(desk_rig):
    cid = _ready(desk_rig)
    admit = ds.admit_query(cid, "q")
    ds.complete_query(admit.request_id, ["a", "cite:x"])
    # Observed non-exempt native retrieval + opaque bash + a telemetry gap.
    ds.record_event(cid, "0", ds.EV_NATIVE_LOCAL_DISCOVERY, "grep:1")
    ds.record_event(cid, "0", ds.EV_OPAQUE_BASH, "bash:1", payload={"output_bytes": 512})
    ds.record_event(cid, "0", ds.EV_TELEMETRY_GAP, "gap:1")

    usage = ds.project_usage(cid)
    # routed_share = completed / (completed + native) = 1 / (1 + 1) = 0.5
    assert usage.routed_share == 0.5
    # UNKNOWN opaque bash is NOT folded into the ratio; it sits beside it.
    assert usage.opaque_bash_calls == 1
    assert usage.opaque_output_bytes == 512
    assert usage.telemetry_gaps == 1
    assert "unknown_opaque_bash=1" in usage.coverage_note


def test_duplicate_native_event_deduplicated(desk_rig):
    cid = _ready(desk_rig)
    # Same (conversation, kind, dedup_key) recorded 3x -> counted once.
    for _ in range(3):
        ds.record_event(cid, "0", ds.EV_NATIVE_LOCAL_DISCOVERY, "grep:same")
    usage = ds.project_usage(cid)
    assert usage.native_local_discovery == 1
