"""F829 Amendment A2 — canonical seat ownership + admitted-root semantics.

Covers AC-A2.1 … AC-A2.11 and the five named mutant guards. Uses the real
sqlite fixture (schema from Base.metadata); the A2.2 owner-backfill migration is
invoked directly against the fixture DB file (init_db's migration registry is
not run by the fixture).

MUTANT MAP (each killed by a NAMED test below):
* swallow-the-mint     → test_mutant_swallow_mint_leaves_no_root_is_caught
* drop-the-token-check → test_ac_a2_8_caller_token_missing / _caller_unverified
* accept-a-None-caller → test_ac_a2_2_authorize_refuses_none_caller
* attach-on-collision  → test_ac_a2_11_seeded_uuid_conflict_refuses
* hand-copied principal in shim → test_ac_a2_7_shim_deleted (grep=0) +
  test_ac_a2_1_principal_is_own_mailbox_not_parent
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients import database as d
from cli_agent_orchestrator.clients.database import (
    MailboxIncarnationModel,
    MailboxModel,
    PrincipalRefused,
    RootAdmission,
    SeededSessionConflict,
    TerminalIdentityModel,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _seed_mailbox(db, mailbox_id, terminal_id, *, generation=1, session="s", role="worker"):
    db.add(
        MailboxModel(
            id=mailbox_id,
            session_name=f"{session}-{mailbox_id}",
            role=f"{role}-{mailbox_id}",
            current_terminal_id=terminal_id,
            generation=generation,
        )
    )
    db.add(
        MailboxIncarnationModel(
            mailbox_id=mailbox_id, generation=generation, terminal_id=terminal_id
        )
    )
    db.flush()


def _seed_incarnation(db, terminal_id, *, provider="codex", identity_key=None):
    row = TerminalIdentityModel(
        terminal_id=terminal_id,
        provider=provider,
        agent_profile="dev",
        cwd="/tmp",
        session_name="s",
        base_name=terminal_id,
        lifecycle="live",
        identity_key=identity_key,
    )
    db.add(row)
    db.flush()


# ==========================================================================
# AC-A2.1 / AC-A2.9 — principal_for_terminal: four discriminated outcomes,
# OWN mailbox never the parent's.
# ==========================================================================
def test_ac_a2_9_mailbox_less_seat_uses_terminal_id(real_sqlite_env):
    S = real_sqlite_env["TestSession"]
    with S() as db:
        r = d.principal_for_terminal("aaaa1111", db=db)
    assert r.ok and r.principal == "aaaa1111"  # (i) mailbox-less seat


def test_ac_a2_9_no_caller_refuses_principal_unavailable(real_sqlite_env):
    S = real_sqlite_env["TestSession"]
    with S() as db:
        r = d.principal_for_terminal(None, db=db)
    assert not r.ok and r.error == "principal_unavailable" and r.retryable  # missing caller


def test_ac_a2_9_own_mailbox_resolved(real_sqlite_env):
    S = real_sqlite_env["TestSession"]
    with S() as db:
        _seed_mailbox(db, "mb_self", "bbbb2222")
        r = d.principal_for_terminal("bbbb2222", db=db)
    assert r.ok and r.principal == "mb_self"


def test_ac_a2_9_inconsistent_membership_refuses(real_sqlite_env):
    """(iv) more than one distinct mailbox across incarnations → refuse
    principal_inconsistent (retryable false). Unreachable via the UNIQUE schema,
    so inject the multi-mailbox lookup result directly (defence-in-depth guard).
    """
    from unittest.mock import patch

    S = real_sqlite_env["TestSession"]

    class _Row:
        def __init__(self, v):
            self._v = v

        def __getitem__(self, i):
            return self._v

    with S() as db:
        with patch.object(db, "query") as q:
            q.return_value.filter.return_value.all.return_value = [_Row("mb_a"), _Row("mb_b")]
            r = d.principal_for_terminal("cccc3333", db=db)
    assert not r.ok and r.error == "principal_inconsistent" and r.retryable is False


def test_ac_a2_1_principal_is_own_mailbox_not_parent(real_sqlite_env):
    """The F857 incident: mint must own the seat by the CALLER's OWN mailbox,
    never by the child seat's parent. principal_for_terminal(caller) resolves the
    caller's own mailbox; it never reads a caller_mailbox_id parent pointer."""
    S = real_sqlite_env["TestSession"]
    with S() as db:
        _seed_mailbox(db, "mb_parent", "parent00")
        _seed_mailbox(db, "mb_child", "child000")
        r = d.principal_for_terminal("child000", db=db)
    assert r.ok and r.principal == "mb_child"  # NOT mb_parent


# ==========================================================================
# AC-A2.1 — mint owns the root by the owner_caller_id's OWN principal.
# ==========================================================================
def test_ac_a2_1_mint_owns_root_by_caller_own_mailbox(real_sqlite_env):
    S = real_sqlite_env["TestSession"]
    with S() as db:
        _seed_mailbox(db, "mb_sup", "sup00001")
        _seed_incarnation(db, "wrk00001", identity_key=None)
        d.mint_spawn_identity(
            identity_key="conv_wrk00001",
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_caller_id="sup00001",
            origin_callback_ref=None,
            current_terminal_id="wrk00001",
            cwd="/tmp",
            db=db,
        )
        db.commit()
    root = d.get_conversation_identity("conv_wrk00001")
    assert root is not None and root["owner_principal"] == "mb_sup"


def test_ac_a2_1_mint_refuses_when_principal_unresolvable(real_sqlite_env):
    """A2.1: an owner_caller_id whose principal cannot be resolved (schema gone)
    raises PrincipalRefused so the create transaction ABORTS — never a
    best-effort write with a bad/absent owner."""
    from unittest.mock import patch

    S = real_sqlite_env["TestSession"]
    with S() as db:
        with patch.object(d, "_mailbox_schema_available", return_value=False):
            with pytest.raises(PrincipalRefused) as ei:
                d.mint_spawn_identity(
                    identity_key="conv_x",
                    provider="codex",
                    provider_namespace="ns",
                    agent_profile="dev",
                    model="m",
                    reasoning_effort=None,
                    owner_caller_id="sup00001",
                    origin_callback_ref=None,
                    current_terminal_id="wrk_x",
                    cwd="/tmp",
                    db=db,
                )
    assert ei.value.result.error == "principal_unavailable"
    assert d.get_conversation_identity("conv_x") is None  # aborted, no root


