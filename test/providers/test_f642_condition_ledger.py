"""F642 — condition-plane durable decision log through the real ConditionDelivery seam.

Drives ``ConditionDelivery`` with the DB-backed ``DbConditionLogStore`` and
asserts against the real ``condition_ledger`` table + inbox side effects:

* AC5  — a BUSY transition fires fleet+bus but NO inbox push; a `busy_class`
  `delivered` row is written; a CAPPED on the same terminal still pushes once.
  Mutant: the unconditional inbox sink (no map) → BUSY pushes.
* AC9  — de-dup survives a cao-server restart (a fresh ConditionDelivery with an
  empty ``_last`` dict). Mutant: the in-memory dict → the restart re-arms.
* AC20 — a BUSY suppression AND a de-dup suppression each produce a durable
  condition_ledger row with NO message id.
* AC21 — the four sequences through the seam with the durable store.
* AC24 — a below-gate (LOW) condition writes a `decision='gated'` row and moves
  no surface. Mutant: write no row for gated → no durable trace.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    ConditionLedgerModel,
    DbConditionLogStore,
    create_terminal,
)
from cli_agent_orchestrator.providers.condition import (
    Condition,
    ConditionDelivery,
    ConditionKind,
    Confidence,
)


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    return sessions


def _cond(kind, subtype="s", conf=Confidence.HIGH):
    return Condition(kind=kind, provider="codex", subtype=subtype, evidence="e", confidence=conf)


def _delivery(inbox_calls):
    """A ConditionDelivery wired with the durable store and a recording inbox
    sink (counts pushes)."""
    return ConditionDelivery(
        inbox_sink=lambda t, c: inbox_calls.append((t, c.kind.value)),
        log_store=DbConditionLogStore(),
    )


def _decisions(db_env, terminal_id):
    with db_env() as db:
        rows = (
            db.query(ConditionLedgerModel)
            .filter(ConditionLedgerModel.terminal_id == terminal_id)
            .order_by(ConditionLedgerModel.id.asc())
            .all()
        )
        return [(r.decision, r.kind, r.suppressed_reason, r.inbox_message_id) for r in rows]


# ── AC5 / D5: BUSY makes no inbox row; CAPPED still pushes ────────────────────
def test_ac5_busy_no_inbox_push_capped_pushes(db_env):
    inbox = []
    d = _delivery(inbox)
    # BUSY: fleet+bus fire, inbox declined
    res_busy = d.deliver("wrk", _cond(ConditionKind.BUSY), epoch=1)
    assert res_busy.delivered is True  # it's still a `delivered` decision
    assert res_busy.inbox_pushes == 0  # but no inbox leg
    assert inbox == []
    # CAPPED on the same terminal still pushes exactly once
    res_capped = d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert res_capped.inbox_pushes == 1
    assert inbox == [("wrk", "CAPPED")]
    # audit: a busy_class delivered row + a capped delivered row
    decisions = _decisions(db_env, "wrk")
    assert ("delivered", "BUSY", "busy_class", None) in decisions
    assert ("delivered", "CAPPED", None, None) in decisions


def test_ac5_mutant_unconditional_sink_pushes_busy(db_env):
    """MUTANT: no log_store (the pre-F642 unconditional inbox sink) → BUSY pushes,
    reintroducing #494's two decision-free BUSY pushes."""
    inbox = []
    mutant = ConditionDelivery(inbox_sink=lambda t, c: inbox.append((t, c.kind.value)))
    mutant.deliver("wrk", _cond(ConditionKind.BUSY), epoch=1)
    assert inbox == [("wrk", "BUSY")]  # the defect


# ── AC9 ★ / D7: de-dup survives a restart ────────────────────────────────────
def test_ac9_dedup_survives_restart(db_env):
    inbox = []
    d1 = _delivery(inbox)
    r1 = d1.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert r1.inbox_pushes == 1
    # --- simulate a cao-server restart: a brand-new ConditionDelivery with an
    #     EMPTY in-memory _last dict, but the SAME durable condition_ledger. ---
    d2 = _delivery(inbox)
    r2 = d2.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert r2.delivered is False  # suppressed by the durable log
    assert r2.reason == "deduped_same_epoch"
    assert inbox == [("wrk", "CAPPED")]  # only ONE push across the restart


def test_ac9_mutant_in_memory_dict_rearms_on_restart(db_env):
    """MUTANT: the in-memory dict (no log_store) → a 'restart' (new instance)
    re-arms and the seat is pushed twice for one condition."""
    inbox = []
    m1 = ConditionDelivery(inbox_sink=lambda t, c: inbox.append((t, c.kind.value)))
    m1.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    m2 = ConditionDelivery(inbox_sink=lambda t, c: inbox.append((t, c.kind.value)))
    m2.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert inbox == [("wrk", "CAPPED"), ("wrk", "CAPPED")]  # pushed twice — the defect


