"""F829 A1 — AC2 six-category refusal matrix through the PUBLIC assign(resume_from).

Every refusal must (1) return the ONE typed resume_refused envelope with a
missing token in the six-category closed set, (2) carry the mapped reason, and
(3) spawn ZERO terminals. Seeds real conversation_identity roots via the
real-sqlite fixture and drives server._assign_impl, patching the create path
(assert not called) and the caller-principal resolver.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients import database as d
from cli_agent_orchestrator.mcp_server import server

SIX = frozenset({"identity", "session_id", "artifact", "cwd", "provider_capability", "profile"})


def _mkroot(key, provider, *, owner="mb_owner", uuid="u-1", lifecycle="hibernated", artifact=None):
    d.mint_conversation_identity(
        identity_key=key,
        provider=provider,
        provider_namespace="ns",
        agent_profile="dev",
        model="m1",
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=f"t_{key}",
    )
    if uuid:
        d.bind_provider_session_id(key, provider_session_id=uuid, provider_namespace="ns")
        if artifact:
            d.publish_current_terminal(
                key, terminal_id=f"t_{key}", provider_session_id=uuid, artifact_locator=artifact
            )
    d.set_conversation_lifecycle(key, lifecycle)
    d.upsert_recovery_manifest(key, cwd="/tmp")


def _assign(resume_from, caller="mb_owner", **kw):
    """Drive the public assign(resume_from) with the create path stubbed and the
    caller principal fixed. Returns (result, create_mock)."""
    with (
        patch.object(server, "_create_terminal") as create,
        patch.object(server, "_f829_resolve_caller_principal", return_value=caller),
        patch.object(server, "_current_terminal_id", return_value="sup00001"),
    ):
        result = server._assign_impl("dev", "task", resume_from=resume_from, **kw)
    return result, create


def _assert_refused(result, create, *, missing, reason):
    assert result["success"] is False, result
    assert result["error"] == "resume_refused"
    assert result["missing"] in SIX and result["missing"] == missing
    assert result["reason"] == reason
    create.assert_not_called()


def test_ac2_resume_not_owner(real_sqlite_env):
    _mkroot("k1", "codex", owner="mb_owner", uuid="u1")
    result, create = _assign("k1", caller="mb_intruder")
    _assert_refused(result, create, missing="identity", reason="resume_not_owner")
    assert result["identity_key"] == "k1"


def test_ac2_null_owner_is_not_open_season(real_sqlite_env):
    # A NULL-owner root requires an explicit claim; a requester is refused.
    _mkroot("k1b", "codex", owner=None, uuid="u1b")
    result, create = _assign("k1b", caller="mb_anyone")
    _assert_refused(result, create, missing="identity", reason="resume_not_owner")


def test_ac2_session_live_owned(real_sqlite_env):
    _mkroot("k2", "codex", owner="mb_owner", uuid="u2", lifecycle="live")
    result, create = _assign("k2")
    _assert_refused(result, create, missing="identity", reason="session_live_owned")


def test_ac2_session_abandoned(real_sqlite_env):
    _mkroot("k3", "codex", owner="mb_owner", uuid="u3", lifecycle="abandoned")
    result, create = _assign("k3")
    _assert_refused(result, create, missing="identity", reason="session_abandoned")


def test_ac2_session_expired(real_sqlite_env):
    _mkroot("k4", "codex", owner="mb_owner", uuid="u4", lifecycle="expired")
    result, create = _assign("k4")
    _assert_refused(result, create, missing="identity", reason="session_expired")


def test_ac2_capture_unknown_is_artifact_missing(real_sqlite_env):
    # A capture_unknown root (no uuid) → session_artifact_missing (D3 classify).
    _mkroot("k5", "kiro_cli", owner="mb_owner", uuid=None, lifecycle="capture_unknown")
    result, create = _assign("k5")
    _assert_refused(result, create, missing="artifact", reason="session_artifact_missing")


def test_ac2_pi_without_artifact_is_artifact(real_sqlite_env):
    _mkroot("k6", "pi_cli", owner="mb_owner", uuid="u6", artifact=None)
    result, create = _assign("k6")
    _assert_refused(result, create, missing="artifact", reason="pi_artifact_locator_null")


def test_ac2_session_resume_in_progress(real_sqlite_env):
    # A held resume_claim → the CAS at spawn loses → session_resume_in_progress.
    _mkroot("k7", "codex", owner="mb_owner", uuid="u7", lifecycle="hibernated")
    gen = d.get_conversation_identity("k7")["generation"]
    assert d.claim_resume("k7", gen, "other-claimant") is True  # someone else holds it
    result, create = _assign("k7")
    _assert_refused(result, create, missing="identity", reason="session_resume_in_progress")
    assert result["retryable"] is True


def test_ac2_resume_from_plus_fork_from_conflict(real_sqlite_env):
    result, create = _assign("k-any", fork_from="base")
    _assert_refused(result, create, missing="identity", reason="resume_input_conflict")


def test_ac2_unknown_handle_is_identity(real_sqlite_env):
    result, create = _assign("nonexistent-handle")
    assert result["success"] is False and result["error"] == "resume_refused"
    assert result["missing"] == "identity"
    create.assert_not_called()


def test_ac2_every_refusal_missing_in_six_category_set(real_sqlite_env):
    """Sweep: each seeded refusal's missing token is in the closed six-set."""
    cases = [
        ("s1", "codex", {"owner": "mb_owner", "uuid": "su1", "lifecycle": "abandoned"}, "mb_owner"),
        ("s2", "pi_cli", {"owner": "mb_owner", "uuid": "su2", "artifact": None}, "mb_owner"),
        ("s3", "codex", {"owner": "mb_owner", "uuid": "su3"}, "mb_intruder"),
    ]
    for key, prov, kw, caller in cases:
        _mkroot(key, prov, **kw)
        result, create = _assign(key, caller=caller)
        assert result["missing"] in SIX
        create.assert_not_called()


