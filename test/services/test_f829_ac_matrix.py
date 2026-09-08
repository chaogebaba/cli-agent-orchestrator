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


# -- B1 closure: the three categories that lacked a PUBLIC assign(resume_from) --
# witness with a DISTINCT typed code (verdict B1: session_id, cwd,
# provider_capability were only exercised by legacy fork_from or direct-service).


def test_ac2_session_id_missing_through_public_assign(real_sqlite_env):
    """A hibernated, owned root that captured NO provider session id passes the
    classify gate (hibernated is resumable) and refuses in _build_launch_spec
    with missing=session_id / provider_session_id_null — through PUBLIC assign."""
    # uuid=None → no provider_session_id bound; lifecycle hibernated (resumable).
    _mkroot("sid1", "codex", owner="mb_owner", uuid=None, lifecycle="hibernated")
    result, create = _assign("sid1")
    _assert_refused(result, create, missing="session_id", reason="provider_session_id_null")
    assert result["identity_key"] == "sid1"


def test_ac2_cwd_missing_no_provenance_through_public_assign(real_sqlite_env):
    """A resumable owned root whose recorded cwd is gone and has no worktree
    provenance refuses with missing=cwd / cwd_missing_no_provenance — through
    PUBLIC assign, with NO working_directory override supplied."""
    _mkroot("cwd1", "codex", owner="mb_owner", uuid="cwd-uuid", lifecycle="hibernated")
    # Point the manifest at a directory that does not exist and record no
    # worktree provenance, so reconstruction is impossible.
    gone = "/nonexistent/f829/cwd1-gone-dir"
    d.upsert_recovery_manifest("cwd1", cwd=gone)
    result, create = _assign("cwd1")  # no working_directory override
    _assert_refused(result, create, missing="cwd", reason="cwd_missing_no_provenance")


def test_ac2_provider_capability_failed_through_public_assign(real_sqlite_env):
    """A FAILED exact-key resume evidence row refuses in the production
    _build_launch_spec with missing=provider_capability — through PUBLIC assign
    (verdict B3/B1: measured-capability enforcement on the real path)."""
    _mkroot("pc1", "codex", owner="mb_owner", uuid="pc-uuid", lifecycle="hibernated")
    d.record_capability_evidence("codex", "resume", "failed")
    result, create = _assign("pc1")
    _assert_refused(
        result, create, missing="provider_capability", reason="codex_resume_capability_failed"
    )
    assert result["retryable"] is True


# The six-category closed set is asserted for FULL COVERAGE (not membership) by
# the sweep below: every member is witnessed through the PUBLIC assign seam.


def test_ac2_sweep_covers_every_one_of_the_six_categories(real_sqlite_env):
    """Verdict B1: the sweep asserts COVERAGE of every member of the closed
    six-category set through the PUBLIC assign(resume_from) seam — not mere
    membership in the set. Each category below produces a refusal whose ``missing``
    equals that exact category, with zero spawn."""
    observed: set[str] = set()

    # identity — abandoned lifecycle.
    _mkroot("id_w", "codex", owner="mb_owner", uuid="idw", lifecycle="abandoned")
    r, c = _assign("id_w")
    assert r["missing"] == "identity"
    c.assert_not_called()
    observed.add(r["missing"])

    # artifact — pi with no recorded artifact locator.
    _mkroot("art_w", "pi_cli", owner="mb_owner", uuid="artw", artifact=None)
    r, c = _assign("art_w")
    assert r["missing"] == "artifact"
    c.assert_not_called()
    observed.add(r["missing"])

    # session_id — hibernated root, no captured provider session id.
    _mkroot("sid_w", "codex", owner="mb_owner", uuid=None, lifecycle="hibernated")
    r, c = _assign("sid_w")
    assert r["missing"] == "session_id"
    c.assert_not_called()
    observed.add(r["missing"])

    # cwd — resumable root, recorded cwd gone, no worktree provenance.
    _mkroot("cwd_w", "codex", owner="mb_owner", uuid="cwdw", lifecycle="hibernated")
    d.upsert_recovery_manifest("cwd_w", cwd="/nonexistent/f829/cwd_w-gone")
    r, c = _assign("cwd_w")
    assert r["missing"] == "cwd"
    c.assert_not_called()
    observed.add(r["missing"])

    # provider_capability — FAILED exact-key resume evidence.
    _mkroot("pc_w", "codex", owner="mb_owner", uuid="pcw", lifecycle="hibernated")
    d.record_capability_evidence("codex", "resume", "failed")
    r, c = _assign("pc_w")
    assert r["missing"] == "provider_capability"
    c.assert_not_called()
    observed.add(r["missing"])
    # clear the failed row so the profile witness (needs a spawnable path up to
    # the pins gate) is not tripped by capability.
    d.record_capability_evidence("codex", "resume", "passed")

    # profile — inherit_pins=False while the reaped terminal HAD frozen pins and
    # no replacement authority_files supplied (server-level refusal, missing=profile).
    _mkroot("prof_w", "codex", owner="mb_owner", uuid="profw", lifecycle="hibernated")
    d.upsert_recovery_manifest("prof_w", cwd="/tmp")
    # freeze a pin on the CURRENT incarnation (t_prof_w) so known_pins is
    # non-empty; write the row directly (append-only authority_pin store).
    with d.SessionLocal.begin() as _s:
        _s.add(
            d.AuthorityPinModel(
                task_key="t_prof_w",
                file_path="/tmp/a",
                sha256="0" * 64,
                version=1,
                registered_by="test",
                frozen=True,
            )
        )
    r, c = _assign("prof_w", inherit_pins=False)
    assert r["missing"] == "profile", r
    c.assert_not_called()
    observed.add(r["missing"])

    # COVERAGE assertion: every member of the closed six-set was witnessed.
    assert observed == SIX, f"uncovered categories: {SIX - observed}"


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


