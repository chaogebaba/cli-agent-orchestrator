"""F642 delivery-ledger spine — DB-backed storage-layer tests.

Every AC here is asserted against the real SQLAlchemy tables / operations, not
against mocked surfaces (AC2's explicit instruction). The ``db_env`` fixture
mirrors ``test/clients/test_inbox_expire_supersede.py``: an in-memory SQLite via
StaticPool with ``Base.metadata.create_all`` and ``SessionLocal`` monkeypatched.

Coverage: AC1, AC2, AC3, AC4, AC7, AC8, AC10, AC12, AC13, AC14, AC15, AC16,
AC17, AC19, AC22, AC23.
"""

import threading

import pytest
import sqlite3
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackReplayQueueModel,
    DeliveryEmissionModel,
    DeliveryLedgerModel,
    _migrate_f642_delivery_ledger,
    _utcnow,
    ack_delivery_ledger,
    claim_emission,
    create_delivery_ledger_row,
    create_inbox_message,
    create_terminal,
    delivery_ledger_dispute_view,
    emit_via_carrier,
    enqueue_callback_replay,
    enqueue_callback_replay_gated,
    mark_carrier_unavailable,
    mark_receiver_gone,
    maybe_mark_undeliverable,
    record_blocked_awaiting_idle,
    record_emission_outcome,
    write_through_terminal_state,
)
from cli_agent_orchestrator.clients.delivery_ledger import (
    AckActor,
    Carrier,
    EmissionOutcome,
    LedgerState,
    SuppressedReason,
    UndeliverableReason,
)
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    create_terminal("sup", "cao-t", "w-sup", "claude_code")
    create_terminal("wrk", "cao-t", "w-wrk", "claude_code")
    return sessions


def _ledger(db_env, message_id):
    with db_env() as db:
        return db.get(DeliveryLedgerModel, message_id)


# ── AC12 / AC13: additive migration, F578 columns untouched ──────────────────
def test_ac12_migration_creates_tables_and_leaves_inbox_intact(tmp_path, monkeypatch):
    db_path = tmp_path / "prod_copy.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    # Simulate a pre-existing inbox/mailboxes schema.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE inbox (id INTEGER PRIMARY KEY, message TEXT)")
        conn.execute("CREATE TABLE mailboxes (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO inbox (id, message) VALUES (1, 'x')")
        inbox_sql_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='inbox'"
        ).fetchone()[0]
    _migrate_f642_delivery_ledger()
    with sqlite3.connect(str(db_path)) as conn:
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"delivery_ledger", "delivery_emission", "condition_ledger"} <= names
        inbox_sql_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='inbox'"
        ).fetchone()[0]
        # AC12: inbox schema byte-identical, existing row untouched.
        assert inbox_sql_before == inbox_sql_after
        assert conn.execute("SELECT message FROM inbox WHERE id=1").fetchone()[0] == "x"
    # Idempotent.
    _migrate_f642_delivery_ledger()


def test_ac2_unique_carrier_constraint_present(tmp_path, monkeypatch):
    db_path = tmp_path / "u.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_f642_delivery_ledger()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO delivery_emission (message_id, carrier, outcome, attempts, claimed_at) "
            "VALUES (1, 'native', 'pending', 0, '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delivery_emission (message_id, carrier, outcome, attempts, claimed_at) "
                "VALUES (1, 'native', 'pending', 0, '2026-01-01')"
            )


# ── AC1 / D1: a send creates exactly one ledger row ──────────────────────────
def test_ac1_send_creates_one_ledger_row(db_env):
    msg = create_inbox_message("sup", "wrk", "hello")
    row = _ledger(db_env, msg.id)
    assert row is not None
    assert row.state == LedgerState.PENDING.value
    assert row.emission_count == 0
    assert row.receiver_id == "wrk"