# ==========================================================================
# AC-A2.1 (rollback) — root + manifest + link commit or roll back together.
# MUTANT: swallow-the-mint.
# ==========================================================================
def test_ac_a2_1_root_manifest_link_atomic_via_create(real_sqlite_env):
    """A fresh spawn through create_terminal mints root + manifest + links the
    incarnation, all in the create transaction."""
    with real_sqlite_env["TestSession"]() as db:
        _seed_mailbox(db, "mb_sup2", "sup00002")
        db.commit()
    d.create_terminal(
        "wrk00002",
        "sess",
        "win",
        "codex",
        agent_profile="dev",
        working_directory="/tmp",
        caller_id="sup00002",
        root_admission=RootAdmission(
            mode="mint",
            identity_key="conv_wrk00002",
            provider="codex",
            provider_namespace="ns",
            owner_caller_id="sup00002",
        ),
    )
    root = d.get_conversation_identity("conv_wrk00002")
    assert root is not None
    assert d.get_recovery_manifest("conv_wrk00002") is not None
    ti = d.get_terminal_identity("wrk00002")
    assert ti["identity_key"] == "conv_wrk00002"  # incarnation→root link


def test_mutant_swallow_mint_leaves_no_root_is_caught(real_sqlite_env):
    """MUTANT (swallow-the-mint): if a mint that raises were swallowed, a
    terminals row would exist with NO root. We assert the raise PROPAGATES out of
    create_terminal (aborting the create), so the mutant that catches it fails
    this test."""
    from unittest.mock import patch

    with real_sqlite_env["TestSession"]() as db:
        _seed_mailbox(db, "mb_sup3", "sup00003")
        db.commit()
    with patch.object(d, "mint_conversation_identity", side_effect=RuntimeError("boom")):
        with pytest.raises(Exception):
            d.create_terminal(
                "wrk00003",
                "sess",
                "win",
                "codex",
                agent_profile="dev",
                working_directory="/tmp",
                caller_id="sup00003",
                root_admission=RootAdmission(
                    mode="mint",
                    identity_key="conv_wrk00003",
                    provider="codex",
                    provider_namespace="ns",
                    owner_caller_id="sup00003",
                ),
            )
    # No terminals row survived the aborted create.
    assert d.get_terminal_metadata("wrk00003") is None


# ==========================================================================
# AC-A2.3 — fresh codex seed root carries the seeded uuid + namespace; a fresh
# spawn mints exactly one root; ordinary fork mints a new root.
# ==========================================================================
def test_ac_a2_3_fresh_codex_seed_root_carries_uuid(real_sqlite_env):
    with real_sqlite_env["TestSession"]() as db:
        _seed_mailbox(db, "mb_sup4", "sup00004")
        db.commit()
    d.create_terminal(
        "cdx00001",
        "sess",
        "win",
        "codex",
        agent_profile="dev",
        working_directory="/tmp",
        caller_id="sup00004",
        provider_session_id="seed-uuid-1",
        root_admission=RootAdmission(
            mode="mint",
            identity_key="conv_cdx00001",
            provider="codex",
            provider_namespace="/home/u/.codex",
            provider_session_id="seed-uuid-1",
            owner_caller_id="sup00004",
        ),
    )
    root = d.get_conversation_identity("conv_cdx00001")
    assert root["provider_session_id"] == "seed-uuid-1"
    assert root["provider_namespace"] == "/home/u/.codex"
    assert root["lifecycle"] == "live"  # never capture_unknown


# ==========================================================================
# AC-A2.11 — a seeded uuid that already has a root REFUSES session_identity_conflict.
# MUTANT: attach-on-collision.
# ==========================================================================
def test_ac_a2_11_seeded_uuid_conflict_refuses(real_sqlite_env):
    with real_sqlite_env["TestSession"]() as db:
        _seed_mailbox(db, "mb_o", "own00001")
        d.mint_conversation_identity(
            identity_key="conv_first",
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_principal="mb_o",
            origin_callback_ref=None,
            current_terminal_id="own00001",
            provider_session_id="shared-uuid",
            db=db,
        )
        db.commit()
    # A DIFFERENT fresh spawn seeding the SAME (provider, ns, uuid) triple:
    with real_sqlite_env["TestSession"]() as db:
        with pytest.raises(SeededSessionConflict) as ei:
            d.mint_conversation_identity(
                identity_key="conv_second",
                provider="codex",
                provider_namespace="ns",
                agent_profile="dev",
                model="m",
                reasoning_effort=None,
                owner_principal="mb_o",
                origin_callback_ref=None,
                current_terminal_id="own00002",
                provider_session_id="shared-uuid",
                db=db,
            )
    assert ei.value.conflicting_identity_key == "conv_first"
    assert d.get_conversation_identity("conv_second") is None  # never minted


# ==========================================================================
# AC-A2.2 — authorize refuses missing caller / owner / mismatch.
# MUTANT: accept-a-None-caller.
# ==========================================================================
def _mk_owned_root(key, owner="mb_owner", lifecycle="hibernated"):
    d.mint_conversation_identity(
        identity_key=key,
        provider="codex",
        provider_namespace="ns",
        agent_profile="dev",
        model="m",
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=f"t_{key}",
        provider_session_id=f"u_{key}",
    )
    d.set_conversation_lifecycle(key, lifecycle)


def test_ac_a2_2_authorize_refuses_none_caller(real_sqlite_env):
    from cli_agent_orchestrator.services.conversation_transition import (
        authorize_and_classify_resume,
    )

    _mk_owned_root("k_none", owner="mb_owner")
    root = d.get_conversation_identity("k_none")
    adm = authorize_and_classify_resume(root, None)  # MUTANT: a None caller must NOT pass
    assert adm.ok is False and adm.error == "resume_not_owner"


