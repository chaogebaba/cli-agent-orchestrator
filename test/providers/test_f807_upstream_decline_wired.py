"""F807 (#664) — wire F790's dormant upstream BUSY-class / low-context decline.

F790 built an upstream decline (BUSY-class + command_exit PROC_EXITED are recorded
but NEVER enqueued to the supervisor mailbox) but left it DORMANT in production:
the production ``ConditionDelivery`` was constructed with NO ``log_store``, and
``_inbox_declined`` early-returned ``False`` whenever the store was absent — so
the decline never ran and the liveness rows WERE enqueued (only downstream hooks
hid them, while the native wake ring still rang the seat).

F807 wires it:

* (a) a BUSY ``thinking_spinner`` fires fleet + bus but is NEVER enqueued to the
  seat inbox, with or without a durable log store;
* (b) the ANOMALY-class deaths (CAPPED / DIALOG_BLOCKED / a real PROC_EXITED)
  still enqueue exactly one inbox push, unchanged;
* (c) a CONTEXT_EXHAUSTED ``low_context_tip`` (the soft "/compact" tip) AND a
  CONTEXT_EXHAUSTED ``footer_percent_status`` (the plaintext-only status-bar
  reading, made ADVISORY by F836 r6 #693 because a pasted/truncated full snapshot
  is indistinguishable from live chrome) are BOTH declined — no inbox row — while
  the CONTEXT_EXHAUSTED kind map stays ``inbox=True`` so the decline is a
  subtype-scoped predicate, not a map-row flip;
* (d) the PRODUCTION construction (``StatusMonitor._get_condition_delivery``)
  wires a real ``DbConditionLogStore``.
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
)
from cli_agent_orchestrator.clients.delivery_ledger import (
    drain_class_declines_inbox,
    surfaces_for_kind,
)
from cli_agent_orchestrator.providers.condition import (
    Condition,
    ConditionDelivery,
    ConditionKind,
    Confidence,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


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


def _cond(kind, subtype="s", conf=Confidence.HIGH, evidence="e"):
    return Condition(
        kind=kind, provider="codex", subtype=subtype, evidence=evidence, confidence=conf
    )


def _rows(db_env, terminal_id):
    with db_env() as db:
        rows = (
            db.query(ConditionLedgerModel)
            .filter(ConditionLedgerModel.terminal_id == terminal_id)
            .order_by(ConditionLedgerModel.id.asc())
            .all()
        )
        return [
            (r.decision, r.kind, r.subtype, r.surfaces, r.suppressed_reason, r.inbox_message_id)
            for r in rows
        ]


# ══ (a) BUSY thinking_spinner → no inbox row, fleet event still emitted ══════
def test_a_busy_thinking_spinner_no_inbox_fleet_fires(db_env):
    inbox, fleet = [], []
    d = ConditionDelivery(
        fleet_sink=lambda t, lbl: fleet.append((t, lbl)),
        inbox_sink=lambda t, c: inbox.append((t, c.subtype)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver("wrk", _cond(ConditionKind.BUSY, subtype="thinking_spinner"), epoch=1)
    assert res.delivered is True
    assert res.inbox_pushes == 0
    assert inbox == []  # NEVER enqueued to the seat
    assert fleet == [("wrk", "BUSY")]  # fleet field still moved
    # durable row: delivered, fleet+bus only, suppressed_reason marks the decline
    rows = _rows(db_env, "wrk")
    assert ("delivered", "BUSY", "thinking_spinner", "fleet,bus", "busy_class", None) in rows


def test_a_busy_declined_even_without_log_store():
    """F807 core: the decline is a pure (kind, subtype) routing decision and no
    longer gated behind a wired log store. WITHOUT a store, a BUSY ping is still
    declined — the dormancy this feature closes."""
    inbox = []
    d = ConditionDelivery(inbox_sink=lambda t, c: inbox.append(c.subtype))  # NO log_store
    res = d.deliver("wrk", _cond(ConditionKind.BUSY, subtype="asterisk_spinner"), epoch=1)
    assert res.inbox_pushes == 0
    assert inbox == []


def test_a_mutant_early_return_false_reenqueues_busy():
    """MUTANT: restore F790's ``if log_store is None: return False`` gate → with
    no store the BUSY ping is re-enqueued. Pins the fall-through fix."""
    inbox = []
    d = ConditionDelivery(inbox_sink=lambda t, c: inbox.append(c.subtype))  # NO log_store

    def mutant_inbox_declined(kind, subtype):
        if d._log_store is None:  # the reverted dormancy gate
            return False
        return drain_class_declines_inbox(kind, subtype) or not surfaces_for_kind(kind).inbox

    d.deliver("wrk", _cond(ConditionKind.BUSY, subtype="thinking_spinner"), epoch=1)
    assert inbox == []  # real behaviour: declined
    assert mutant_inbox_declined("BUSY", "thinking_spinner") is False  # mutant re-enqueues


# ══ (b) CAPPED / DIALOG_BLOCKED / real PROC_EXITED death → inbox row as today ═
@pytest.mark.parametrize(
    ("kind", "subtype"),
    [
        (ConditionKind.CAPPED, "usage_limit_hard"),
        (ConditionKind.DIALOG_BLOCKED, "trust_dir_dialog"),
        (ConditionKind.PROC_EXITED, "shell_baseline_return"),
    ],
)
def test_b_anomaly_deaths_still_enqueue(db_env, kind, subtype):
    inbox = []
    d = ConditionDelivery(
        inbox_sink=lambda t, c: inbox.append((t, c.kind.value)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver("wrk", _cond(kind, subtype=subtype), epoch=1)
    assert res.inbox_pushes == 1
    assert inbox == [("wrk", kind.value)]
    rows = _rows(db_env, "wrk")
    # a real push: inbox surface recorded, no busy_class suppression
    assert ("delivered", kind.value, subtype, "fleet,bus,inbox", None, None) in rows


# ══ (c) low_context_tip → no row; hard exhaustion → row ══════════════════════
def test_c_low_context_tip_declined(db_env):
    inbox, fleet = [], []
    d = ConditionDelivery(
        fleet_sink=lambda t, lbl: fleet.append((t, lbl)),
        inbox_sink=lambda t, c: inbox.append(c.subtype),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver(
        "wrk",
        _cond(ConditionKind.CONTEXT_EXHAUSTED, subtype="low_context_tip", conf=Confidence.MEDIUM),
        epoch=1,
    )
    assert res.delivered is True
    assert res.inbox_pushes == 0
    assert inbox == []  # the soft tip NEVER reaches the seat
    assert fleet == [("wrk", "CONTEXT_EXHAUSTED")]  # fleet still moved
    rows = _rows(db_env, "wrk")
    # surfaces reflects the ACTUAL routing (fleet+bus, no inbox) though the
    # kind-keyed map keeps inbox=True for hard exhaustion
    assert (
        "delivered",
        "CONTEXT_EXHAUSTED",
        "low_context_tip",
        "fleet,bus",
        "busy_class",
        None,
    ) in rows


def test_c_footer_percent_status_declines_inbox(db_env):
    """F836 r6 (#693): footer_percent_status is now ADVISORY — the classifier's
    only input is plaintext pane bytes, so a pasted/truncated full snapshot is
    indistinguishable from live chrome (codex EMPIRICAL-GATE-NO). It caps at
    MEDIUM at the producer AND declines the inbox (acting) leg here, exactly like
    low_context_tip: it surfaces on fleet/bus but never wakes the seat, so a
    plaintext-only footer match can never trigger a hard stop."""
    inbox = []
    d = ConditionDelivery(
        inbox_sink=lambda t, c: inbox.append(c.subtype),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver(
        "wrk", _cond(ConditionKind.CONTEXT_EXHAUSTED, subtype="footer_percent_status"), epoch=1
    )
    assert res.inbox_pushes == 0
    assert inbox == []  # advisory: never reaches the seat


def test_c_predicate_distinguishes_subtypes():
    """F836 r6 (#693): the drain-class predicate now declines BOTH the
    low_context_tip AND the footer_percent_status subtypes of CONTEXT_EXHAUSTED —
    both are plaintext-only advisories, never hard stops. Any OTHER
    CONTEXT_EXHAUSTED subtype keeps its inbox leg. Pins the subtype split."""
    assert drain_class_declines_inbox("CONTEXT_EXHAUSTED", "low_context_tip") is True
    assert drain_class_declines_inbox("CONTEXT_EXHAUSTED", "footer_percent_status") is True
    # a hypothetical hard subtype (none plaintext-only) keeps its inbox leg
    assert drain_class_declines_inbox("CONTEXT_EXHAUSTED", "some_future_hard_signal") is False
    # the kind-keyed map is UNCHANGED — the decline is a SUBTYPE predicate, not a
    # map-row flip, so a genuinely-independent-signal subtype could still enqueue.
    assert surfaces_for_kind("CONTEXT_EXHAUSTED").inbox is True


def test_c_mutant_flip_kind_map_would_break_context_exhausted():
    """MUTANT: flipping the CONTEXT_EXHAUSTED *map row* to inbox=False (instead of
    the subtype predicate) would silence ANY future non-plaintext hard exhaustion
    at the kind level. This asserts the kind map stays inbox=True so the r6 fix is
    a SUBTYPE-scoped decline, not a map-row flip."""
    real = surfaces_for_kind("CONTEXT_EXHAUSTED")
    assert real.inbox is True  # mutant map-row flip would make this False
    # yet the plaintext-only advisories are declined via the subtype predicate
    assert drain_class_declines_inbox("CONTEXT_EXHAUSTED", "low_context_tip") is True
    assert drain_class_declines_inbox("CONTEXT_EXHAUSTED", "footer_percent_status") is True


# ══ (d) the production construction wires a real DbConditionLogStore ═════════
def test_d_production_construction_has_log_store():
    monitor = StatusMonitor()
    delivery = monitor._get_condition_delivery()
    assert isinstance(delivery._log_store, DbConditionLogStore)
    # idempotent: the ONE seam is cached, still carrying the store
    assert monitor._get_condition_delivery() is delivery
    assert delivery._log_store is not None


def test_d_mutant_omitted_log_store_is_none():
    """MUTANT: revert the wiring (construct without log_store) → _log_store is
    None and the durable ledger + F790 spine are dormant again. Pins the wire."""
    bare = ConditionDelivery(inbox_sink=lambda t, c: None)  # the reverted construction
    assert bare._log_store is None  # what the production seam must NOT be
    assert isinstance(StatusMonitor()._get_condition_delivery()._log_store, DbConditionLogStore)