# --------------------------------------------------------------------------
# AC1 (positive) + AC7 mutants — through the PUBLIC assign(resume_from)
# --------------------------------------------------------------------------


def _assign_reaches_spawn(resume_from, caller="mb_owner"):
    """Drive assign(resume_from) with a stubbed create that returns a fake
    (terminal_id, provider); return (result, create_mock)."""
    with (
        patch.object(server, "_create_terminal", return_value=("new00001", "codex")) as create,
        patch.object(server, "_f829_resolve_caller_principal", return_value=caller),
        patch.object(server, "_current_terminal_id", return_value="sup00001"),
        # keep the rest of the assign body from doing real IO after create.
        patch.object(server, "_send_initial_task", return_value=None, create=True),
    ):
        result = server._assign_impl("dev", "task", resume_from=resume_from)
    return result, create


def test_ac1_kiro_resume_reaches_spawn_despite_no_fork(real_sqlite_env):
    """AC7 mutant guard: kiro CANNOT fork (declares fork=False) yet a resumable
    kiro root REACHES _create_terminal exactly once with a resume-mode
    fork_context — proving resume does NOT gate on fork capability."""
    _mkroot("kk", "kiro_cli", owner="mb_owner", uuid="sess_kk-uuid", lifecycle="hibernated")
    result, create = _assign_reaches_spawn("kk")
    assert create.call_count == 1, result
    # the fork_context handed to create is resume-mode carrying the stored id.
    _, kwargs = create.call_args
    fc = kwargs.get("fork_context")
    assert fc is not None and fc.mode == "resume"
    assert fc.session_uuid == "sess_kk-uuid"


def test_ac1_codex_resume_reaches_spawn_and_claims(real_sqlite_env):
    """A hibernated codex root reaches spawn once AND the CAS claim is taken
    (a second concurrent resume would then lose — AC2 concurrency)."""
    _mkroot("kc", "codex", owner="mb_owner", uuid="cc-uuid", lifecycle="hibernated")
    result, create = _assign_reaches_spawn("kc")
    assert create.call_count == 1
    # the claim is now held → a second resume attempt refuses.
    result2, create2 = _assign("kc")
    _assert_refused(result2, create2, missing="identity", reason="session_resume_in_progress")