def test_ac_a2_2_authorize_refuses_mismatch(real_sqlite_env):
    from cli_agent_orchestrator.services.conversation_transition import (
        authorize_and_classify_resume,
    )

    _mk_owned_root("k_mm", owner="mb_owner")
    root = d.get_conversation_identity("k_mm")
    adm = authorize_and_classify_resume(root, "mb_other")
    assert adm.ok is False and adm.error == "resume_not_owner"


def test_ac_a2_2_authorize_allows_exact_owner(real_sqlite_env):
    from cli_agent_orchestrator.services.conversation_transition import (
        authorize_and_classify_resume,
    )

    _mk_owned_root("k_ok", owner="mb_owner")
    root = d.get_conversation_identity("k_ok")
    adm = authorize_and_classify_resume(root, "mb_owner")
    assert adm.ok is True


# ==========================================================================
# AC-A2.2 — versioned idempotent owner backfill.
# ==========================================================================
def _run_backfill(db_file: Path):
    """Point DATABASE_FILE at the fixture db and run the A2.2 migration."""
    from unittest.mock import patch

    import cli_agent_orchestrator.constants as consts

    with patch.object(consts, "DATABASE_FILE", str(db_file)):
        d._migrate_f829_a2_owner_backfill()


def test_ac_a2_2_backfill_rewrites_bare_owner_to_mailbox_idempotent(real_sqlite_env):
    db_file = real_sqlite_env["db_file"]
    with real_sqlite_env["TestSession"]() as db:
        # A terminal-fallback root: owner is a bare 8-hex terminal id that HAS a
        # terminal_identity incarnation row AND a unique mailbox.
        _seed_incarnation(db, "deadbeef")
        _seed_mailbox(db, "mb_real", "deadbeef")
        d.mint_conversation_identity(
            identity_key="conv_bf",
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_principal="deadbeef",  # bare terminal-id fallback owner
            origin_callback_ref=None,
            current_terminal_id="deadbeef",
            db=db,
        )
        db.commit()
    _run_backfill(db_file)
    assert d.get_conversation_identity("conv_bf")["owner_principal"] == "mb_real"
    # Idempotent: a second run selects nothing (owner is now mb_-prefixed).
    _run_backfill(db_file)
    assert d.get_conversation_identity("conv_bf")["owner_principal"] == "mb_real"


def test_ac_a2_2_backfill_preserves_ambiguous_and_null(real_sqlite_env):
    db_file = real_sqlite_env["db_file"]
    with real_sqlite_env["TestSession"]() as db:
        # NULL owner — preserved for explicit claim.
        d.mint_conversation_identity(
            identity_key="conv_null",
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_principal=None,
            origin_callback_ref=None,
            current_terminal_id="t_null",
            db=db,
        )
        # bare id with NO incarnation row — ambiguous, preserved.
        d.mint_conversation_identity(
            identity_key="conv_amb",
            provider="codex",
            provider_namespace="ns2",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_principal="feedface",
            origin_callback_ref=None,
            current_terminal_id="t_amb",
            db=db,
        )
        db.commit()
    _run_backfill(db_file)
    assert d.get_conversation_identity("conv_null")["owner_principal"] is None
    assert d.get_conversation_identity("conv_amb")["owner_principal"] == "feedface"


def test_ac_a2_2_backfill_skips_active_claim(real_sqlite_env):
    db_file = real_sqlite_env["db_file"]
    with real_sqlite_env["TestSession"]() as db:
        _seed_incarnation(db, "cafe1234")
        _seed_mailbox(db, "mb_c", "cafe1234")
        d.mint_conversation_identity(
            identity_key="conv_claim",
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_principal="cafe1234",
            origin_callback_ref=None,
            current_terminal_id="cafe1234",
            db=db,
        )
        db.commit()
    gen = d.get_conversation_identity("conv_claim")["generation"]
    assert d.claim_resume("conv_claim", gen, "someone") is True
    _run_backfill(db_file)
    # An actively-claimed root is skipped — owner unchanged.
    assert d.get_conversation_identity("conv_claim")["owner_principal"] == "cafe1234"


# ==========================================================================
# AC-A2.10 — a raw fork_context mode=resume WITHOUT admission → resume_not_admitted.
# (The deprecated fork_from+resume translation reaching the same admission is
# asserted at the shim layer in test_assign_resume_from.py.)
# ==========================================================================
def test_ac_a2_10_missing_root_resume_refuses_not_admitted(real_sqlite_env):
    from cli_agent_orchestrator.services.resume_service import ResumeRefused, prepare_resume

    with pytest.raises(ResumeRefused) as ei:
        prepare_resume(
            resume_from="no-such-handle",
            requested_agent_profile="dev",
            requested_working_directory="/tmp",
            caller_principal="mb_owner",
        )
    assert ei.value.missing == "identity"
    assert ei.value.reason == "resume_not_admitted"
    assert ei.value.retryable is False


# ==========================================================================
# AC-A2.7 — the hot-fix shim is gone (grep = 0 in src).
# MUTANT: hand-copied principal in shim.
# ==========================================================================
def test_ac_a2_7_shim_deleted():
    root = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"
    hits = subprocess.run(
        ["grep", "-rn", "_f829_resolve_caller_principal", str(root)],
        capture_output=True,
        text=True,
    )
    assert hits.stdout.strip() == "", f"shim still referenced:\n{hits.stdout}"


# ==========================================================================
# AC-A2.8 — caller authentication, two distinct arms with distinct reasons AND
# retryable values. MUTANT: drop-the-token-check.
# ==========================================================================
class _FakeRequest:
    def __init__(self, token=None):
        self.headers = {} if token is None else {"x-cao-terminal-token": token}


def test_ac_a2_8_caller_token_missing(real_sqlite_env):
    """No X-CAO-Terminal-Token → caller_token_missing (retryable TRUE), a caller
    CONFIGURATION fault, never spoofing."""
    from fastapi import HTTPException

    from cli_agent_orchestrator.api.main import _f829_verify_caller_binding

    with pytest.raises(HTTPException) as ei:
        _f829_verify_caller_binding(_FakeRequest(token=None), "sup00001")
    detail = ei.value.detail
    assert detail["reason"] == "caller_token_missing"
    assert detail["retryable"] is True
    assert detail["missing"] == "identity"


