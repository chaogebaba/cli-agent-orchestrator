"""F829 acceptance tests — durable session UUID + hibernate/resume.

Maps the blueprint AC1-AC8 arms to unit tests over the real-sqlite fixture.
Provider-live arms (AC1's actual crash/resume of a real CLI) are covered by the
D9 probe report (`/data/cao-scratch/briefs/f829-d9-probe.md`) and marked live;
here we test every DB/service-level invariant the build must uphold.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from cli_agent_orchestrator.clients import database as d
from cli_agent_orchestrator.clients.database import (
    AuthorityPinModel,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    ConversationIdentityModel,
    DeliveryLedgerModel,
    InboxModel,
    MailboxModel,
    TerminalIdentityModel,
    TerminalModel,
    WarmIntentModel,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _mkroot(key, provider, owner, lifecycle, terminal, *, uuid=None, ns="ns", model=None):
    d.mint_conversation_identity(
        identity_key=key,
        provider=provider,
        provider_namespace=ns,
        agent_profile="dev",
        model=model,
        reasoning_effort=None,
        owner_principal=owner,
        origin_callback_ref=None,
        current_terminal_id=terminal,
    )
    d.set_conversation_lifecycle(key, lifecycle)
    if uuid:
        d.bind_provider_session_id(key, provider_session_id=uuid, provider_namespace=ns)
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalIdentityModel(
                terminal_id=terminal,
                provider=provider,
                base_name=terminal,
                lifecycle="reaped",
                identity_key=key,
                provider_session_id=uuid,
            )
        )


# --------------------------------------------------------------------------
# D1 — migration + collapse rule + CAS + bound-uniqueness
# --------------------------------------------------------------------------


def _seed_legacy(db_file):
    conn = sqlite3.connect(db_file)
    conn.execute("DROP TABLE IF EXISTS conversation_identity")
    conn.execute("DROP TABLE IF EXISTS conversation_event")
    conn.execute("DROP TABLE IF EXISTS terminal_identity")
    conn.execute(
        "CREATE TABLE terminal_identity ("
        "terminal_id VARCHAR PRIMARY KEY, provider VARCHAR NOT NULL, agent_profile VARCHAR, "
        "cwd VARCHAR, session_name VARCHAR, provider_session_id VARCHAR, base_name VARCHAR NOT NULL, "
        "retained_persona_home VARCHAR, lifecycle VARCHAR DEFAULT 'live' NOT NULL, git_sha VARCHAR, "
        "dirty_hashes TEXT, created_at DATETIME, reaped_at DATETIME, "
        "CONSTRAINT ck_terminal_identity_lifecycle CHECK (lifecycle IN ('live','reaped')))"
    )
    # terminals already exists (real schema, 35 cols) from create_all; insert
    # the one live row with explicit columns. terminal_identity is dropped +
    # recreated in the LEGACY shape (no identity_key) to exercise the rebuild.
    conn.execute(
        "INSERT INTO terminals (id, tmux_session, tmux_window, provider, caller_mailbox_id) "
        "VALUES ('live1','s','w','codex','mb_owner1')"
    )
    conn.execute(
        "INSERT INTO terminal_identity (terminal_id,provider,base_name,provider_session_id,lifecycle,created_at) "
        "VALUES ('live1','codex','live1','uuid-live-1','live','2026-01-01 00:00:00')"
    )
    # duplicate codex pair (same uuid, both reaped) -> ONE root, 2 incarnations
    for tid, ts in (("dupA", "10:00:00"), ("dupB", "10:32:00")):
        conn.execute(
            "INSERT INTO terminal_identity (terminal_id,provider,base_name,provider_session_id,lifecycle,created_at) "
            f"VALUES ('{tid}','codex','{tid}','uuid-dup','reaped','2026-01-01 {ts}')"
        )
    # reaped row sharing a uuid with the live root -> ATTACH (S1)
    conn.execute(
        "INSERT INTO terminal_identity (terminal_id,provider,base_name,provider_session_id,lifecycle,created_at) "
        "VALUES ('attach1','codex','attach1','uuid-live-1','reaped','2026-01-01 09:00:00')"
    )
    # NULL-uuid reaped -> its own capture_unknown root
    conn.execute(
        "INSERT INTO terminal_identity (terminal_id,provider,base_name,provider_session_id,lifecycle,created_at) "
        "VALUES ('nulluuid','kiro_cli','nulluuid',NULL,'reaped','2026-01-01 08:00:00')"
    )
    conn.commit()
    conn.close()


def test_ac7_migration_collapse_rule(real_sqlite_env):
    """AC7: collapse rule — live-first branch, dup->one root, S1 attach, NULL-uuid own root."""
    db_file = str(real_sqlite_env["db_file"])
    _seed_legacy(db_file)
    import cli_agent_orchestrator.constants as k

    with mock.patch.object(k, "DATABASE_FILE", db_file):
        d._migrate_f829_conversation_identity()

    conn = sqlite3.connect(db_file)
    # no unrooted rows
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM terminal_identity WHERE identity_key IS NULL"
        ).fetchone()[0]
        == 0
    )
    # live1 -> live root with owner + origin=spawn
    row = conn.execute(
        "SELECT lifecycle,owner_principal,origin FROM conversation_identity WHERE current_terminal_id='live1'"
    ).fetchone()
    assert row == ("live", "mb_owner1", "spawn")
    # dup uuid -> ONE root, 2 incarnations
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM conversation_identity WHERE provider_session_id='uuid-dup'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM terminal_identity WHERE provider_session_id='uuid-dup'"
        ).fetchone()[0]
        == 2
    )
    # attach1 shares live1's root (S1) — no second root for uuid-live-1
    lk = conn.execute(
        "SELECT identity_key FROM terminal_identity WHERE terminal_id='live1'"
    ).fetchone()[0]
    ak = conn.execute(
        "SELECT identity_key FROM terminal_identity WHERE terminal_id='attach1'"
    ).fetchone()[0]
    assert lk == ak
    # NULL-uuid -> capture_unknown + legacy_unknown_owner
    assert conn.execute(
        "SELECT lifecycle,origin FROM conversation_identity WHERE current_terminal_id='nulluuid'"
    ).fetchone() == ("capture_unknown", "legacy_unknown_owner")
    conn.close()


def test_ac7_migration_idempotent(real_sqlite_env):
    """AC7: running the migration twice on a half-migrated db changes nothing."""
    db_file = str(real_sqlite_env["db_file"])
    _seed_legacy(db_file)
    import cli_agent_orchestrator.constants as k

    with mock.patch.object(k, "DATABASE_FILE", db_file):
        d._migrate_f829_conversation_identity()
        conn = sqlite3.connect(db_file)
        n1 = conn.execute("SELECT COUNT(*) FROM conversation_identity").fetchone()[0]
        conn.close()
        d._migrate_f829_conversation_identity()
        conn = sqlite3.connect(db_file)
        n2 = conn.execute("SELECT COUNT(*) FROM conversation_identity").fetchone()[0]
        conn.close()
    assert n1 == n2


def test_ac7_migration_half_state_attaches_live_row(real_sqlite_env):
    """AC7/E3: a HALF-migrated db where a surviving LIVE incarnation is unrooted
    (``terminal_identity.identity_key IS NULL``) but its (provider, namespace,
    uuid) root ALREADY exists must ATTACH the live row to that root — not INSERT
    a second one (which collides on uq_conversation_identity_bound and, with the
    collision swallowed, leaves the row permanently unrooted). Running the
    migration twice over that exact half-state is byte-for-byte stable.
    """
    db_file = str(real_sqlite_env["db_file"])
    _seed_legacy(db_file)
    import cli_agent_orchestrator.constants as k

    with mock.patch.object(k, "DATABASE_FILE", db_file):
        # First full migration builds the schema + all roots (incl. live1's).
        d._migrate_f829_conversation_identity()

        conn = sqlite3.connect(db_file)
        # Synthesize the half-state: keep live1's root + surviving terminals row,
        # but UN-root the live terminal_identity row (as a crashed partial run
        # would have left it — root inserted, FK back-fill not yet applied).
        conn.execute("UPDATE terminal_identity SET identity_key=NULL WHERE terminal_id='live1'")
        conn.commit()
        # Sanity: exactly the half-state we intend to exercise.
        assert (
            conn.execute(
                "SELECT identity_key FROM terminal_identity WHERE terminal_id='live1'"
            ).fetchone()[0]
            is None
        )
        root_key = conn.execute(
            "SELECT identity_key FROM conversation_identity "
            "WHERE provider='codex' AND provider_namespace='default:codex' "
            "AND provider_session_id='uuid-live-1'"
        ).fetchone()[0]
        assert root_key is not None
        conn.close()

        # Run the migration TWICE over the half-state.
        d._migrate_f829_conversation_identity()

        conn = sqlite3.connect(db_file)
        # Zero unrooted incarnations after the first re-run.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM terminal_identity WHERE identity_key IS NULL"
            ).fetchone()[0]
            == 0
        )
        # Exactly ONE bound root for the triplet (no second root inserted).
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversation_identity "
                "WHERE provider='codex' AND provider_namespace='default:codex' "
                "AND provider_session_id='uuid-live-1'"
            ).fetchone()[0]
            == 1
        )
        # The live row attached to the pre-existing root, which is live +
        # current=live1 and retained its owner.
        after1 = conn.execute(
            "SELECT identity_key,lifecycle,current_terminal_id,owner_principal "
            "FROM conversation_identity WHERE provider_session_id='uuid-live-1'"
        ).fetchone()
        assert after1 == (root_key, "live", "live1", "mb_owner1")
        assert (
            conn.execute(
                "SELECT identity_key FROM terminal_identity WHERE terminal_id='live1'"
            ).fetchone()[0]
            == root_key
        )
        snapshot1 = conn.execute(
            "SELECT identity_key,provider,provider_namespace,provider_session_id,"
            "lifecycle,current_terminal_id,owner_principal,generation "
            "FROM conversation_identity ORDER BY identity_key"
        ).fetchall()
        conn.close()

        # Second re-run over the (now fully-migrated) db: no change at all.
        d._migrate_f829_conversation_identity()
        conn = sqlite3.connect(db_file)
        snapshot2 = conn.execute(
            "SELECT identity_key,provider,provider_namespace,provider_session_id,"
            "lifecycle,current_terminal_id,owner_principal,generation "
            "FROM conversation_identity ORDER BY identity_key"
        ).fetchall()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM terminal_identity WHERE identity_key IS NULL"
            ).fetchone()[0]
            == 0
        )
        conn.close()
    assert snapshot1 == snapshot2
    """AC2 / AC7 CAS mutant: two concurrent claims at the same generation, one wins."""
    d.mint_conversation_identity(
        identity_key="k1",
        provider="codex",
        provider_namespace="ns",
        agent_profile="dev",
        model=None,
        reasoning_effort=None,
        owner_principal="mb",
        origin_callback_ref=None,
        current_terminal_id="t1",
    )
    assert d.claim_resume("k1", 0, "a") is True
    assert d.claim_resume("k1", 0, "b") is False  # session_resume_in_progress


# --------------------------------------------------------------------------
# D1 [A1] — recovery_manifest (1:1 root-owned)
# --------------------------------------------------------------------------


def test_a1_recovery_manifest_upsert_and_partial_update(real_sqlite_env):
    """A1/D1: upsert creates the row, later calls update ONLY supplied fields
    (a None argument never blanks a stored value), and get decodes JSON."""
    _mkroot("k_man", "codex", "mb", "live", "t_man", uuid="um1")
    d.upsert_recovery_manifest(
        "k_man",
        cwd="/w/repo",
        repo_root="/w/repo",
        worktree_path="/w/repo/wt",
        worktree_branch="cao/x",
        worktree_commit="abc123",
        retained_store_refs={"codex_home": "/h/.codex"},
        launch_attempt_id="att-1",
        capture_nonce="nonce-1",
        task_label="do the thing",
        frozen_pin_revision=1,
        checkpoint_token="mid:42",
    )
    m = d.get_recovery_manifest("k_man")
    assert m is not None
    assert m["cwd"] == "/w/repo" and m["worktree_branch"] == "cao/x"
    assert m["retained_store_refs"] == {"codex_home": "/h/.codex"}
    assert m["capture_nonce"] == "nonce-1" and m["checkpoint_token"] == "mid:42"
    # Partial update: only checkpoint_token supplied — everything else intact.
    d.upsert_recovery_manifest("k_man", checkpoint_token="mid:43")
    m2 = d.get_recovery_manifest("k_man")
    assert m2["checkpoint_token"] == "mid:43"
    assert m2["cwd"] == "/w/repo"  # untouched
    assert m2["capture_nonce"] == "nonce-1"  # untouched
    assert m2["retained_store_refs"] == {"codex_home": "/h/.codex"}  # untouched


def test_a1_migration_backfills_one_manifest_per_root_idempotent(real_sqlite_env):
    """A1/D1: the migration backfills exactly one recovery_manifest per root,
    drawing cwd/worktree provenance from the current incarnation, idempotently."""
    db_file = str(real_sqlite_env["db_file"])
    _seed_legacy(db_file)
    # Simulate the hot-fix's prior _migrate_f631_terminal_identity having added
    # the worktree columns (it runs BEFORE _migrate_f829 in the real chain), then
    # record provenance on the live incarnation so the backfill can read it.
    conn = sqlite3.connect(db_file)
    for col in ("worktree_path", "worktree_branch", "worktree_repo_root"):
        conn.execute(f"ALTER TABLE terminal_identity ADD COLUMN {col} VARCHAR")
    conn.execute(
        "UPDATE terminal_identity SET cwd='/w/live1', worktree_path='/w/live1/wt', "
        "worktree_branch='cao/live1', worktree_repo_root='/w/live1', git_sha='deadbee' "
        "WHERE terminal_id='live1'"
    )
    conn.commit()
    conn.close()
    import cli_agent_orchestrator.constants as k

    with mock.patch.object(k, "DATABASE_FILE", db_file):
        d._migrate_f829_conversation_identity()
        conn = sqlite3.connect(db_file)
        roots = conn.execute("SELECT COUNT(*) FROM conversation_identity").fetchone()[0]
        mans = conn.execute("SELECT COUNT(*) FROM recovery_manifest").fetchone()[0]
        assert mans == roots and roots > 0
        # live1's manifest picked up its incarnation provenance.
        lk = conn.execute(
            "SELECT identity_key FROM terminal_identity WHERE terminal_id='live1'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT cwd, worktree_path, worktree_branch, repo_root, worktree_commit "
            "FROM recovery_manifest WHERE identity_key=?",
            (lk,),
        ).fetchone()
        assert row == ("/w/live1", "/w/live1/wt", "cao/live1", "/w/live1", "deadbee")
        conn.close()
        # Idempotent: a second run neither adds rows nor overwrites the manifest.
        d._migrate_f829_conversation_identity()
        conn = sqlite3.connect(db_file)
        assert conn.execute("SELECT COUNT(*) FROM recovery_manifest").fetchone()[0] == roots
        conn.close()


def test_ac7_cas_null_guard_holds_at_matching_generation(real_sqlite_env):
    """AC7 CAS-guard mutant kill: a SECOND claim at the CURRENT generation, while a
    claim is already held, must still lose — proving the ``resume_claim IS NULL``
    guard is load-bearing independently of the generation check.
    """
    d.mint_conversation_identity(
        identity_key="k_cas",
        provider="codex",
        provider_namespace="ns",
        agent_profile="dev",
        model=None,
        reasoning_effort=None,
        owner_principal="mb",
        origin_callback_ref=None,
        current_terminal_id="t1",
    )
    assert d.claim_resume("k_cas", 0, "a") is True  # generation now 1, claim held
    # Address the CURRENT generation (1): the generation guard is satisfied, so
    # only the ``resume_claim IS NULL`` guard can reject this — it must.
    assert d.claim_resume("k_cas", 1, "b") is False


def test_ac7_cas_generation_guard_rejects_stale_generation(real_sqlite_env):
    """AC7 CAS-guard mutant kill (E4): a claim addressing a STALE generation must
    lose while ``resume_claim IS NULL`` — proving the
    ``generation == expected_generation`` predicate is load-bearing INDEPENDENTLY
    of the NULL guard.

    Sequence: mint at generation 0, claim (generation → 1, claim held), then
    clear the claim (claim → NULL, generation STAYS 1). A second claim at the
    stale generation 0 now has the NULL guard satisfied — so ONLY the generation
    predicate can reject it. Deleting that predicate lets the stale claim win,
    which this test alone catches.
    """
    d.mint_conversation_identity(
        identity_key="k_stale",
        provider="codex",
        provider_namespace="ns",
        agent_profile="dev",
        model=None,
        reasoning_effort=None,
        owner_principal="mb",
        origin_callback_ref=None,
        current_terminal_id="t1",
    )
    assert d.claim_resume("k_stale", 0, "first") is True  # generation now 1
    d.clear_resume_claim("k_stale")  # claim back to NULL; generation stays 1
    assert d.get_conversation_identity("k_stale")["generation"] == 1
    assert d.get_conversation_identity("k_stale")["resume_claim"] is None
    # NULL guard is satisfied (claim is None); the ONLY predicate that can reject
    # a claim at the stale generation 0 is ``generation == expected_generation``.
    assert d.claim_resume("k_stale", 0, "stale") is False
    # unchanged: still generation 1, still no claim held
    assert d.get_conversation_identity("k_stale")["generation"] == 1
    assert d.get_conversation_identity("k_stale")["resume_claim"] is None


def test_ac7_bound_uniqueness(real_sqlite_env):
    """AC7 drop-UNIQUE mutant target: a duplicate (provider,ns,uuid) bind is rejected."""
    for key in ("k1", "k2"):
        d.mint_conversation_identity(
            identity_key=key,
            provider="codex",
            provider_namespace="ns",
            agent_profile="dev",
            model=None,
            reasoning_effort=None,
            owner_principal="mb",
            origin_callback_ref=None,
            current_terminal_id=key,
        )
    assert (
        d.bind_provider_session_id("k1", provider_session_id="U", provider_namespace="ns") is True
    )
    assert (
        d.bind_provider_session_id("k2", provider_session_id="U", provider_namespace="ns") is False
    )


# --------------------------------------------------------------------------
# D2 — three transitions
# --------------------------------------------------------------------------


def test_ac6_uuidless_kiro_hibernate_refused(real_sqlite_env):
    """AC6: a capture_unknown (uuid-less) kiro conversation refuses planned hibernate."""
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot("k_cu", "kiro_cli", "mb", "capture_unknown", "t_cu")
    dec = ct.evaluate_planned_hibernate("t_cu")
    assert not dec.allowed and dec.reason == "capture_unknown"


def test_d2_planned_hibernate_valid_artifact(real_sqlite_env):
    from cli_agent_orchestrator.services import conversation_transition as ct
    from cli_agent_orchestrator.services.session_artifact import ArtifactState, ArtifactStatus

    _mkroot("k_v", "codex", "mb", "live", "t_v", uuid="u1")
    with mock.patch(
        "cli_agent_orchestrator.services.session_artifact.resolve_artifact",
        return_value=ArtifactStatus(ArtifactState.VALID, "/roll.jsonl"),
    ):
        dec = ct.evaluate_planned_hibernate("t_v")
    assert dec.allowed and dec.lifecycle == "hibernated"
    ct.commit_hibernate(dec)
    assert d.get_conversation_identity("k_v")["lifecycle"] == "hibernated"


def test_d2_explicit_reap_abandoned(real_sqlite_env):
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot("k_r", "codex", "mb", "live", "t_r", uuid="u2")
    assert ct.commit_reap("t_r") == "k_r"
    assert d.get_conversation_identity("k_r")["lifecycle"] == "abandoned"


# --------------------------------------------------------------------------
# D3 — authorize / classify / claim / verify / publish
# --------------------------------------------------------------------------


def test_ac2_resume_not_owner(real_sqlite_env):
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot("k_o", "codex", "mb_owner", "hibernated", "t_o", uuid="u3")
    adm = ct.authorize_and_classify_resume(d.get_conversation_identity("k_o"), "mb_other")
    assert not adm.ok and adm.error == "resume_not_owner"


def test_ac2_null_owner_requires_claim(real_sqlite_env):
    """Supervisor ask 1: a NULL-owner (top-level/legacy) root is resume_not_owner until claimed."""
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot("k_n", "codex", None, "hibernated", "t_n", uuid="u4")
    adm = ct.authorize_and_classify_resume(d.get_conversation_identity("k_n"), "mb_anyone")
    assert not adm.ok and adm.error == "resume_not_owner"
    # claim then it authorizes
    d.claim_identity_owner("k_n", "mb_anyone")
    adm2 = ct.authorize_and_classify_resume(d.get_conversation_identity("k_n"), "mb_anyone")
    assert adm2.ok


@pytest.mark.parametrize(
    "lifecycle,token",
    [
        ("live", "session_live_owned"),
        ("abandoned", "session_abandoned"),
        ("expired", "session_expired"),
        ("capture_unknown", "session_artifact_missing"),
    ],
)
def test_ac2_classify_tokens(real_sqlite_env, lifecycle, token):
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot(f"k_{lifecycle}", "codex", "mb", lifecycle, f"t_{lifecycle}", uuid=f"u_{lifecycle}")
    adm = ct.authorize_and_classify_resume(d.get_conversation_identity(f"k_{lifecycle}"), "mb")
    assert not adm.ok and adm.error == token


def test_ac1_verify_publish_and_mismatch(real_sqlite_env):
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot("k_p", "codex", "mb", "hibernated", "t_old", uuid="u5", model="gpt-5.6-luna")
    adm = ct.authorize_and_classify_resume(d.get_conversation_identity("k_p"), "mb")
    adm = ct.claim_resume_admission(adm, "claimant")
    assert adm.ok
    # mismatch does NOT publish and clears the claim (retryable)
    bad = ct.verify_and_publish_resume(
        adm, terminal_id="t_new", reported_session_id="WRONG", provider="codex"
    )
    assert not bad.ok and bad.error == "session_identity_mismatch"
    root = d.get_conversation_identity("k_p")
    assert root["current_terminal_id"] == "t_old" and root["resume_claim"] is None
    # re-claim + matching id publishes
    adm = ct.claim_resume_admission(ct.authorize_and_classify_resume(root, "mb"), "claimant2")
    ok = ct.verify_and_publish_resume(
        adm, terminal_id="t_new", reported_session_id="u5", provider="codex"
    )
    assert ok.ok
    assert d.get_conversation_identity("k_p")["current_terminal_id"] == "t_new"


# --------------------------------------------------------------------------
# D4 — capture attribution
# --------------------------------------------------------------------------


def test_ac3_capture_not_owned_by_another(real_sqlite_env):
    """AC3: a uuid already bound to another identity is never re-attached; event recorded."""
    from cli_agent_orchestrator.services import conversation_transition as ct

    _mkroot("k_a", "codex", "mb", "live", "t_a")  # no uuid yet
    _mkroot("k_b", "codex", "mb", "live", "t_b")
    assert (
        ct.attach_captured_uuid(
            "t_a", provider_session_id="U", provider="codex", provider_namespace="ns"
        )["status"]
        == "captured"
    )
    res = ct.attach_captured_uuid(
        "t_b", provider_session_id="U", provider="codex", provider_namespace="ns"
    )
    assert res["status"] == "capture_rejected" and res["conflicting_identity"] == "k_a"
    assert d.get_conversation_identity("k_b")["provider_session_id"] is None
    assert "uuid_capture_rejected" in [e["event"] for e in d.get_conversation_events("k_b")]


# --------------------------------------------------------------------------
# D8 — crash-detach + reconciliation
# --------------------------------------------------------------------------


def test_ac4_crash_detach_narrow(real_sqlite_env):
    """AC4/E2: crash_detach settles BOTH delivery authorities (inbox + ledger)
    to ``receiver_gone``, nullifies the mailbox pointer, drops the warm intent
    and the terminals row, flips the root to ``detached`` — and touches NOTHING
    ELSE (no barrier cancel, no member GONE flip, no pin change).

    Seeds every named non-cascade invariant so removing either settlement call
    OR any preservation guard makes this test fail:
      * a PENDING inbox row + its paired PENDING ledger row,
      * a DELIVERING inbox row + its paired EMITTED ledger row,
      * a mailbox bound to the dying terminal,
      * a warm intent for the dying terminal,
      * an OPEN barrier + an AWAITING member for the dying terminal,
      * a frozen authority pin for the dying terminal.
    """
    _mkroot("k_cd", "codex", "mb", "live", "t_cd", uuid="u6")
    now = datetime.now(timezone.utc)
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalModel(
                id="t_cd",
                tmux_session="s",
                tmux_window="w",
                provider="codex",
                lifecycle="ephemeral",
                init_state="ready",
            )
        )
        # (1) a PENDING inbox row + paired PENDING ledger row to t_cd.
        m1 = InboxModel(
            id=9001,
            sender_id="s_send",
            receiver_id="t_cd",
            message="pending-msg",
            status=d.MessageStatus.PENDING.value,
        )
        # (2) a DELIVERING inbox row + paired EMITTED ledger row to t_cd.
        m2 = InboxModel(
            id=9002,
            sender_id="s_send",
            receiver_id="t_cd",
            message="delivering-msg",
            status=d.MessageStatus.DELIVERING.value,
        )
        # A control row to an UNRELATED receiver — must stay pending, untouched.
        m3 = InboxModel(
            id=9003,
            sender_id="s_send",
            receiver_id="other_term",
            message="other-msg",
            status=d.MessageStatus.PENDING.value,
        )
        db.add_all([m1, m2, m3])
        db.flush()
        db.add(
            DeliveryLedgerModel(
                message_id=9001,
                receiver_id="t_cd",
                state=d.LedgerState.PENDING.value,
            )
        )
        db.add(
            DeliveryLedgerModel(
                message_id=9002,
                receiver_id="t_cd",
                state=d.LedgerState.EMITTED.value,
            )
        )
        db.add(
            DeliveryLedgerModel(
                message_id=9003,
                receiver_id="other_term",
                state=d.LedgerState.PENDING.value,
            )
        )
        # (3) mailbox bound to t_cd.
        db.add(
            MailboxModel(
                id="mb_cd",
                session_name="s",
                role="worker",
                current_terminal_id="t_cd",
            )
        )
        # (4) warm intent for t_cd.
        db.add(
            WarmIntentModel(
                intent_id="wi_cd",
                worker_terminal_id="t_cd",
                session_name="s",
                worker_profile="dev",
                parent_base_name="base",
                provider="codex",
            )
        )
        # (5) OPEN barrier + AWAITING member for t_cd — must be preserved.
        db.add(
            CallbackBarrierModel(
                id=7001,
                owner_terminal_id="sup_term",
                owner_generation=1,
                label="bar_cd",
                state="OPEN",
                timeout_at=now + timedelta(seconds=600),
            )
        )
        db.flush()
        db.add(
            CallbackBarrierMemberModel(
                id=8001,
                barrier_id=7001,
                member_key="mk_cd",
                position=0,
                terminal_id="t_cd",
                lifecycle_generation=1,
                state="AWAITING",
            )
        )
        # (6) frozen authority pin for t_cd — must be preserved byte-for-byte.
        db.add(
            AuthorityPinModel(
                task_key="t_cd",
                file_path="/some/authority.md",
                sha256="deadbeef",
                version=1,
                registered_by="sup_term",
                frozen=True,
            )
        )

    out = d.crash_detach_terminal("t_cd")

    # narrow transition results
    assert out["terminal_deleted"] and out["lifecycle"] == "detached"
    # A1 D8i: both authority settlement counts are returned (2 undelivered rows).
    assert out["ledger_settled"] == 2 and out["inbox_settled"] == 2
    assert d.get_conversation_identity("k_cd")["lifecycle"] == "detached"
    assert "crash_detached" in [e["event"] for e in d.get_conversation_events("k_cd")]

    with d.SessionLocal() as db:
        # BOTH authorities settled to receiver_gone for the two undelivered rows.
        i1 = db.query(InboxModel).filter_by(id=9001).one()
        i2 = db.query(InboxModel).filter_by(id=9002).one()
        assert i1.status == d.MessageStatus.DELIVERY_FAILED.value
        assert i1.failure_reason == d.UndeliverableReason.RECEIVER_GONE.value
        assert i2.status == d.MessageStatus.DELIVERY_FAILED.value
        assert i2.failure_reason == d.UndeliverableReason.RECEIVER_GONE.value
        l1 = db.query(DeliveryLedgerModel).filter_by(message_id=9001).one()
        l2 = db.query(DeliveryLedgerModel).filter_by(message_id=9002).one()
        assert l1.state == d.LedgerState.UNDELIVERABLE.value
        assert l1.undeliverable_reason == d.UndeliverableReason.RECEIVER_GONE.value
        assert l2.state == d.LedgerState.UNDELIVERABLE.value
        assert l2.undeliverable_reason == d.UndeliverableReason.RECEIVER_GONE.value

        # unrelated receiver's rows untouched.
        i3 = db.query(InboxModel).filter_by(id=9003).one()
        l3 = db.query(DeliveryLedgerModel).filter_by(message_id=9003).one()
        assert i3.status == d.MessageStatus.PENDING.value and i3.failure_reason is None
        assert l3.state == d.LedgerState.PENDING.value and l3.undeliverable_reason is None

        # mailbox pointer nulled; warm intent + terminals row gone.
        assert db.query(MailboxModel).filter_by(id="mb_cd").one().current_terminal_id is None
        assert db.query(WarmIntentModel).filter_by(worker_terminal_id="t_cd").count() == 0
        assert db.query(TerminalModel).filter_by(id="t_cd").count() == 0

        # NON-cascade invariants preserved: barrier still OPEN, member still
        # AWAITING (not GONE), pin still present and frozen.
        bar = db.query(CallbackBarrierModel).filter_by(id=7001).one()
        assert bar.state == "OPEN"
        mem = db.query(CallbackBarrierMemberModel).filter_by(id=8001).one()
        assert mem.state == "AWAITING"
        pin = db.query(AuthorityPinModel).filter_by(task_key="t_cd").one()
        assert pin.frozen is True and pin.sha256 == "deadbeef"


def test_ac4_claim_ttl_reconcile(real_sqlite_env):
    _mkroot("k_ttl", "codex", "mb", "hibernated", "t_ttl", uuid="u7")
    assert d.claim_resume("k_ttl", d.get_conversation_identity("k_ttl")["generation"], "c") is True
    with d.SessionLocal.begin() as db:
        row = db.query(ConversationIdentityModel).filter_by(identity_key="k_ttl").one()
        row.resume_claim_at = datetime.now(timezone.utc) - timedelta(seconds=10000)
    cleared = d.reconcile_stale_resume_claims(600.0)
    assert "k_ttl" in cleared
    assert d.get_conversation_identity("k_ttl")["resume_claim"] is None


def test_d8_reconcile_live_roots(real_sqlite_env):
    from cli_agent_orchestrator.services import conversation_reconcile as cr

    _mkroot("k_dead", "codex", "mb", "live", "term_dead", uuid="u8")
    _mkroot("k_alive", "codex", "mb", "live", "term_alive", uuid="u9")
    with d.SessionLocal.begin() as db:
        for tid in ("term_dead", "term_alive"):
            db.add(
                TerminalModel(
                    id=tid,
                    tmux_session="s",
                    tmux_window=tid,
                    provider="codex",
                    lifecycle="ephemeral",
                    init_state="ready",
                )
            )
    with mock.patch(
        "cli_agent_orchestrator.services.delivery_service.is_target_confirmed_dead",
        side_effect=lambda tid, db: tid == "term_dead",
    ):
        res = cr.reconcile_live_roots()
    assert res["detached"] == 1 and res["left_live"] == 1
    assert d.get_conversation_identity("k_dead")["lifecycle"] == "detached"
    assert d.get_conversation_identity("k_alive")["lifecycle"] == "live"


def test_d8_reconcile_pi_no_artifact_classified_before_detach(real_sqlite_env):
    """E2 (D8, Pi half): a dead pi_cli root with no recoverable artifact is
    classified ``session_artifact_missing`` (with the D8 diagnostic) BEFORE the
    narrow crash-detach, so the required no-artifact outcome is recorded rather
    than silently lost. Detach still proceeds (a dead terminal is detached
    regardless of artifact state).
    """
    from cli_agent_orchestrator.services import conversation_reconcile as cr
    from cli_agent_orchestrator.services.session_artifact import ArtifactState, ArtifactStatus

    # pi root with provider_session_id NULL -> resolve_artifact is MISSING.
    _mkroot("k_pi", "pi_cli", "mb", "live", "term_pi", uuid=None)
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalModel(
                id="term_pi",
                tmux_session="s",
                tmux_window="term_pi",
                provider="pi_cli",
                lifecycle="ephemeral",
                init_state="ready",
            )
        )
    with (
        mock.patch(
            "cli_agent_orchestrator.services.delivery_service.is_target_confirmed_dead",
            side_effect=lambda tid, db: tid == "term_pi",
        ),
        mock.patch(
            "cli_agent_orchestrator.services.session_artifact.resolve_artifact",
            return_value=ArtifactStatus(ArtifactState.MISSING, detail="pi mid-turn crash"),
        ),
    ):
        res = cr.reconcile_live_roots()
    assert res["detached"] == 1
    assert d.get_conversation_identity("k_pi")["lifecycle"] == "detached"
    events = [e["event"] for e in d.get_conversation_events("k_pi")]
    # classification recorded BEFORE the crash_detached event.
    assert "session_artifact_missing" in events
    assert "crash_detached" in events
    assert events.index("session_artifact_missing") < events.index("crash_detached")


# --------------------------------------------------------------------------
# AC1 provider-live arms — evidence is the D9 probe (real CLI crash/resume).
# --------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.parametrize("provider", ["claude_code", "codex", "kiro_cli"])
def test_ac1_provider_crash_resume_live(provider):
    """AC1 per-provider crash->resume recall. Real-CLI arm — see the D9 probe
    report (/data/cao-scratch/briefs/f829-d9-probe.md): claude_code / codex /
    kiro-cli all RECOVER (incl. mid-turn kill); pi is PARTIAL (mid-turn = no
    artifact). Automated live coverage requires --run-live + real provider CLIs.
    """
    pytest.skip("live provider arm — evidence in the D9 probe report")


# --------------------------------------------------------------------------
# AC5 — caller continuity (owner_principal callback routing)
# --------------------------------------------------------------------------


def test_ac5_owner_principal_callback_routing(real_sqlite_env):
    """AC5: the owner_principal mailbox resolves to its CURRENT terminal binding;
    a mailbox with no live binding yields caller_unavailable (queued-retryable),
    never a substitution of the recovering terminal.

    This exercises the same DB seam the no-receiver callback uses
    (get_current_mailbox_terminal): a resumed worker whose root.owner_principal
    is the caller's durable mailbox routes to whatever terminal that mailbox
    currently binds — or nothing when the caller is down.
    """
    from cli_agent_orchestrator.clients.database import (
        MailboxModel,
        get_current_mailbox_terminal,
    )

    # A mailbox bound to a live caller terminal resolves to it.
    with d.SessionLocal.begin() as db:
        db.add(
            MailboxModel(
                id="mb_caller",
                session_name="s",
                role="supervisor",
                current_terminal_id="caller_live",
            )
        )
    assert get_current_mailbox_terminal("mb_caller") == "caller_live"

    # A mailbox with no current binding -> None (caller_unavailable; the row
    # stays queued-retryable, and the recovering terminal is never substituted).
    with d.SessionLocal.begin() as db:
        db.add(
            MailboxModel(id="mb_down", session_name="s", role="worker", current_terminal_id=None)
        )
    assert get_current_mailbox_terminal("mb_down") is None


def test_ac5_resume_bare_callback_routes_to_original_caller(real_sqlite_env):
    """AC5 (the closed gap): a worker resumed FROM supervisor B, replying with a
    no-receiver send_message, resolves to supervisor A's mailbox — the root's
    owner_principal — never supervisor B (the recovering terminal).
    """
    from cli_agent_orchestrator.clients.database import (
        TerminalModel,
        resolve_bare_callback_receiver,
    )

    _mkroot("k_res5", "codex", "mb_A", "hibernated", "t_worker_old", uuid="u_ac5")
    d.publish_current_terminal("k_res5", terminal_id="t_worker_new", provider_session_id="u_ac5")
    with d.SessionLocal.begin() as db:
        # the resumed worker's fresh terminal row records supervisor B as caller
        db.add(
            TerminalModel(
                id="t_worker_new",
                tmux_session="s",
                tmux_window="w",
                provider="codex",
                lifecycle="ephemeral",
                init_state="ready",
                caller_mailbox_id="mb_B",
            )
        )
        db.add(
            TerminalIdentityModel(
                terminal_id="t_worker_new",
                provider="codex",
                base_name="t_worker_new",
                lifecycle="live",
                identity_key="k_res5",
                provider_session_id="u_ac5",
            )
        )
    # THE ASSERTION: bare callback resolves to the ROOT owner (mb_A), not mb_B.
    assert resolve_bare_callback_receiver("t_worker_new") == "mb_A"


def test_ac5_fallback_to_terminal_caller_when_no_owner(real_sqlite_env):
    """A terminal with NO root, or a NULL-owner/legacy root, falls back to the
    terminal-row caller (pre-F829 behaviour preserved for those)."""
    from cli_agent_orchestrator.clients.database import (
        TerminalModel,
        resolve_bare_callback_receiver,
    )

    with d.SessionLocal.begin() as db:
        db.add(
            TerminalModel(
                id="t_noroot",
                tmux_session="s",
                tmux_window="w",
                provider="codex",
                lifecycle="ephemeral",
                init_state="ready",
                caller_mailbox_id="mb_direct",
            )
        )
    assert resolve_bare_callback_receiver("t_noroot") == "mb_direct"

    _mkroot("k_null5", "codex", None, "hibernated", "t_null5", uuid="u_null5")
    with d.SessionLocal.begin() as db:
        db.add(
            TerminalModel(
                id="t_null5_term",
                tmux_session="s",
                tmux_window="w",
                provider="codex",
                lifecycle="ephemeral",
                init_state="ready",
                caller_mailbox_id="mb_legacy",
            )
        )
        db.add(
            TerminalIdentityModel(
                terminal_id="t_null5_term",
                provider="codex",
                base_name="t_null5_term",
                lifecycle="live",
                identity_key="k_null5",
            )
        )
    assert resolve_bare_callback_receiver("t_null5_term") == "mb_legacy"
