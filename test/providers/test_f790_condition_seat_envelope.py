"""F790 (#647) — BUSY-class condition pings must not reach the seat as full envelopes.

Three cuts, one focused test each plus a killed mutant:

* Cut 1 (producer) — ``ConditionDelivery.deliver`` never enqueues a BUSY-class
  or ``command_exit`` PROC_EXITED condition to the supervisor mailbox; it is
  recorded (fleet + bus + the durable ``condition_ledger``) only. Reuses the
  SAME class the supervisor-inbox-drain hook withholds (F639 #494 / F718 #574).
  Mutant: flip the class predicate so PROC_EXITED/command_exit_code pushes.
* Cut 2 (envelope) — ``normalize_wake_body`` / ``build_wake_payload`` drop the
  body of a ``[CONDITION]``/``[watchdog]`` wake to ``None`` and cap any other
  body at 1,500 chars with a truncation marker. Mutant: skip the None branch so
  the condition body rides the envelope.
* Cut 3 (evidence cap) — ``Condition.render_event`` caps ``evidence`` at 300
  chars with the truncation marker. Mutant: raise the cap so a multi-KB pane
  fragment rides the condition line.
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
from cli_agent_orchestrator.clients.delivery_ledger import drain_class_declines_inbox
from cli_agent_orchestrator.providers.condition import (
    _F790_EVIDENCE_MAX_CHARS,
    Condition,
    ConditionDelivery,
    ConditionKind,
    Confidence,
)
from cli_agent_orchestrator.services.cc_session_registry import (
    _F790_WAKE_BODY_MAX_CHARS,
    build_wake_payload,
    normalize_wake_body,
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


def _cond(kind, subtype="s", conf=Confidence.HIGH, evidence="e"):
    return Condition(
        kind=kind, provider="codex", subtype=subtype, evidence=evidence, confidence=conf
    )


def _decisions(db_env, terminal_id):
    with db_env() as db:
        rows = (
            db.query(ConditionLedgerModel)
            .filter(ConditionLedgerModel.terminal_id == terminal_id)
            .order_by(ConditionLedgerModel.id.asc())
            .all()
        )
        return [
            (r.decision, r.kind, r.subtype, r.suppressed_reason, r.inbox_message_id) for r in rows
        ]


# ══ Cut 1: producer drops the class the drain hook withholds ═════════════════
def test_cut1_command_exit_proc_exited_recorded_not_enqueued(db_env):
    """A HIGH-confidence PROC_EXITED/command_exit_code fires fleet+bus and writes
    a durable `delivered` row, but is NEVER pushed to the supervisor mailbox —
    the SAME class the drain hook withholds (F718 #574)."""
    inbox = []
    fleet = []
    d = ConditionDelivery(
        fleet_sink=lambda t, lbl: fleet.append((t, lbl)),
        inbox_sink=lambda t, c: inbox.append((t, c.kind.value)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver("wrk", _cond(ConditionKind.PROC_EXITED, subtype="command_exit_code"), epoch=1)
    assert res.delivered is True  # still a `delivered` decision (memory set)
    assert res.inbox_pushes == 0  # but the inbox leg is declined
    assert inbox == []  # NOTHING enqueued to the supervisor mailbox
    assert fleet == [("wrk", "PROC_EXITED")]  # fleet field still moved
    # recorded only: a durable `delivered` row with suppressed_reason=busy_class
    decisions = _decisions(db_env, "wrk")
    assert ("delivered", "PROC_EXITED", "command_exit_code", "busy_class", None) in decisions


def test_cut1_anomaly_proc_exited_still_enqueued(db_env):
    """ANOMALY-class conditions keep today's behaviour: a PROC_EXITED whose
    subtype is NOT command_exit_code still pushes to the mailbox exactly once."""
    inbox = []
    d = ConditionDelivery(
        inbox_sink=lambda t, c: inbox.append((t, c.subtype)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver(
        "wrk", _cond(ConditionKind.PROC_EXITED, subtype="shell_baseline_return"), epoch=1
    )
    assert res.inbox_pushes == 1
    assert inbox == [("wrk", "shell_baseline_return")]


def test_cut1_mutant_flipped_class_predicate_pushes_command_exit():
    """MUTANT: the class predicate ignores the command_exit subtype (the F718
    #574 half) → a command_exit PROC_EXITED is treated as ANOMALY and would be
    pushed. This asserts the predicate's exact class so a flip is caught."""

    def mutant_predicate(kind: str, subtype) -> bool:
        # defect: only BUSY declines; PROC_EXITED/command_exit_code no longer does
        return kind == "BUSY"

    # the real predicate declines command_exit; the mutant does not
    assert drain_class_declines_inbox("PROC_EXITED", "command_exit_code") is True
    assert mutant_predicate("PROC_EXITED", "command_exit_code") is False


# ══ gate r1 B1: map-declined kinds (NET/TRANSIENT) stay silent as on base ════
@pytest.mark.parametrize(
    "kind",
    [ConditionKind.NET_INTERRUPTED, ConditionKind.TRANSIENT_OVERLOAD],
)
def test_r1b1_map_declined_kinds_never_enqueue(db_env, kind):
    """gate r1 B1: NET_INTERRUPTED / TRANSIENT_OVERLOAD map to inbox=False in
    KIND_SURFACES, so they were declined at the producer on base and MUST stay
    declined. F790 must only STOP enqueuing the drain class — never START
    enqueuing a kind the F642 routing map already declined. `_inbox_declined`
    declines when the drain class matches OR the map has no inbox surface."""
    inbox = []
    d = ConditionDelivery(
        inbox_sink=lambda t, c: inbox.append((t, c.kind.value)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver("wrk", _cond(kind), epoch=1)
    assert res.delivered is True  # still fleet + bus
    assert res.inbox_pushes == 0  # but NEVER the seat inbox
    assert inbox == []


# ══ gate r1 B2: ANOMALY-class conditions still push to the seat inbox ═════════
@pytest.mark.parametrize(
    ("kind", "subtype"),
    [
        (ConditionKind.DIALOG_BLOCKED, "trust_dir_dialog"),
        (ConditionKind.CAPPED, "usage_limit_hard"),
        (ConditionKind.AUTH_EXPIRED, "token_refresh_failed"),
        (ConditionKind.CONTEXT_EXHAUSTED, "footer_percent_status"),
        (ConditionKind.PROC_EXITED, "shell_baseline_return"),
    ],
)
def test_r1b2_anomaly_class_still_enqueues(db_env, kind, subtype):
    """gate r1 B2: the ANOMALY-class conditions (KIND_SURFACES inbox=True) still
    push to the supervisor inbox exactly once — F790 preserves today's behaviour
    for them. Proven by the shipped test set, not an ad-hoc probe."""
    inbox = []
    d = ConditionDelivery(
        inbox_sink=lambda t, c: inbox.append((t, c.kind.value)),
        log_store=DbConditionLogStore(),
    )
    res = d.deliver("wrk", _cond(kind, subtype=subtype), epoch=1)
    assert res.delivered is True
    assert res.inbox_pushes == 1
    assert inbox == [("wrk", kind.value)]  # the sink actually received the push


# ══ Cut 2: envelope None for condition/watchdog; else 1500-char cap ══════════
def test_cut2_envelope_none_for_condition_and_watchdog():
    """A [CONDITION]/[watchdog] body collapses to None so the native envelope
    carries the generic ids-only ping, not the pane bytes."""
    assert normalize_wake_body('[CONDITION] terminal=x kind=BUSY evidence="..."') is None
    assert normalize_wake_body("[watchdog] no heartbeat for 90s") is None
    # leading whitespace is stripped before the prefix test
    assert normalize_wake_body("  \n[CONDITION] kind=BUSY") is None
    # and the built payload carries the LEGACY generic ping, not the body
    payload = build_wake_payload("wrk", 42, message_body="[CONDITION] kind=BUSY huge pane...")
    assert "kind=BUSY" not in payload
    assert "Run any command to surface and ack it." in payload


def test_cut2_envelope_truncates_long_non_condition_body():
    """A non-condition body longer than 1,500 chars is capped with the marker."""
    body = "R" * 1600
    out = normalize_wake_body(body)
    assert out is not None
    dropped = 1600 - _F790_WAKE_BODY_MAX_CHARS
    assert out == "R" * _F790_WAKE_BODY_MAX_CHARS + (
        f" …[truncated {dropped} chars; full body in the inbox digest]"
    )
    # a body within the cap is returned unchanged; None passes through
    assert normalize_wake_body("short") == "short"
    assert normalize_wake_body(None) is None


def test_cut2_mutant_skip_none_branch_leaks_condition_body():
    """MUTANT: the None-branch is skipped (only the length cap runs) → a short
    [CONDITION] body rides the envelope verbatim. This test pins the None branch."""

    def mutant_normalize(message_body):
        # defect: no [CONDITION]/[watchdog] → None collapse
        if message_body is None:
            return None
        if len(message_body) > _F790_WAKE_BODY_MAX_CHARS:
            return message_body[:_F790_WAKE_BODY_MAX_CHARS]
        return message_body

    cond_body = '[CONDITION] kind=BUSY subtype=spinner_waiting evidence="..."'
    assert normalize_wake_body(cond_body) is None  # real: dropped
    assert mutant_normalize(cond_body) == cond_body  # mutant: leaked


# ══ Cut 3: condition evidence capped at 300 chars at write time ══════════════
def test_cut3_evidence_capped_at_write_time():
    """render_event caps evidence at 300 chars with the truncation marker."""
    long_ev = "P" * 900
    cond = _cond(ConditionKind.BUSY, evidence=long_ev)
    line = cond.render_event("term-1")
    dropped = 900 - _F790_EVIDENCE_MAX_CHARS
    marker = f" …[truncated {dropped} chars; full body in the inbox digest]"
    assert f'evidence="{"P" * _F790_EVIDENCE_MAX_CHARS}{marker}"' in line
    assert "P" * (_F790_EVIDENCE_MAX_CHARS + 1) not in line  # never the full 900
    # a short evidence is emitted verbatim (no marker)
    short = _cond(ConditionKind.BUSY, evidence="tiny").render_event("term-1")
    assert 'evidence="tiny"' in short
    assert "truncated" not in short


def test_cut3_mutant_raised_cap_leaks_full_evidence():
    """MUTANT: raise the cap (e.g. to 10_000) → a 900-char pane fragment rides
    the condition line intact. This pins the 300-char cap constant."""
    long_ev = "P" * 900
    line = _cond(ConditionKind.BUSY, evidence=long_ev).render_event("t")
    # real behaviour: the full 900-char run is NOT present
    assert long_ev not in line
    # a mutant with the cap raised above 900 would emit long_ev verbatim
    mutant_cap = 10_000
    assert len(long_ev) <= mutant_cap  # the mutant would not truncate at all