def test_ac_a2_8_caller_unverified(real_sqlite_env):
    """A token that does not verify caller_id → caller_unverified (retryable
    FALSE). A build emitting one token for both arms fails: this asserts the
    DISTINCT reason + retryable."""
    from unittest.mock import patch

    from fastapi import HTTPException

    from cli_agent_orchestrator.api.main import _f829_verify_caller_binding

    with patch(
        "cli_agent_orchestrator.services.terminal_token_service.verify_sender_token",
        return_value=(False, "mismatch"),
    ):
        with pytest.raises(HTTPException) as ei:
            _f829_verify_caller_binding(_FakeRequest(token="wrong-token"), "sup00001")
    detail = ei.value.detail
    assert detail["reason"] == "caller_unverified"
    assert detail["retryable"] is False
    # The two arms carry DIFFERENT reasons and DIFFERENT retryable values.
    assert detail["reason"] != "caller_token_missing"


def test_ac_a2_8_verified_caller_passes(real_sqlite_env):
    from unittest.mock import patch

    from cli_agent_orchestrator.api.main import _f829_verify_caller_binding

    with patch(
        "cli_agent_orchestrator.services.terminal_token_service.verify_sender_token",
        return_value=(True, None),
    ):
        # No exception → bound.
        _f829_verify_caller_binding(_FakeRequest(token="good"), "sup00001")


# ==========================================================================
# AC-A2.5 — a resume claim is released on ANY post-claim failure (no leaked claim).
# ==========================================================================
def test_ac_a2_5_claim_released_on_post_claim_failure(real_sqlite_env):
    """The claim compensator (clear_resume_claim) clears a held claim so a failed
    spawn leaves NO claim — diag shows none. resumed_by is never stamped."""
    _mk_owned_root("k_comp", owner="mb_owner")
    gen = d.get_conversation_identity("k_comp")["generation"]
    assert d.claim_resume("k_comp", gen, "claimant") is True
    assert d.get_conversation_identity("k_comp")["resume_claim"] is not None
    # Simulate the api create-path compensator on a post-claim failure.
    d.clear_resume_claim("k_comp", event="resume_failed")
    assert d.get_conversation_identity("k_comp")["resume_claim"] is None
    events = [e["event"] for e in d.get_conversation_events("k_comp")]
    assert "resume_failed" in events
    # resumed_by is never stamped for a failed resume (no publish happened).
    assert "resume_published" not in events


def test_ac_a2_5_release_verb_is_owner_guarded(real_sqlite_env):
    """cao identity release <key> --owner: clears a leaked claim ONLY for the
    recorded owner; a non-owner cannot release. A leaked claim is a DEAD claimant
    (past the AC4 TTL) — modelled here with claim_ttl_s=0.0 so the interlock
    treats it as releasable."""
    _mk_owned_root("k_rel", owner="mb_owner")
    gen = d.get_conversation_identity("k_rel")["generation"]
    d.claim_resume("k_rel", gen, "claimant")
    # Wrong owner refused (owner check precedes the liveness interlock).
    assert (
        d.release_resume_claim_owned("k_rel", "mb_intruder", claim_ttl_s=0.0)["reason"]
        == "not_owner"
    )
    assert d.get_conversation_identity("k_rel")["resume_claim"] is not None
    # Correct owner releases a dead/leaked claim.
    assert d.release_resume_claim_owned("k_rel", "mb_owner", claim_ttl_s=0.0)["released"] is True
    assert d.get_conversation_identity("k_rel")["resume_claim"] is None


# ==========================================================================
# AC-A2.4 — honest reap taxonomy: root/link integrity gates resumable:true, and
# a COLD reap of a rootless terminal is NOT blocked (boundary: admission is
# resume-only).
# ==========================================================================
def test_ac_a2_4_cold_reap_of_rootless_terminal_is_allowed(real_sqlite_env):
    """Boundary: evaluate_planned_hibernate on a terminal with NO F829 root
    ALLOWS the ordinary reap (admission is resume-only; the cold cascade keeps
    its pre-A2 behaviour)."""
    from cli_agent_orchestrator.services.conversation_transition import (
        evaluate_planned_hibernate,
    )

    with real_sqlite_env["TestSession"]() as db:
        _seed_incarnation(db, "rootless1", identity_key=None)
        db.commit()
    dec = evaluate_planned_hibernate("rootless1")
    assert dec.allowed is True and dec.lifecycle is None


# ==========================================================================
# AC-A2.3 (r2 M-1) — the named mutant "drop the namespace on the seeded root".
# The other A2.3 test hands create_terminal a ready-made RootAdmission, so it
# never exercises the SERVICE construction site (terminal_service.py:2383-2397).
# This probe (supplied verbatim by the EMPIRICAL r1 verdict) drives the REAL
# create path with the db writer mocked and asserts the admission the SERVICE
# built carries the seeded uuid AND the non-NULL wrapper namespace — so
# substituting provider_namespace=None at the construction site is KILLED here.
# ==========================================================================
@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
@patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
@patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
@patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
@patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
@patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
@patch("cli_agent_orchestrator.backends.registry._backend")
@patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
@patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
@patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
@patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
async def test_ac_a2_3_service_built_seed_admission_carries_namespace(
    mock_load_profile,
    mock_gen_id,
    mock_gen_session,
    mock_gen_window,
    mock_tmux,
    mock_db_create,
    mock_provider_manager,
    mock_fifo_dir,
    mock_fifo_manager,
    mock_status_monitor,
    mock_delete,
):
    from unittest.mock import AsyncMock

    from cli_agent_orchestrator.models.terminal import ForkContext
    from cli_agent_orchestrator.providers.codex import _resolved_codex_home
    from cli_agent_orchestrator.services.terminal_service import create_terminal
    from cli_agent_orchestrator.utils.agent_profiles import AgentProfile

    mock_gen_id.return_value = "cdx01234"
    mock_gen_session.return_value = "cao-session"
    mock_gen_window.return_value = "dev-abcd"
    mock_tmux.session_exists.return_value = False
    mock_load_profile.return_value = AgentProfile(name="dev", description="d")
    mock_provider = AsyncMock()
    mock_provider.initialize.return_value = True
    mock_provider_manager.create_provider.return_value = mock_provider
    mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

    fc = ForkContext(
        mode="resume",
        session_uuid="seed-uuid-probe",
        base_name="seed",
        provider="codex",
        initial_preamble="",
    )
    await create_terminal("codex", "dev", new_session=True, fork_context=fc)

    adm = mock_db_create.call_args.kwargs["root_admission"]
    assert adm is not None and adm.mode == "mint"
    assert adm.provider_session_id == "seed-uuid-probe"
    expected_ns = str(_resolved_codex_home("cdx01234")).rstrip("/")
    # Mutant kill: provider_namespace=None at the construction site fails HERE.
    assert adm.provider_namespace == expected_ns, (
        f"seeded root namespace {adm.provider_namespace!r} != wrapper "
        f"_resolved_codex_home {expected_ns!r}"
    )
    assert adm.provider_namespace is not None