# --------------------------------------------------------------------------
# AC1 (positive) — INTEGRATED resume through the PRODUCTION create/publish path.
# Verdict B2: prior tests stubbed _create_terminal and stopped at reachability.
# This drives the real prepare_resume → claim → publish/verify seam (a fake
# provider stands in for the tmux spawn, per brief: unit-level with a fake
# provider is acceptable; live arms are AC1 separately). It PROVES: resumed
# incarnation exists, identity verified, ONE root / TWO incarnations, blind
# checkpoint (claim), workspace + pins retained, callback continuity.
# --------------------------------------------------------------------------

from cli_agent_orchestrator.clients.database import (  # noqa: E402
    TerminalIdentityModel,
)
from cli_agent_orchestrator.services.conversation_transition import (  # noqa: E402
    attach_captured_uuid,
    authorize_and_classify_resume,
    claim_resume_admission,
)
from cli_agent_orchestrator.services.resume_service import prepare_resume  # noqa: E402


def _seed_resumable_with_incarnation(key, *, provider, owner, uuid, old_terminal, model="m1"):
    """Seed a resumable root PLUS its prior (reaped) incarnation linked by
    identity_key, a manifest cwd (existing dir), and a frozen pin on the old
    incarnation — the durable state a real resume reconstructs."""
    d.mint_conversation_identity(
        identity_key=key,
        provider=provider,
        provider_namespace="ns",
        agent_profile="dev",
        model=model,
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=old_terminal,
    )
    d.bind_provider_session_id(key, provider_session_id=uuid, provider_namespace="ns")
    d.set_conversation_lifecycle(key, "hibernated")
    d.upsert_recovery_manifest(key, cwd="/tmp")
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalIdentityModel(
                terminal_id=old_terminal,
                provider=provider,
                base_name=old_terminal,
                lifecycle="reaped",
                identity_key=key,
                provider_session_id=uuid,
                cwd="/tmp",
            )
        )
        # a frozen pin on the OLD incarnation so inherit_pins carries it forward.
        db.add(
            d.AuthorityPinModel(
                task_key=old_terminal,
                file_path="/tmp/AUTH.md",
                sha256="a" * 64,
                version=1,
                registered_by="test",
                frozen=True,
            )
        )


