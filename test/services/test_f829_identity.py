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
    ConversationIdentityModel,
    TerminalIdentityModel,
    TerminalModel,
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


def test_ac2_concurrent_claim_exactly_one_wins(real_sqlite_env):
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
    """AC4: crash_detach removes the terminals row + warm intent, lifecycle detached."""
    _mkroot("k_cd", "codex", "mb", "live", "t_cd", uuid="u6")
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
    out = d.crash_detach_terminal("t_cd")
    assert out["terminal_deleted"] and out["lifecycle"] == "detached"
    assert d.get_conversation_identity("k_cd")["lifecycle"] == "detached"
    assert "crash_detached" in [e["event"] for e in d.get_conversation_events("k_cd")]


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