# ==========================================================================
# F865 R1 — folds of the 9 A2 acceptance-wall gaps (issue #721).
#
# CODE fixes (fail-before / pass-after):
#   * B3 → test_f865_b3_null_owner_root_not_advertised_resumable
#   * S3 → test_f865_s3_resumed_by_stamped_only_at_publish
#          test_f865_s3_spawn_failure_after_claim_leaves_no_resumed_by
# TEST-ONLY pins (behaviour already correct, previously unpinned):
#   * B4 → test_f865_b4_release_plane_is_owner_cas_not_terminal_token
#   * B6 → test_f865_b6_stored_namespace_is_authoritative_not_re_resolved
#   * S4 → test_f865_s4_diag_distinguishes_all_five_states
# ==========================================================================


def _seed_reap_root_and_incarnation(db_mod, *, identity_key, terminal_id, owner, uuid=None):
    """A codex conversation root + its live incarnation, as after a fresh spawn.

    ``owner=None`` mints a NULL-owner (legacy_unknown_owner-shaped) root; resume
    of such a root is refused ``resume_not_owner`` until an explicit claim.
    """
    db_mod.mint_conversation_identity(
        identity_key=identity_key,
        provider="codex",
        provider_namespace="/home/u/.codex",
        agent_profile="dev",
        model="m",
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=terminal_id,
        provider_session_id=uuid,
    )
    with db_mod.SessionLocal.begin() as s:
        s.add(
            db_mod.TerminalIdentityModel(
                terminal_id=terminal_id,
                provider="codex",
                agent_profile="dev",
                cwd="/tmp",
                session_name="cao-test",
                provider_session_id=uuid,
                base_name=terminal_id,
                lifecycle="live",
                identity_key=identity_key,
            )
        )


# --------------------------------------------------------------------------
# B3 (CODE): a NULL-owner root passes root/link integrity but resume refuses it
# (resume_not_owner), so advertising resumable:true for it is DISHONEST. The
# reap resolver must report resumable=False, reason=identity_owner_unknown.
# MUTANT: drop-the-owner-check (revert to root-link-only) → this test fails.
# --------------------------------------------------------------------------
def test_f865_b3_null_owner_root_not_advertised_resumable(real_sqlite_env):
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services.resume_service import provider_supports_resume

    assert provider_supports_resume("codex") is True
    _seed_reap_root_and_incarnation(
        d, identity_key="conv_b3_null", terminal_id="b3null01", owner=None, uuid="uuid-b3-null"
    )
    cap_id, resumable, reason = ts._resolve_reap_resume_key(
        "b3null01", {"working_directory": "/tmp"}, force=False
    )
    # Dishonesty guard: a NULL-owner root is NOT resumable through assign(resume_from).
    assert resumable is False, (cap_id, reason)
    assert reason == "identity_owner_unknown", reason


def test_f865_b3_owned_root_still_advertised_resumable(real_sqlite_env):
    """Control: an OWNED root with a captured id remains honestly resumable
    (the B3 fix narrows only the NULL-owner case, nothing else)."""
    from cli_agent_orchestrator.services import terminal_service as ts

    _seed_reap_root_and_incarnation(
        d, identity_key="conv_b3_own", terminal_id="b3own001", owner="mb_owner", uuid="uuid-b3-own"
    )
    cap_id, resumable, reason = ts._resolve_reap_resume_key(
        "b3own001", {"working_directory": "/tmp"}, force=False
    )
    assert resumable is True and reason == "resumable", (cap_id, reason)


def test_f865_b3_claimed_null_owner_root_is_resumable_after_claim(real_sqlite_env):
    """The recovery path B3 points at: `cao identity claim` sets the owner, after
    which the SAME root is honestly advertised resumable."""
    from cli_agent_orchestrator.services import terminal_service as ts

    _seed_reap_root_and_incarnation(
        d, identity_key="conv_b3_clm", terminal_id="b3clm001", owner=None, uuid="uuid-b3-clm"
    )
    # Before claim: not resumable.
    _, resumable_before, _ = ts._resolve_reap_resume_key(
        "b3clm001", {"working_directory": "/tmp"}, force=False
    )
    assert resumable_before is False
    # Owner claims → now resumable.
    assert d.claim_identity_owner("conv_b3_clm", "mb_late_owner")["status"] == "claimed"
    _, resumable_after, reason_after = ts._resolve_reap_resume_key(
        "b3clm001", {"working_directory": "/tmp"}, force=False
    )
    assert resumable_after is True and reason_after == "resumable"