def test_ac1_integrated_positive_resume_publishes_second_incarnation(real_sqlite_env):
    """AC1 positive: a resumable codex root resumes through the PRODUCTION
    prepare→claim→attach/publish seam (fake provider for the spawn). Asserts the
    full recovery contract without a _create_terminal stub around the publish."""
    key, old_t, new_t, uuid = "ac1", "oldaaaa1", "newbbbb1", "cx-ac1-uuid"
    _seed_resumable_with_incarnation(
        key, provider="codex", owner="mb_orig_caller", uuid=uuid, old_terminal=old_t
    )
    # measured PASSING resume evidence so the capability arm admits cleanly.
    d.record_capability_evidence("codex", "resume", "passed")

    # 1) PREPARE (real): resolves root+manifest → ResumeLaunchSpec. Workspace,
    #    model, and pins are reconstructed here — not stubbed.
    prepared = prepare_resume(
        resume_from=key,
        requested_agent_profile=None,
        requested_working_directory=None,
        caller_principal="mb_orig_caller",
    )
    assert prepared["via_identity"] is True
    spec = prepared["launch_spec"]
    assert spec.provider == "codex" and spec.provider_session_id == uuid
    assert spec.model == "m1"  # model retained from the root (M6 re-assert downstream)
    assert prepared["working_directory"] == "/tmp"  # workspace retained
    # pins retained (inherit_pins default True): the old incarnation's frozen pin.
    assert prepared["pins_inherited"] == 1
    assert prepared["authority_files"] == [{"file_path": "/tmp/AUTH.md", "sha256": "a" * 64}]
    assert spec.capability_unverified is None  # passing evidence → verified

    # 2) BLIND CHECKPOINT / CLAIM (real): take the CAS claim before any spawn
    #    effect. A second concurrent claim must then lose.
    admission = authorize_and_classify_resume(d.get_conversation_identity(key), "mb_orig_caller")
    assert admission.ok
    claimed = claim_resume_admission(admission, claimant="sup_recovering")
    assert claimed.ok
    # the root now holds the claim; a second resume attempt refuses.
    result2, create2 = _assign(key, caller="mb_orig_caller")
    _assert_refused(result2, create2, missing="identity", reason="session_resume_in_progress")

    # 3) SPAWN (fake provider): register the NEW incarnation exactly as the
    #    production create path does — a terminal_identity row linked to the SAME
    #    root. No _create_terminal stub wraps the publish below.
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalIdentityModel(
                terminal_id=new_t,
                provider="codex",
                base_name=new_t,
                lifecycle="live",
                identity_key=key,
                cwd="/tmp",
            )
        )

    # 4) PUBLISH/VERIFY (real production seam): the resumed worker reports its id;
    #    attach_captured_uuid runs verify_and_publish_resume (identity match →
    #    publish current_terminal + clear claim).
    out = attach_captured_uuid(new_t, provider_session_id=uuid, provider="codex")
    assert out["status"] == "resume_published", out

    # -- ASSERT the AC1 recovery contract --
    root = d.get_conversation_identity(key)
    # identity verified + published under the SAME uuid (one root).
    assert root["provider_session_id"] == uuid
    assert root["lifecycle"] == "live"
    # current incarnation MOVED to the new terminal; claim cleared.
    assert root["current_terminal_id"] == new_t
    assert root["resume_claim"] is None
    # ONE root / TWO incarnations: both terminal_identity rows share identity_key.
    with d.SessionLocal() as db:
        incs = (
            db.query(TerminalIdentityModel)
            .filter_by(identity_key=key)
            .order_by(TerminalIdentityModel.terminal_id.asc())
            .all()
        )
    assert {i.terminal_id for i in incs} == {old_t, new_t}
    # event timeline records the resume publication (continuity, D5).
    events = [e["event"] for e in d.get_conversation_events(key)]
    assert "resume_claimed" in events and "resume_published" in events
    # CALLBACK CONTINUITY (AC5): a bare callback from the NEW incarnation still
    # routes to the ORIGINAL caller (owner_principal preserved), never the
    # recovering supervisor.
    assert d.resolve_bare_callback_receiver(new_t) == "mb_orig_caller"


def test_ac1_identity_mismatch_does_not_publish(real_sqlite_env):
    """AC7-adjacent: if the resumed worker reports a DIFFERENT id than the root's
    (non-claude), verify FAILS, nothing publishes, the claim is cleared and the
    identity stays retryable — the publish never runs before verification."""
    key, old_t, new_t, uuid = "ac1m", "oldmmmm1", "newmmmm1", "cx-real-uuid"
    _seed_resumable_with_incarnation(
        key, provider="codex", owner="mb_c", uuid=uuid, old_terminal=old_t
    )
    admission = authorize_and_classify_resume(d.get_conversation_identity(key), "mb_c")
    claim_resume_admission(admission, claimant="sup_r")
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalIdentityModel(
                terminal_id=new_t,
                provider="codex",
                base_name=new_t,
                lifecycle="live",
                identity_key=key,
                cwd="/tmp",
            )
        )
    out = attach_captured_uuid(new_t, provider_session_id="WRONG-uuid", provider="codex")
    assert out["status"] == "resume_failed"
    root = d.get_conversation_identity(key)
    # NOT published: current_terminal stays the OLD incarnation, claim cleared.
    assert root["current_terminal_id"] == old_t
    assert root["resume_claim"] is None
    events = [e["event"] for e in d.get_conversation_events(key)]
    assert "resume_failed" in events and "resume_published" not in events