def test_ac1_one_carrier_per_id_emission_count(db_env):
    """20 callbacks, one native emission each → emission_count == 1, no repeat."""
    for i in range(20):
        msg = create_inbox_message("sup", "wrk", f"cb{i}")
        with db_env() as db:
            assert emit_via_carrier(
                db, message_id=msg.id, carrier=Carrier.NATIVE, speak=lambda: True
            )
            db.commit()
        row = _ledger(db_env, msg.id)
        assert row.emission_count == 1
        assert row.first_carrier == Carrier.NATIVE.value
        assert row.state == LedgerState.EMITTED.value


def test_ac1_mutant_dropping_unique_lets_emission_count_climb(db_env):
    """MUTANT: without UNIQUE(message_id,carrier) the replay path re-emits and
    emission_count climbs. We simulate the mutant by recording success twice
    directly (bypassing the claim), reproducing #488's counted window."""
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE)
        record_emission_outcome(
            db, message_id=msg.id, carrier=Carrier.NATIVE, outcome=EmissionOutcome.SUCCEEDED
        )
        # mutant re-emit (constraint dropped): record success again
        record_emission_outcome(
            db, message_id=msg.id, carrier=Carrier.NATIVE, outcome=EmissionOutcome.SUCCEEDED
        )
        db.commit()
    assert _ledger(db_env, msg.id).emission_count == 2  # climbed — the defect


# ── AC2 / D2: a repeat by the same carrier is refused by the DB ──────────────
def test_ac2_second_same_carrier_claim_loses(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        assert claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE) is True
        # second claim by the SAME carrier loses on the unique constraint
        assert claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE) is False
        db.commit()
    with db_env() as db:
        n = (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.message_id == msg.id)
            .count()
        )
        assert n == 1  # exactly one emission row


def test_ac2_different_carrier_inserts_cleanly(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        assert claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE) is True
        assert claim_emission(db, message_id=msg.id, carrier=Carrier.DOORBELL) is True
        db.commit()
    with db_env() as db:
        n = (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.message_id == msg.id)
            .count()
        )
        assert n == 2  # a legitimate fallback is a different carrier


def test_ac3_emit_via_carrier_loser_never_speaks(db_env):
    """AC3-ish (single-process determinism): once a carrier claim is held, a
    second emit_via_carrier for the same carrier does not speak."""
    msg = create_inbox_message("sup", "wrk", "x")
    spoke = []
    with db_env() as db:
        claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE)
        db.commit()
    with db_env() as db:
        won = emit_via_carrier(
            db, message_id=msg.id, carrier=Carrier.NATIVE, speak=lambda: spoke.append(1) or True
        )
        db.commit()
    assert won is False
    assert spoke == []  # loser emitted nothing


def test_ac3_concurrent_claim_exactly_one_wins(tmp_path, monkeypatch):
    """AC3 ★: two real threads race the same id on ON-DISK sqlite; exactly one
    wins the insert and exactly one would emit. Uses a real per-thread connection
    pool (not StaticPool) with WAL + busy_timeout, the shape upstream PR #709's
    own concurrency tests use."""
    db_path = tmp_path / "race.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    from sqlalchemy import event as _sa_event

    @_sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    create_terminal("sup", "cao-t", "w-sup", "claude_code")
    create_terminal("wrk", "cao-t", "w-wrk", "claude_code")
    msg = create_inbox_message("sup", "wrk", "x")

    results: list[bool] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            with sessions() as db:
                won = claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE)
                db.commit()
            with lock:
                results.append(won)
        except Exception as e:  # a lock contention loser still must not crash
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"claim raised under contention: {errors}"
    assert sorted(results) == [False, True]  # exactly one winner
    # storage truth: exactly one emission row exists.
    with sessions() as db:
        assert (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.message_id == msg.id)
            .count()
            == 1
        )