# --------------------------------------------------------------------------
# S3 (CODE): resumed_by is stamped ONLY once the incarnation row exists (at
# publish), never at claim. A spawn that fails after the claim leaves NO
# resumed_by. MUTANT: stamp-resumed_by-at-claim → the "after claim only"
# assertion below fails.
# --------------------------------------------------------------------------
def test_f865_s3_resumed_by_stamped_only_at_publish(real_sqlite_env):
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mk_owned_root("k_s3_pub", owner="mb_owner")
    root = d.get_conversation_identity("k_s3_pub")
    adm = ct.authorize_and_classify_resume(root, "mb_owner")
    adm = ct.claim_resume_admission(adm, "mb_recoverer")
    assert adm.ok
    # AFTER CLAIM, BEFORE PUBLISH: no resumed_by yet (the incarnation row does
    # not exist). This is the exact assertion the stamp-at-claim mutant fails.
    events = [e["event"] for e in d.get_conversation_events("k_s3_pub")]
    assert "resume_claimed" in events
    assert "resumed_by" not in events, "resumed_by stamped before the incarnation existed"
    # PUBLISH (the incarnation row now exists) → resumed_by is stamped, carrying
    # the recovering principal (never overwriting owner_principal).
    vr = ct.verify_and_publish_resume(
        adm,
        terminal_id="t_s3_new",
        reported_session_id=root["provider_session_id"],
        provider="codex",
    )
    assert vr.ok
    evs = d.get_conversation_events("k_s3_pub")
    names = [e["event"] for e in evs]
    assert "resume_published" in names and "resumed_by" in names
    # owner_principal is untouched: a bare callback still routes to the ORIGINAL
    # caller, never the recoverer.
    assert d.get_conversation_identity("k_s3_pub")["owner_principal"] == "mb_owner"


def test_f865_s3_spawn_failure_after_claim_leaves_no_resumed_by(real_sqlite_env):
    """A2.5 + S3: a post-claim spawn failure (verify mismatch) clears the claim
    and leaves NEITHER resume_published NOR resumed_by."""
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mk_owned_root("k_s3_fail", owner="mb_owner")
    root = d.get_conversation_identity("k_s3_fail")
    adm = ct.claim_resume_admission(ct.authorize_and_classify_resume(root, "mb_owner"), "mb_rec")
    assert adm.ok
    # The resumed worker reports the WRONG id → verify fails, claim cleared.
    vr = ct.verify_and_publish_resume(
        adm, terminal_id="t_s3_fail", reported_session_id="WRONG-ID", provider="codex"
    )
    assert vr.ok is False
    names = [e["event"] for e in d.get_conversation_events("k_s3_fail")]
    assert "resume_failed" in names
    assert "resume_published" not in names
    assert "resumed_by" not in names, "a failed spawn left a stale resumed_by"
    assert d.get_conversation_identity("k_s3_fail")["resume_claim"] is None


def test_f865_s3_resumed_by_recovered_from_event_at_capture_convergence(real_sqlite_env):
    """The real flow rebuilds the admission from the root at the D4 capture
    convergence (attach_captured_uuid), so claimed_by is not on that admission.
    resumed_by must still be stamped, recovered from the durable resume_claimed
    event's claimant."""
    from cli_agent_orchestrator.services.conversation_transition import (
        attach_captured_uuid,
        authorize_and_classify_resume,
        claim_resume_admission,
    )

    _mk_owned_root("k_s3_cap", owner="mb_owner")
    root = d.get_conversation_identity("k_s3_cap")
    claim_resume_admission(authorize_and_classify_resume(root, "mb_owner"), "mb_recoverer")
    # SPAWN: register the NEW incarnation linked to the SAME root, exactly as the
    # production create path does before capture.
    from cli_agent_orchestrator.clients.database import TerminalIdentityModel

    with d.SessionLocal.begin() as db:
        db.add(
            TerminalIdentityModel(
                terminal_id="t_s3_cap_new",
                provider="codex",
                base_name="t_s3_cap_new",
                lifecycle="live",
                identity_key="k_s3_cap",
                cwd="/tmp",
            )
        )
    # The reported id matches the root's uuid → publish via the capture seam.
    out = attach_captured_uuid(
        "t_s3_cap_new", provider_session_id=root["provider_session_id"], provider="codex"
    )
    assert out["status"] == "resume_published", out
    evs = d.get_conversation_events("k_s3_cap")
    resumed = [e for e in evs if e["event"] == "resumed_by"]
    assert resumed, "resumed_by not stamped at the capture convergence"


# --------------------------------------------------------------------------
# B4 (TEST-ONLY): the `cao identity release` auth plane is the OPERATOR /
# owner-CAS plane (--owner compare-and-set against the recorded owner), NOT
# A2.1's terminal-token binding. The review's proposed terminal-token fix would
# break the leaked-claim recovery this verb exists for: a leaked claim is
# exactly when the owning terminal is GONE and cannot present a token. This test
# pins that release works with NO terminal token / no live owning terminal.
# --------------------------------------------------------------------------
def test_f865_b4_release_plane_is_owner_cas_not_terminal_token(real_sqlite_env):
    _mk_owned_root("k_b4", owner="mb_owner")
    gen = d.get_conversation_identity("k_b4")["generation"]
    d.claim_resume("k_b4", gen, "leaked-claimant")
    assert d.get_conversation_identity("k_b4")["resume_claim"] is not None
    # No X-CAO-Terminal-Token, no live owning terminal — the owner principal
    # alone (compare-and-set) releases. This is the operator plane the blueprint
    # should name; the terminal-token plane would be UNSATISFIABLE here. A leaked
    # claim is a DEAD claimant (past the AC4 TTL) — claim_ttl_s=0.0 models that.
    out = d.release_resume_claim_owned("k_b4", "mb_owner", claim_ttl_s=0.0)
    assert out.get("released") is True
    assert d.get_conversation_identity("k_b4")["resume_claim"] is None
    # And a non-owner principal is refused even with everything else identical.
    d.claim_resume("k_b4", d.get_conversation_identity("k_b4")["generation"], "again")
    assert (
        d.release_resume_claim_owned("k_b4", "mb_not_owner", claim_ttl_s=0.0)["reason"]
        == "not_owner"
    )


