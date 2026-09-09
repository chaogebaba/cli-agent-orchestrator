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
    recorded owner; a non-owner cannot release."""
    _mk_owned_root("k_rel", owner="mb_owner")
    gen = d.get_conversation_identity("k_rel")["generation"]
    d.claim_resume("k_rel", gen, "claimant")
    # Wrong owner refused.
    assert d.release_resume_claim_owned("k_rel", "mb_intruder")["reason"] == "not_owner"
    assert d.get_conversation_identity("k_rel")["resume_claim"] is not None
    # Correct owner releases.
    assert d.release_resume_claim_owned("k_rel", "mb_owner")["released"] is True
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