# ── AC20 / B3/B1: suppression is expressible with no message id ───────────────
def test_ac20_busy_and_dedup_produce_durable_rows_no_message_id(db_env):
    inbox = []
    d = _delivery(inbox)
    # BUSY suppression (inbox declined) — durable row, no message id
    d.deliver("wrk", _cond(ConditionKind.BUSY), epoch=1)
    # de-dup suppression — CAPPED twice
    d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    decisions = _decisions(db_env, "wrk")
    # a busy_class delivered row (no message id) and a deduped row (no message id)
    assert any(dec == "delivered" and reason == "busy_class" and mid is None
               for dec, _k, reason, mid in decisions)
    assert any(dec == "deduped" and mid is None for dec, _k, _r, mid in decisions)


# ── AC21 ★ / D7: the four sequences through the seam ──────────────────────────
def test_ac21a_capped_then_capped(db_env):
    inbox = []
    d = _delivery(inbox)
    d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    r2 = d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert r2.delivered is False
    assert inbox == [("wrk", "CAPPED")]  # only first pushes


def test_ac21b_capped_dialog_capped(db_env):
    inbox = []
    d = _delivery(inbox)
    d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    d.deliver("wrk", _cond(ConditionKind.DIALOG_BLOCKED), epoch=1)
    r3 = d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert r3.delivered is True  # all three deliver
    assert inbox == [("wrk", "CAPPED"), ("wrk", "DIALOG_BLOCKED"), ("wrk", "CAPPED")]


def test_ac21c_capped_clear_capped(db_env):
    inbox = []
    d = _delivery(inbox)
    d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    d.deliver("wrk", None, epoch=1)  # clear
    r3 = d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert r3.delivered is True  # the clear re-armed
    assert inbox == [("wrk", "CAPPED"), ("wrk", "CAPPED")]
    # the clear wrote a `cleared` row (NULL tuple)
    assert any(dec == "cleared" and k is None for dec, k, _r, _m in _decisions(db_env, "wrk"))


def test_ac21d_capped_gated_capped_suppresses(db_env):
    """(d) ★ the common shell-baseline interleave: CAPPED → gated(LOW) → CAPPED
    SUPPRESSES the second, because the `gated` row is skipped."""
    inbox = []
    d = _delivery(inbox)
    d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    # a LOW-confidence PROC_EXITED (shell_baseline_return) — below the gate
    d.deliver(
        "wrk",
        _cond(ConditionKind.PROC_EXITED, subtype="shell_baseline_return", conf=Confidence.LOW),
        epoch=1,
    )
    r3 = d.deliver("wrk", _cond(ConditionKind.CAPPED), epoch=1)
    assert r3.delivered is False  # suppressed
    assert inbox == [("wrk", "CAPPED")]  # only the first CAPPED pushed
    # audit: a gated row exists and is skipped by the comparison
    decisions = _decisions(db_env, "wrk")
    assert any(dec == "gated" for dec, _k, _r, _m in decisions)


# ── AC24 / D7/B1: the gate leaves a durable trace ─────────────────────────────
def test_ac24_gated_condition_writes_row_moves_nothing(db_env):
    inbox = []
    fleet = []
    d = ConditionDelivery(
        fleet_sink=lambda t, lbl: fleet.append((t, lbl)),
        inbox_sink=lambda t, c: inbox.append((t, c.kind.value)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver(
        "wrk",
        _cond(ConditionKind.PROC_EXITED, subtype="shell_baseline_return", conf=Confidence.LOW),
        epoch=1,
    )
    assert res.delivered is False
    assert inbox == []  # no inbox
    assert fleet == []  # no fleet field moved
    # but a durable `gated` row exists — the one otherwise-invisible outcome
    decisions = _decisions(db_env, "wrk")
    assert [dec for dec, _k, _r, _m in decisions] == ["gated"]


def test_ac24_mutant_no_row_for_gated_leaves_no_trace(db_env):
    """MUTANT: write no row for gated conditions (no log_store) → the outcome has
    no durable trace anywhere."""
    inbox = []
    mutant = ConditionDelivery(inbox_sink=lambda t, c: inbox.append((t, c.kind.value)))
    mutant.deliver(
        "wrk",
        _cond(ConditionKind.PROC_EXITED, subtype="shell_baseline_return", conf=Confidence.LOW),
        epoch=1,
    )
    assert _decisions(db_env, "wrk") == []  # no trace — the defect