# --------------------------------------------------------------------------
# B6 (TEST-ONLY): the namespace RECORDED at mint is authoritative — every later
# check compares the STORED value, never a fresh resolution. This pins the three
# properties the r3/r4 folds bought (non-NULL, canonical single-producer,
# compare-against-stored) that AC-A2.3 did not assert.
# --------------------------------------------------------------------------
def test_f865_b6_stored_namespace_is_authoritative_not_re_resolved(real_sqlite_env):
    with real_sqlite_env["TestSession"]() as db:
        _seed_mailbox(db, "mb_b6", "supb6001")
        db.commit()
    d.create_terminal(
        "cdxb6001",
        "sess",
        "win",
        "codex",
        agent_profile="dev",
        working_directory="/tmp",
        caller_id="supb6001",
        provider_session_id="seed-uuid-b6",
        root_admission=RootAdmission(
            mode="mint",
            identity_key="conv_b6",
            provider="codex",
            provider_namespace="/home/u/.codex",
            provider_session_id="seed-uuid-b6",
            owner_caller_id="supb6001",
        ),
    )
    root = d.get_conversation_identity("conv_b6")
    # (1) non-NULL — never leaves D1's UNIQUE triple inert.
    assert root["provider_namespace"] is not None
    stored_ns = root["provider_namespace"]
    # (3) compare-against-STORED: authorize/classify reads the stored namespace
    # onto the admission verbatim (a fresh resolution is never substituted).
    from cli_agent_orchestrator.services.conversation_transition import (
        authorize_and_classify_resume,
    )

    d.set_conversation_lifecycle("conv_b6", "hibernated")
    adm = authorize_and_classify_resume(d.get_conversation_identity("conv_b6"), "mb_b6")
    assert adm.ok and adm.provider_namespace == stored_ns


# --------------------------------------------------------------------------
# S4 (TEST-ONLY): `cao identity diag` distinguishes all FIVE A2.4 states, never
# a manufactured root/owner. AC-A2.4 lists only three; this pins all five.
# --------------------------------------------------------------------------
def test_f865_s4_diag_distinguishes_all_five_states(real_sqlite_env):
    import json

    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.identity import identity_diag
    from cli_agent_orchestrator.clients.database import get_conversation_identity as _gci

    runner = CliRunner()

    def _diag(identifier):
        res = runner.invoke(identity_diag, [identifier, "--json"])
        assert res.exit_code == 0, res.output
        return json.loads(res.output)

    # 1. unknown_handle — nothing at all.
    assert _diag("nohandle0")["state"] == "unknown_handle"

    # 2. incarnation_present_root_absent — a registered incarnation, no root link.
    with real_sqlite_env["TestSession"]() as db:
        _seed_incarnation(db, "s4inc001", identity_key=None)
        db.commit()
    assert _diag("s4inc001")["state"] == "incarnation_present_root_absent"

    # 3. dangling_root_link — incarnation links to a root that does not exist.
    with real_sqlite_env["TestSession"]() as db:
        _seed_incarnation(db, "s4dng001", identity_key="conv_missing_root")
        db.commit()
    assert _diag("s4dng001")["state"] == "dangling_root_link"

    # 4. root_present_owner_unknown — a NULL-owner root (claimable).
    _seed_reap_root_and_incarnation(
        d, identity_key="conv_s4_null", terminal_id="s4nul001", owner=None, uuid="u-s4-null"
    )
    assert _diag("conv_s4_null")["state"] == "root_present_owner_unknown"

    # 5. binding_or_artifact_missing — owned root with no captured uuid.
    _seed_reap_root_and_incarnation(
        d, identity_key="conv_s4_bind", terminal_id="s4bnd001", owner="mb_o", uuid=None
    )
    assert _gci("conv_s4_bind")["provider_session_id"] is None
    assert _diag("conv_s4_bind")["state"] == "binding_or_artifact_missing"


# ==========================================================================
# F865 R2 — Opus DESIGN-delta folds (blueprint 41ca40cd: A2.5 release interlock
# + liveness refusal; A2.2 no owner-override claim + operator backfill entry
# point; AC-A2.9 audit-record separation).
#
# CODE (fail-before/pass-after + named mutants):
#   * A2.5 release interlock →
#       test_f865_r2_release_refuses_live_claimant
#       test_f865_r2_release_quiesces_dead_claimant
#       test_f865_r2_release_never_rewrites_owner_or_stamps_resumed_by
#     mutants: skip-the-liveness-check; rewrite-owner-on-release
#   * AC-A2.9 audit separation →
#       test_f865_r2_principal_states_ii_iii_separated_in_audit
# ADDED surface + test:
#   * A2.2 operator backfill entry point →
#       test_f865_r2_operator_backfill_entry_point_recovers_skipped_root
#   * A2.2 no owner-override on claim →
#       test_f865_r2_claim_has_no_owner_override_mode
# ==========================================================================


def _claim_with_age(identity_key: str, claimant: str, *, age_seconds: float) -> None:
    """Take a resume claim, then backdate resume_claim_at by age_seconds so the
    AC4 TTL interlock can be exercised deterministically."""
    import datetime as _dt

    gen = d.get_conversation_identity(identity_key)["generation"]
    assert d.claim_resume(identity_key, gen, claimant) is True
    backdated = d._utcnow() - _dt.timedelta(seconds=age_seconds)
    with d.SessionLocal.begin() as db:
        db.query(d.ConversationIdentityModel).filter_by(identity_key=identity_key).update(
            {d.ConversationIdentityModel.resume_claim_at: backdated}, synchronize_session=False
        )


# --------------------------------------------------------------------------
# A2.5 release interlock — REFUSE a live/uncertain claimant (within TTL).
# MUTANT: skip-the-liveness-check → this test fails (a live claim is released).
# --------------------------------------------------------------------------
def test_f865_r2_release_refuses_live_claimant(real_sqlite_env):
    _mk_owned_root("k_r2_live", owner="mb_owner")
    # A fresh claim (age 0) is well within the TTL → live/uncertain.
    _claim_with_age("k_r2_live", "claimant", age_seconds=0)
    out = d.release_resume_claim_owned("k_r2_live", "mb_owner", claim_ttl_s=600.0)
    assert out["released"] is False and out["reason"] == "claimant_live", out
    # The claim is left INTACT — release never raced a resume in flight.
    assert d.get_conversation_identity("k_r2_live")["resume_claim"] is not None
    names = [e["event"] for e in d.get_conversation_events("k_r2_live")]
    assert "claim_released_by_owner" not in names


