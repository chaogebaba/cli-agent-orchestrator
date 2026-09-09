"""AC-12 (D6): a capped pi_cli never becomes a general or other-provider lane;
capped, cert_failed, fleet_full, server_down and init_timeout are each a
distinct typed cause.

Mutant: fall back to a general profile when certification fails ->
test_cert_failed_stays_degraded RED (state would be READY on a general lane).
"""

from __future__ import annotations

from test.app.desk.conftest import degraded_boundary

import pytest

from cli_agent_orchestrator.clients.database import DeskBindingModel
from cli_agent_orchestrator.services.desk_reconciler import (
    DEGRADED_CAUSES,
    CreateOutcome,
    CreateStatus,
    reconcile_once,
)


@pytest.mark.parametrize(
    "status_name,expected_cause",
    [
        ("capped", "capped"),
        ("cert_failed", "cert_failed"),
        ("fleet_full", "fleet_full"),
        ("server_down", "server_down"),
        ("init_timeout", "init_timeout"),
    ],
)
def test_each_failure_is_a_distinct_typed_cause(desk_rig, status_name, expected_cause):
    cid = f"conv_{status_name}"
    desk_rig.seed_live_conversation(cid)
    reconcile_once(degraded_boundary(status_name))
    with desk_rig.SessionLocal() as s:
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.state == "DEGRADED"
        assert b.degraded_cause == expected_cause
        assert b.degraded_cause in DEGRADED_CAUSES
        # Never a general/other-provider lane: no provider_binding was set.
        assert b.provider_binding is None


def test_cert_failed_stays_degraded_never_general(desk_rig):
    """The mutant target: certification failure must yield DEGRADED:cert_failed,
    NOT a READY general-profile fallback."""
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    reconcile_once(degraded_boundary("cert_failed"))
    with desk_rig.SessionLocal() as s:
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.state != "READY"
        assert b.state == "DEGRADED"
        assert b.degraded_cause == "cert_failed"


def test_capped_never_switches_provider(desk_rig):
    cid = "conv1"
    desk_rig.seed_live_conversation(cid, provider="pi_cli")
    reconcile_once(degraded_boundary("capped"))
    with desk_rig.SessionLocal() as s:
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.position == "secretary"
        assert b.provider_binding is None
        assert b.degraded_cause == "capped"
