"""AC-14 (D6, pack.md:2001): no hibernation path is reachable until the
provider's resume artifact and a memory-retention round trip are verified;
without them the desk stops at session end.

Mutant: hibernate on a merely declared artifact -> test_declared_only RED
(hibernation_allowed would return True with only the artifact declared).
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.clients.database import DeskBindingModel
from cli_agent_orchestrator.services.desk_reconciler import (
    hibernation_allowed,
    reconcile_once,
    stop_at_session_end,
)
from test.app.desk.conftest import ready_boundary


def test_both_verified_allows_hibernation(desk_rig):
    assert hibernation_allowed(
        resume_artifact_verified=True, memory_roundtrip_verified=True
    ) is True


def test_declared_only_refuses(desk_rig):
    """A merely DECLARED artifact (verified=False) or a missing memory round
    trip must NOT enable hibernation."""
    assert hibernation_allowed(
        resume_artifact_verified=True, memory_roundtrip_verified=False
    ) is False
    assert hibernation_allowed(
        resume_artifact_verified=False, memory_roundtrip_verified=True
    ) is False
    assert hibernation_allowed(
        resume_artifact_verified=False, memory_roundtrip_verified=False
    ) is False


def test_desk_stops_at_session_end(desk_rig):
    """Without verified hibernation, session end STOPS the desk (cold-start
    next session), never a hibernated/persisted state."""
    cid = "conv1"
    desk_rig.seed_live_conversation(cid)
    reconcile_once(ready_boundary)
    stop_at_session_end(cid)
    with desk_rig.SessionLocal() as s:
        b = s.query(DeskBindingModel).filter_by(conversation_id=cid).one()
        assert b.state == "STOPPED"