def test_f865_r2_release_quiesces_dead_claimant(real_sqlite_env):
    """A claim PAST the TTL is a confirmed-dead claimant: quiesced
    (claim_reconciled) then released (claim_released_by_owner)."""
    _mk_owned_root("k_r2_dead", owner="mb_owner")
    _claim_with_age("k_r2_dead", "dead-claimant", age_seconds=1200)
    out = d.release_resume_claim_owned("k_r2_dead", "mb_owner", claim_ttl_s=600.0)
    assert out["released"] is True, out
    assert d.get_conversation_identity("k_r2_dead")["resume_claim"] is None
    names = [e["event"] for e in d.get_conversation_events("k_r2_dead")]
    assert "claim_reconciled" in names  # quiesced first
    assert "claim_released_by_owner" in names  # then released


def test_f865_r2_release_never_rewrites_owner_or_stamps_resumed_by(real_sqlite_env):
    """Release clears the claim ONLY: owner_principal is untouched, resumed_by is
    never stamped, and the release is audited as a conversation event."""
    _mk_owned_root("k_r2_inv", owner="mb_owner")
    _claim_with_age("k_r2_inv", "dead", age_seconds=1200)
    out = d.release_resume_claim_owned("k_r2_inv", "mb_owner", claim_ttl_s=600.0)
    assert out["released"] is True
    root = d.get_conversation_identity("k_r2_inv")
    assert root["owner_principal"] == "mb_owner"  # NEVER rewritten
    assert root["resume_claim"] is None
    names = [e["event"] for e in d.get_conversation_events("k_r2_inv")]
    assert "claim_released_by_owner" in names  # audit event written
    assert "resumed_by" not in names  # never stamped by release


# --------------------------------------------------------------------------
# A2.2 — `cao identity claim` has NO owner-override mode: --owner on an
# already-owned (non-legacy) root refuses already_owned.
# --------------------------------------------------------------------------
def test_f865_r2_claim_has_no_owner_override_mode(real_sqlite_env):
    _mk_owned_root("k_r2_owned", owner="mb_owner")
    # An operator trying to override the owner via claim --owner is refused.
    res = d.claim_identity_owner("k_r2_owned", "mb_usurper")
    assert res["status"] == "already_owned"
    assert res["owner_principal"] == "mb_owner"
    assert d.get_conversation_identity("k_r2_owned")["owner_principal"] == "mb_owner"
    # And the CLI surfaces already_owned (no --owner override path exists).
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.identity import identity_claim

    res_cli = CliRunner().invoke(identity_claim, ["k_r2_owned", "--owner", "mb_usurper"])
    assert res_cli.exit_code != 0
    assert "already owned" in res_cli.output


# --------------------------------------------------------------------------
# A2.2 — the explicit OPERATOR backfill entry point recovers a terminal-fallback
# root that the once-at-start migration SKIPPED (active claim then): release the
# stuck claim, then re-run the backfill through the operator command.
# --------------------------------------------------------------------------
def test_f865_r2_operator_backfill_entry_point_recovers_skipped_root(real_sqlite_env):
    db_file = real_sqlite_env["db_file"]
    from unittest.mock import patch

    import cli_agent_orchestrator.constants as consts

    with real_sqlite_env["TestSession"]() as db:
        _seed_incarnation(db, "beefcafe")
        _seed_mailbox(db, "mb_recovered", "beefcafe")
        d.mint_conversation_identity(
            identity_key="conv_r2_bf",
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model="m",
            reasoning_effort=None,
            owner_principal="beefcafe",  # bare terminal-id fallback owner
            origin_callback_ref=None,
            current_terminal_id="beefcafe",
            db=db,
        )
        db.commit()
    # Simulate the once-at-start run skipping it because a claim was active.
    gen = d.get_conversation_identity("conv_r2_bf")["generation"]
    d.claim_resume("conv_r2_bf", gen, "some-claimant")
    with patch.object(consts, "DATABASE_FILE", str(db_file)):
        d._migrate_f829_a2_owner_backfill()
    assert d.get_conversation_identity("conv_r2_bf")["owner_principal"] == "beefcafe"  # skipped
    # Recovery path: release the stuck claim (dead), then re-run via the operator
    # entry point — NOT a claim owner-override.
    # Backdate the claim so release treats it as dead.
    import datetime as _dt

    with d.SessionLocal.begin() as db:
        db.query(d.ConversationIdentityModel).filter_by(identity_key="conv_r2_bf").update(
            {
                d.ConversationIdentityModel.resume_claim_at: d._utcnow()
                - _dt.timedelta(seconds=1200)
            },
            synchronize_session=False,
        )
    assert d.release_resume_claim_owned("conv_r2_bf", "beefcafe", claim_ttl_s=600.0)["released"]
    with patch.object(consts, "DATABASE_FILE", str(db_file)):
        tally = d.run_owner_backfill_operator()
    assert tally["backfilled"] >= 1, tally
    assert d.get_conversation_identity("conv_r2_bf")["owner_principal"] == "mb_recovered"
    # Idempotent: a second operator run rewrites nothing.
    with patch.object(consts, "DATABASE_FILE", str(db_file)):
        tally2 = d.run_owner_backfill_operator()
    assert tally2["backfilled"] == 0


# --------------------------------------------------------------------------
# AC-A2.9 — states (ii) schema-unavailable and (iii) lookup-failed share the
# coarse error (principal_unavailable / retryable true) and are separated ONLY
# in the audit record.
# --------------------------------------------------------------------------
def test_f865_r2_principal_states_ii_iii_separated_in_audit(real_sqlite_env):
    from unittest.mock import patch

    S = real_sqlite_env["TestSession"]
    # (ii) schema unavailable.
    with S() as db:
        with patch.object(d, "_mailbox_schema_available", return_value=False):
            r_ii = d.principal_for_terminal("term_ii00", db=db)
    # (iii) lookup raised.
    with S() as db:
        with patch.object(db, "query", side_effect=RuntimeError("db boom")):
            r_iii = d.principal_for_terminal("term_iii0", db=db)
    # Same COARSE outcome for both...
    assert r_ii.error == r_iii.error == "principal_unavailable"
    assert r_ii.retryable is True and r_iii.retryable is True
    # ...separated ONLY in the audit record.
    assert r_ii.audit == "schema_unavailable"
    assert r_iii.audit == "lookup_failed"
    assert r_ii.audit != r_iii.audit