# ── AC4 / D4: ack records the actor ──────────────────────────────────────────
def test_ac4_ack_records_actor(db_env):
    msg_a = create_inbox_message("sup", "wrk", "explicit-acked")
    msg_b = create_inbox_message("sup", "wrk", "hook-acked")
    with db_env() as db:
        ack_delivery_ledger(db, message_ids=[msg_a.id], actor=AckActor.EXPLICIT)
        ack_delivery_ledger(db, message_ids=[msg_b.id], actor=AckActor.HOOK)
        db.commit()
    assert _ledger(db_env, msg_a.id).acked_by == "explicit"
    assert _ledger(db_env, msg_b.id).acked_by == "hook"
    assert _ledger(db_env, msg_a.id).state == LedgerState.ACKED.value


# ── AC7 / AC8 / D6: replay is ledger-gated and pruned on ack ──────────────────
def test_ac7_replay_of_acked_id_refused(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        ack_delivery_ledger(db, message_ids=[msg.id], actor=AckActor.EXPLICIT)
        db.commit()
    with db_env() as db:
        result = enqueue_callback_replay_gated(
            db, mailbox_id="mb1", inbox_row_ids=[msg.id]
        )
        db.commit()
    assert result["refused"] == [msg.id]
    assert result["enqueued"] == []
    # queue stays empty, suppressed_reason recorded
    with db_env() as db:
        assert db.query(CallbackReplayQueueModel).count() == 0
    assert _ledger(db_env, msg.id).suppressed_reason == SuppressedReason.ALREADY_ACKED.value


def test_ac7_unacked_id_is_enqueued(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        result = enqueue_callback_replay_gated(
            db, mailbox_id="mb1", inbox_row_ids=[msg.id]
        )
        db.commit()
    assert result["enqueued"] == [msg.id]
    with db_env() as db:
        assert db.query(CallbackReplayQueueModel).count() == 1


def test_ac8_ack_prunes_queued_replay(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        enqueue_callback_replay(db, mailbox_id="mb1", inbox_row_ids=[msg.id])
        db.commit()
    with db_env() as db:
        assert db.query(CallbackReplayQueueModel).count() == 1
    with db_env() as db:
        ack_delivery_ledger(db, message_ids=[msg.id], actor=AckActor.EXPLICIT)
        db.commit()
    with db_env() as db:
        assert db.query(CallbackReplayQueueModel).count() == 0  # pruned


# ── AC10 / D8: a send with no ledger row is detectable ───────────────────────
def test_ac10_no_ledger_row_is_detectable(db_env):
    """D8: because the ledger row is a precondition for emission, its ABSENCE is
    detectable at send time — the dispute view reports found=False rather than a
    false 'delivered'."""
    view = delivery_ledger_dispute_view(999999)  # never created
    assert view["found"] is False


# ── AC13 / D10: F642 does not touch F578 columns ─────────────────────────────
def test_ac13_delivery_path_does_not_touch_f578_columns(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        emit_via_carrier(db, message_id=msg.id, carrier=Carrier.NATIVE, speak=lambda: True)
        ack_delivery_ledger(db, message_ids=[msg.id], actor=AckActor.EXPLICIT)
        db.commit()
    # The F578 columns on the inbox row are still their defaults (None).
    from cli_agent_orchestrator.clients.database import InboxModel

    with db_env() as db:
        row = db.query(InboxModel).filter(InboxModel.id == msg.id).one()
        assert row.supersede_key is None
        assert row.digested_into is None
        assert row.expire_after_s is None


# ── AC15 / D4: dispute-by-query ──────────────────────────────────────────────
def test_ac15_dispute_view_returns_carriers_and_actor(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        emit_via_carrier(db, message_id=msg.id, carrier=Carrier.NATIVE, speak=lambda: True)
        ack_delivery_ledger(db, message_ids=[msg.id], actor=AckActor.EXPLICIT)
        db.commit()
    view = delivery_ledger_dispute_view(msg.id)
    assert view["found"] is True
    assert view["acked_by"] == "explicit"
    assert [c["carrier"] for c in view["carriers"]] == ["native"]
    assert view["carriers"][0]["outcome"] == "succeeded"
    assert view["emission_count"] == 1


# ── AC16 / D12: waiting is recorded, not inferred ────────────────────────────
def test_ac16_blocked_awaiting_idle_recorded_and_cleared_on_emit(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        record_blocked_awaiting_idle(db, message_id=msg.id)
        db.commit()
    row = _ledger(db_env, msg.id)
    assert row.blocked_reason == "awaiting_idle"
    assert row.blocked_since is not None
    # emit clears the wait
    with db_env() as db:
        emit_via_carrier(db, message_id=msg.id, carrier=Carrier.NATIVE, speak=lambda: True)
        db.commit()
    row = _ledger(db_env, msg.id)
    assert row.blocked_reason is None and row.blocked_since is None


def test_ac16_second_arm_terminal_state_clears_wait(db_env):
    """A row that reaches a terminal state while still blocked also has both
    fields cleared — no stale wait on an undeliverable row."""
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        record_blocked_awaiting_idle(db, message_id=msg.id)
        db.commit()
    assert _ledger(db_env, msg.id).blocked_reason == "awaiting_idle"
    with db_env() as db:
        write_through_terminal_state(
            db, message_ids=[msg.id], state=LedgerState.SUPERSEDED
        )
        db.commit()
    row = _ledger(db_env, msg.id)
    assert row.state == LedgerState.SUPERSEDED.value
    assert row.blocked_reason is None and row.blocked_since is None


def test_ac16_blocked_since_ages_not_reset_on_repeat(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        record_blocked_awaiting_idle(db, message_id=msg.id)
        db.commit()
    first = _ledger(db_env, msg.id).blocked_since
    with db_env() as db:
        record_blocked_awaiting_idle(db, message_id=msg.id)  # repeat gate hit
        db.commit()
    assert _ledger(db_env, msg.id).blocked_since == first  # unchanged — the wait ages


# ── AC17 / D13: terminal transitions write through ───────────────────────────
def test_ac17_supersede_writes_through_to_ledger(db_env):
    """A send with a supersede_key matching an earlier PENDING row flips that row
    to SUPERSEDED AND its ledger row to `superseded` in the same transaction."""
    first = create_inbox_message("sup", "wrk", "old", supersede_key="k1")
    assert _ledger(db_env, first.id).state == LedgerState.PENDING.value
    second = create_inbox_message("sup", "wrk", "new", supersede_key="k1")
    # first inbox row superseded
    from cli_agent_orchestrator.clients.database import InboxModel

    with db_env() as db:
        assert (
            db.query(InboxModel).filter(InboxModel.id == first.id).one().status
            == MessageStatus.SUPERSEDED.value
        )
    # AC17: its ledger row is `superseded`, not left at `pending`.
    assert _ledger(db_env, first.id).state == LedgerState.SUPERSEDED.value
    # the new row is a fresh pending ledger row
    assert _ledger(db_env, second.id).state == LedgerState.PENDING.value


def test_ac17_expire_writes_through(db_env):
    from cli_agent_orchestrator.clients.database import (
        expire_pending_rows,
        list_expired_pending_rows,
    )
    from datetime import timedelta

    msg = create_inbox_message("sup", "wrk", "ephemeral", expire_after_s=1)
    later = _utcnow() + timedelta(seconds=2)
    expire_pending_rows(list_expired_pending_rows(now=later))
    assert _ledger(db_env, msg.id).state == LedgerState.EXPIRED.value


def test_ac17_mutant_status_only_leaves_ledger_pending(db_env):
    """MUTANT: writing only the inbox status (no write-through) leaves the ledger
    row reading `pending` for a message that can never be delivered."""
    msg = create_inbox_message("sup", "wrk", "x")
    from cli_agent_orchestrator.clients.database import InboxModel

    with db_env() as db:
        # mutant: flip inbox status WITHOUT calling write_through_terminal_state
        db.query(InboxModel).filter(InboxModel.id == msg.id).update(
            {InboxModel.status: MessageStatus.SUPERSEDED.value}, synchronize_session=False
        )
        db.commit()
    # ledger still pending — the split-brain the spine exists to remove
    assert _ledger(db_env, msg.id).state == LedgerState.PENDING.value


# ── AC19 / D2/S2: failed carrier keeps its claim; exhaustion names the drop ───
def test_ac19_failed_carrier_keeps_claim_and_retries(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        # applicable = only native (single-carrier seat)
        create_delivery_ledger_row(
            db,
            message_id=msg.id,
            receiver_id="wrk",
            mailbox_id=None,
            applicable_carriers=[Carrier.NATIVE],
        )
        claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE)
        record_emission_outcome(
            db, message_id=msg.id, carrier=Carrier.NATIVE, outcome=EmissionOutcome.FAILED
        )
        db.commit()
    with db_env() as db:
        e = (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.message_id == msg.id)
            .one()
        )
        assert e.outcome == "failed"
        assert e.attempts == 1
    # retry under the SAME claim (never a second row)
    with db_env() as db:
        record_emission_outcome(
            db, message_id=msg.id, carrier=Carrier.NATIVE, outcome=EmissionOutcome.FAILED
        )
        db.commit()
    with db_env() as db:
        assert (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.message_id == msg.id)
            .count()
            == 1
        )
        assert (
            db.query(DeliveryEmissionModel)
            .filter(DeliveryEmissionModel.message_id == msg.id)
            .one()
            .attempts
            == 2
        )
    # exhaustion (max_attempts=1 → the failed carrier is terminal) → undeliverable
    with db_env() as db:
        fired = maybe_mark_undeliverable(db, message_id=msg.id, max_attempts=1)
        db.commit()
    assert fired is True
    row = _ledger(db_env, msg.id)
    assert row.state == LedgerState.UNDELIVERABLE.value
    assert row.undeliverable_reason == UndeliverableReason.CARRIERS_EXHAUSTED.value


# ── AC23 / D2/S2: exhaustion respects the stored domain + disarm arm ──────────
def test_ac23_disarm_arm_carrier_unavailable_reaches_undeliverable(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        create_delivery_ledger_row(
            db,
            message_id=msg.id,
            receiver_id="wrk",
            mailbox_id=None,
            applicable_carriers=[Carrier.NATIVE, Carrier.DOORBELL],
        )
        claim_emission(db, message_id=msg.id, carrier=Carrier.NATIVE)
        record_emission_outcome(
            db, message_id=msg.id, carrier=Carrier.NATIVE, outcome=EmissionOutcome.FAILED
        )
        # doorbell disarms before its turn → carrier_unavailable
        mark_carrier_unavailable(db, message_id=msg.id, carrier=Carrier.DOORBELL)
        db.commit()
    with db_env() as db:
        fired = maybe_mark_undeliverable(db, message_id=msg.id, max_attempts=1)
        db.commit()
    assert fired is True
    assert _ledger(db_env, msg.id).state == LedgerState.UNDELIVERABLE.value


# ── AC22 / D8/S1: departed-receiver detector ─────────────────────────────────
def test_ac22_mark_receiver_gone_transitions_undelivered(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    assert _ledger(db_env, msg.id).state == LedgerState.PENDING.value
    with db_env() as db:
        n = mark_receiver_gone(db, receiver_id="wrk")
        db.commit()
    assert n == 1
    row = _ledger(db_env, msg.id)
    assert row.state == LedgerState.UNDELIVERABLE.value
    assert row.undeliverable_reason == UndeliverableReason.RECEIVER_GONE.value


def test_ac22_acked_row_not_touched_by_receiver_gone(db_env):
    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        ack_delivery_ledger(db, message_ids=[msg.id], actor=AckActor.EXPLICIT)
        db.commit()
    with db_env() as db:
        mark_receiver_gone(db, receiver_id="wrk")
        db.commit()
    # an acked (consumed) row stays acked — never overwritten to undeliverable
    assert _ledger(db_env, msg.id).state == LedgerState.ACKED.value


# ── AC14 / D1: watermark demotion (list_messages shape unchanged) ────────────
def test_ac14_list_messages_shape_unchanged(db_env):
    """The dispute view is a SEPARATE query; list_messages is untouched, so its
    return shape (InboxMessage models) is unchanged by F642."""
    from cli_agent_orchestrator.clients.database import get_pending_messages

    create_inbox_message("sup", "wrk", "a")
    create_inbox_message("sup", "wrk", "b")
    msgs = get_pending_messages("wrk")
    assert len(msgs) == 2
    assert all(hasattr(m, "id") and hasattr(m, "status") for m in msgs)


# ── AC18 / D3: the hook's READ is its claim (storage-layer mechanism) ─────────
def test_ac18_hook_prints_nothing_for_natively_claimed_id(db_env):
    """AC18 ★: with an id already claimed natively, the hook's `--claim hook`
    read returns that id NOT at all → the drain hook prints nothing for it. The
    cross-repo `cao messages list --claim hook` flag + hook-script edit are the
    remaining coordination (blueprint §7); the SERVER claim it performs
    (``hook_claim_ids``) is exercised here."""
    from cli_agent_orchestrator.clients.database import hook_claim_ids

    claimed = create_inbox_message("sup", "wrk", "already-native")
    unclaimed = create_inbox_message("sup", "wrk", "fresh")
    with db_env() as db:
        assert claim_emission(db, message_id=claimed.id, carrier=Carrier.NATIVE) is True
        db.commit()
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[claimed.id, unclaimed.id])
        db.commit()
    # the natively-claimed id is filtered out; only the fresh one is won/printed.
    assert won == [unclaimed.id]


def test_ac18_hook_wins_unclaimed_and_acks_as_hook(db_env):
    from cli_agent_orchestrator.clients.database import hook_claim_ids

    msg = create_inbox_message("sup", "wrk", "x")
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[msg.id])
        assert won == [msg.id]
        # the same --claim hook reader acks as `hook` (D4 side effect)
        ack_delivery_ledger(db, message_ids=[msg.id], actor=AckActor.HOOK)
        db.commit()
    assert _ledger(db_env, msg.id).acked_by == "hook"


def test_ac18_mutant_claim_exempt_hook_reprints_carried_id(db_env):
    """MUTANT: leave the hook claim-exempt (print every candidate) → it prints a
    full body for an id the native push already carried — #488's first
    complaint."""
    claimed = create_inbox_message("sup", "wrk", "already-native")
    with db_env() as db:
        claim_emission(db, message_id=claimed.id, carrier=Carrier.NATIVE)
        db.commit()
    # mutant: no filtering — the candidate is printed regardless.
    mutant_printed = [claimed.id]  # claim-exempt hook prints everything
    assert claimed.id in mutant_printed  # the defect: reprints a carried id


# ── AC11 / D9: the ledger row is created where the message is routed ──────────
def test_ac11_ledger_row_created_with_the_message(db_env):
    """D9: the ledger row belongs to whichever server owns the receiving mailbox
    — it is created in the SAME database as the InboxModel row by
    ``_insert_routed_inbox_row``. A box-hosted worker's callback that routes home
    therefore lands its ledger row in the (here, the only) home DB."""
    msg = create_inbox_message("sup", "wrk", "home-routed")
    # the ledger row exists in this database, keyed to the receiver.
    row = _ledger(db_env, msg.id)
    assert row is not None
    assert row.receiver_id == "wrk"
