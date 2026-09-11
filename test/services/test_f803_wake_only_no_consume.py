"""F803 #660: a native wake ring must NOT consume an inbox row on an ids-only
ping.

The #640 (F783) defect this pins: consumption was gated on the raw
``message_body`` *argument* of ``_attempt_native_ring``. But the socket payload
is built by ``build_wake_payload`` -> ``normalize_wake_body``, which can carry
NO body even when the argument is non-None:

  * ``teammate_push=false`` -> the caller synthesizes a ``[cao-fleet]`` digest
    argument, but the seat is woken with ids + a count and NO text; and
  * a ``[CONDITION]``/``[watchdog]`` body collapses to ``None`` under F790.

In both cases the seat received no text, so the row MUST stay ``pending`` for the
drain hook to claim and inject the body — recording ``native_consumed`` there was
the bug (rows flipped to delivered, ``hook_claim_ids`` returned [], nothing
surfaced; live callbacks 4723/4727/4729 and the 98-min stall of 4754).

Option 1 (decided): consumption attaches ONLY when the body is actually carried
(``normalize_wake_body(message_body) is not None``). An ids-only ring records a
``wake_only`` NATIVE emission that leaves the row pending and does NOT mute the
hook. This suite drives the REAL ``_attempt_native_ring`` (socket seam mocked,
same harness as the F783 r1 tests) and the ledger primitives directly.

Fixtures mirror ``test_f783_native_consumption.py`` (in-memory SQLite bound
through both the ``database`` and ``mailbox_service`` module symbols).
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import cli_agent_orchestrator.services.doorbell_service as _dbs
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryEmissionModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    create_delivery_ledger_row,
    hook_claim_ids,
)
from cli_agent_orchestrator.clients.delivery_ledger import (
    _TERMINAL_EMISSION_OUTCOMES,
    Carrier,
    EmissionOutcome,
    EmissionView,
    is_carrier_exhausted,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import (
    list_messages,
    record_native_wake_only,
)

MB = "mb_5ea77e12"
SEAT = "5ea77e12"


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions, raising=False)
    database.clear_terminal_metadata_cache()
    monkeypatch.setattr(
        database, "_remove_supervisor_pending_flag_if_drained", lambda: None, raising=False
    )
    import cli_agent_orchestrator.services.nudge_discipline as _nd

    monkeypatch.setattr(_nd.nudge_discipline, "on_cursor_advance", lambda *a, **k: None)
    with sessions() as db:
        _make_mailbox(db, consumed_through_id=0)
        db.commit()
    return sessions


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_mailbox(db: Any, *, consumed_through_id: int) -> MailboxModel:
    row = MailboxModel(
        id=MB,
        session_name="cao-orch",
        role="supervisor",
        current_terminal_id=SEAT,
        generation=1,
        consumed_through_id=consumed_through_id,
        cc_inbox_path=None,
        cc_inbox_path_version=0,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=MB,
            generation=1,
            terminal_id=SEAT,
            published_at=_now(),
        )
    )
    return row


def _add_row(db: Any, row_id: int, *, status: str = MessageStatus.PENDING.value) -> InboxModel:
    row = InboxModel(
        id=row_id,
        sender_id="wrk",
        receiver_id=SEAT,
        logical_receiver_id=MB,
        message=f"body-{row_id}",
        status=status,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    create_delivery_ledger_row(
        db,
        message_id=row_id,
        receiver_id=SEAT,
        mailbox_id=MB,
        applicable_carriers=list(Carrier),
    )
    return row


def _status(sessions, row_id: int) -> str:
    with sessions() as db:
        return str(db.query(InboxModel.status).filter(InboxModel.id == row_id).scalar())


def _cursor(sessions) -> int:
    with sessions() as db:
        return int(
            db.query(MailboxModel.consumed_through_id).filter(MailboxModel.id == MB).scalar()
        )


def _emissions(sessions, row_id: int) -> dict[str, str]:
    with sessions() as db:
        rows = (
            db.query(DeliveryEmissionModel).filter(DeliveryEmissionModel.message_id == row_id).all()
        )
        return {r.carrier: r.outcome for r in rows}


def _trace_kinds(sessions, row_id: int) -> list[str]:
    with sessions() as db:
        return [
            r.kind
            for r in db.query(InboxMessageTraceEventModel)
            .filter(InboxMessageTraceEventModel.message_id == row_id)
            .all()
        ]


class _FakeRecord:
    pid = 4242
    proc_start = 111
    version = "1.0.0"
    status_updated_at = "2026-01-01T00:00:00Z"

    def __init__(self, socket_path: str = "/tmp/does-not-matter.sock") -> None:
        self.messaging_socket_path = socket_path


@contextlib.contextmanager
def _native_socket(monkeypatch, *, write_err, verify=True, socket_path="/x.sock"):
    """Mock the cc_session_registry seam so the REAL _attempt_native_ring runs
    end-to-end without a live Claude Code session. normalize_wake_body is the
    REAL one (not mocked) — the whole point of #660 is what it does to the body."""
    import cli_agent_orchestrator.services.cc_session_registry as _reg

    rec = _FakeRecord(socket_path)
    monkeypatch.setattr(
        _dbs, "get_terminal_metadata", lambda tid: {"tmux_session": "s", "tmux_window": "w"}
    )
    monkeypatch.setattr(_reg, "resolve_target", lambda *a, **k: _reg.ResolveResult(record=rec))
    monkeypatch.setattr(_reg, "check_version_guard", lambda record: None)
    monkeypatch.setattr(_reg, "read_peer_token", lambda *a, **k: None)
    monkeypatch.setattr(_reg, "write_to_socket", lambda *a, **k: write_err)
    monkeypatch.setattr(_reg, "verify_wake", lambda *a, **k: verify)
    yield


# ── AC(a): ids-only ring → row stays pending, hook_claim wins it ──────────────


# WP-ARCH 3c K3c: the two coalesced end-to-end arms that lived here drove the
# REAL DoorbellCoalesceService into _attempt_native_ring. The coalescer is
# deleted (the delivery tick is the seat's single carrier), so the body_carried
# decision is now pinned only at _attempt_native_ring directly, by the arms below.


def test_ac_a_condition_body_collapses_to_ids_only_stays_pending(db_env, monkeypatch):
    """A `[CONDITION]` body collapses to None in normalize_wake_body (F790): the
    seat gets an ids-only ping. The row MUST stay pending and the hook MUST win
    — recording native_consumed here was the #660 bug."""
    with db_env() as db:
        _add_row(db, 301)
        db.commit()

    with _native_socket(monkeypatch, write_err=None, verify=True):
        decision = _dbs._attempt_native_ring(
            SEAT, 301, message_body="[CONDITION] BUSY subtype=capped epoch=3"
        )

    # The ring still fired (that IS the wake) …
    assert decision == "rang"
    # … but it carried no body, so the row is untouched.
    assert _status(db_env, 301) == MessageStatus.PENDING.value
    assert _cursor(db_env) == 0
    # A wake_only NATIVE emission is recorded (the wake DID fire) — NOT succeeded.
    assert _emissions(db_env, 301) == {Carrier.NATIVE.value: EmissionOutcome.WAKE_ONLY.value}
    # The hook WINS the id (fallback owns it) — the whole point of #660.
    with db_env() as db:
        assert hook_claim_ids(db, candidate_ids=[301]) == [301]
        db.commit()
    # Still listed as pending.
    listed = [int(i["id"]) for i in list_messages(SEAT, status=MessageStatus.PENDING)["items"]]
    assert 301 in listed


def test_ac_a_bodyless_none_stays_pending_wake_only_recorded(db_env, monkeypatch):
    """message_body=None (pre-F459 ids-only caller). Row stays pending; unlike
    the pre-#660 behaviour a wake_only emission is now recorded so the trace can
    prove a wake fired without a body."""
    with db_env() as db:
        _add_row(db, 302)
        db.commit()

    with _native_socket(monkeypatch, write_err=None, verify=True):
        decision = _dbs._attempt_native_ring(SEAT, 302, message_body=None)

    assert decision == "rang"
    assert _status(db_env, 302) == MessageStatus.PENDING.value
    assert _emissions(db_env, 302) == {Carrier.NATIVE.value: EmissionOutcome.WAKE_ONLY.value}
    with db_env() as db:
        assert hook_claim_ids(db, candidate_ids=[302]) == [302]
        db.commit()


def test_ac_a_ids_only_even_when_verify_unconfirmed(db_env, monkeypatch):
    """A busy seat (verify_wake False) with an ids-only body still leaves the row
    pending — write success is not consumption when nothing was carried."""
    with db_env() as db:
        _add_row(db, 303)
        db.commit()
    with _native_socket(monkeypatch, write_err=None, verify=False):
        decision = _dbs._attempt_native_ring(SEAT, 303, message_body="[watchdog] tick")
    assert decision == "wake_unverified"
    assert _status(db_env, 303) == MessageStatus.PENDING.value
    assert _emissions(db_env, 303) == {Carrier.NATIVE.value: EmissionOutcome.WAKE_ONLY.value}


# ── AC(b): body-carried ring → consumed as today ─────────────────────────────


def test_ac_b_body_carried_still_consumes(db_env, monkeypatch):
    """A genuine body (survives normalize_wake_body) is consumed exactly as F783
    does today — the fix must not regress the body-carried path."""
    with db_env() as db:
        _add_row(db, 310)
        db.commit()
    with _native_socket(monkeypatch, write_err=None, verify=True):
        decision = _dbs._attempt_native_ring(SEAT, 310, message_body="real callback text")
    assert decision == "rang"
    assert _status(db_env, 310) == MessageStatus.DELIVERED.value
    assert _emissions(db_env, 310) == {Carrier.NATIVE.value: EmissionOutcome.SUCCEEDED.value}
    assert _cursor(db_env) == 310
    with db_env() as db:
        assert hook_claim_ids(db, candidate_ids=[310]) == []
        db.commit()


def test_ac_b_body_carried_consumes_even_when_unverified(db_env, monkeypatch):
    """F783 invariant preserved: body written + verify False (busy seat) still
    consumes."""
    with db_env() as db:
        _add_row(db, 311)
        db.commit()
    with _native_socket(monkeypatch, write_err=None, verify=False):
        decision = _dbs._attempt_native_ring(SEAT, 311, message_body="busy-seat body")
    assert decision == "wake_unverified"
    assert _status(db_env, 311) == MessageStatus.DELIVERED.value
    assert _emissions(db_env, 311) == {Carrier.NATIVE.value: EmissionOutcome.SUCCEEDED.value}


# ── AC(c): the delivery trace records WHICH (consumed vs wake_only vs failed) ──


def test_ac_c_trace_records_wake_only_for_ids_only(db_env, monkeypatch):
    with db_env() as db:
        _add_row(db, 320)
        db.commit()
    with _native_socket(monkeypatch, write_err=None, verify=True):
        _dbs._attempt_native_ring(SEAT, 320, message_body="[CONDITION] busy")
    assert "f803.native_wake_only" in _trace_kinds(db_env, 320)


def test_ac_c_trace_records_socket_delivered_for_body(db_env, monkeypatch):
    """A body-carried ring does NOT record a wake_only trace (it consumed)."""
    with db_env() as db:
        _add_row(db, 321)
        db.commit()
    with _native_socket(monkeypatch, write_err=None, verify=True):
        _dbs._attempt_native_ring(SEAT, 321, message_body="real body")
    assert "f803.native_wake_only" not in _trace_kinds(db_env, 321)


def test_ac_c_write_failure_records_no_wake_only_for_ids_only(db_env, monkeypatch):
    """An ids-only ring that FAILS to write records NEITHER consumption NOR a
    wake_only emission (no wake fired, no body carried); the row stays pending
    and the hook owns it. A bodyless attempt is not a missed body delivery, so
    _f783_note_native_failure is also a no-op."""
    with db_env() as db:
        _add_row(db, 322)
        db.commit()
    with _native_socket(monkeypatch, write_err="socket_econnrefused"):
        decision = _dbs._attempt_native_ring(SEAT, 322, message_body="[CONDITION] busy")
    assert decision == "socket_econnrefused"
    assert _status(db_env, 322) == MessageStatus.PENDING.value
    # No emission at all — the write failed before the wake_only attach point,
    # and the body-carrying failure recorder is a no-op for an ids-only ping.
    assert _emissions(db_env, 322) == {}
    with db_env() as db:
        assert hook_claim_ids(db, candidate_ids=[322]) == [322]
        db.commit()


# ── SHOULD (r1 verdict): the f459.socket_delivered TRANSPORT marker is written ─
# ── by BOTH outer callers (ring_supervisor_doorbell + attempt_rung1) on a ──────
# ── successful ring, for BOTH body-carrying and ids-only rings, while their ────
# ── consumption outcomes differ. The AC(c) tests above drive _attempt_native_ ──
# ── ring directly, which BYPASSES both marker call sites; these drive the ──────
# ── outer callers so the marker itself is exercised. attempt_rung1 coverage ────
# ── lives in test_f547_rung1_repush_discipline.py (same seam, delivery_service).


def _socket_delivered_traces(sessions, row_id: int) -> int:
    with sessions() as db:
        return (
            db.query(InboxMessageTraceEventModel)
            .filter(
                InboxMessageTraceEventModel.message_id == row_id,
                InboxMessageTraceEventModel.kind == "f459.socket_delivered",
            )
            .count()
        )


def _seat_meta_and_flags(monkeypatch):
    """Satisfy ring_supervisor_doorbell's pre-ring guards so a successful native
    ring reaches the _mark_socket_delivered call site."""
    monkeypatch.setattr(_dbs, "_queue_owns_delivery", lambda *_a, **_k: False)
    monkeypatch.setattr(_dbs, "_is_row_still_pending", lambda row_id: True)
    # F747 (#747): this stub returns the CALL SITE's default, which stood in for
    # the shipped default only while ring_supervisor_doorbell passed
    # ``default=True``. supervisor.doorbell now ships OFF and the call site no
    # longer restates a default, so the stub answered None and the ring was
    # skipped before it could reach _mark_socket_delivered. This test is about
    # the ring, so it says which posture it needs.
    monkeypatch.setattr(
        _dbs.ConfigService,
        "get",
        staticmethod(lambda key, default=None: True if key == "supervisor.doorbell" else default),
        raising=False,
    )


def test_should_ring_supervisor_doorbell_marks_socket_delivered_for_body(db_env, monkeypatch):
    """Outer caller ring_supervisor_doorbell, body-carrying ring: writes the
    f459.socket_delivered TRANSPORT marker AND consumes (SUCCEEDED, row
    delivered)."""
    with db_env() as db:
        _add_row(db, 350)
        db.commit()
    _seat_meta_and_flags(monkeypatch)
    with _native_socket(monkeypatch, write_err=None, verify=True):
        decision = _dbs.ring_supervisor_doorbell(
            SEAT, 350, written_count=1, message_body="real callback text"
        )
    assert decision == "rang"
    # TRANSPORT marker present (this is the un-gated call site).
    assert _socket_delivered_traces(db_env, 350) == 1
    # CONSUMPTION: body carried → SUCCEEDED, row delivered.
    assert _status(db_env, 350) == MessageStatus.DELIVERED.value
    assert _emissions(db_env, 350) == {Carrier.NATIVE.value: EmissionOutcome.SUCCEEDED.value}


def test_should_ring_supervisor_doorbell_marks_socket_delivered_for_ids_only(db_env, monkeypatch):
    """Outer caller ring_supervisor_doorbell, ids-only ring: STILL writes the
    f459.socket_delivered TRANSPORT marker (the socket write succeeded) while
    CONSUMPTION differs — WAKE_ONLY, row stays pending, hook wins. This is the
    exact behaviour B2 restored: the marker is transport truth, not body-gated."""
    with db_env() as db:
        _add_row(db, 351)
        db.commit()
    _seat_meta_and_flags(monkeypatch)
    with _native_socket(monkeypatch, write_err=None, verify=True):
        decision = _dbs.ring_supervisor_doorbell(
            SEAT, 351, written_count=1, message_body="[CONDITION] BUSY subtype=capped"
        )
    assert decision == "rang"
    # TRANSPORT marker present EVEN for the ids-only ring (B2: not body-gated).
    assert _socket_delivered_traces(db_env, 351) == 1
    # CONSUMPTION: no body carried → WAKE_ONLY, row pending, hook wins.
    assert _status(db_env, 351) == MessageStatus.PENDING.value
    assert _emissions(db_env, 351) == {Carrier.NATIVE.value: EmissionOutcome.WAKE_ONLY.value}
    with db_env() as db:
        assert hook_claim_ids(db, candidate_ids=[351]) == [351]
        db.commit()


# ── record_native_wake_only unit + ledger semantics ──────────────────────────


def test_record_native_wake_only_keeps_pending_and_is_idempotent(db_env):
    with db_env() as db:
        _add_row(db, 330)
        db.commit()
    record_native_wake_only(330)
    record_native_wake_only(330)  # second call: claim already held, still fine
    assert _status(db_env, 330) == MessageStatus.PENDING.value
    assert _cursor(db_env) == 0
    assert _emissions(db_env, 330) == {Carrier.NATIVE.value: EmissionOutcome.WAKE_ONLY.value}
    with db_env() as db:
        n = (
            db.query(DeliveryEmissionModel)
            .filter(
                DeliveryEmissionModel.message_id == 330,
                DeliveryEmissionModel.carrier == Carrier.NATIVE.value,
            )
            .count()
        )
    assert n == 1  # idempotent claim


def test_wake_only_does_not_mute_hook_but_succeeded_does(db_env):
    """hook_claim_ids: a wake_only NATIVE emission lets the hook WIN; a succeeded
    one mutes it."""
    with db_env() as db:
        _add_row(db, 340)  # wake_only
        _add_row(db, 341)  # succeeded (simulate native consume claim)
        db.commit()
    record_native_wake_only(340)
    from cli_agent_orchestrator.services.mailbox_service import consume_on_native_delivery

    consume_on_native_delivery(341)
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[340, 341])
        db.commit()
    assert won == [340]


def test_wake_only_is_not_terminal_for_exhaustion():
    """A wake_only carrier is NOT exhausted — it is still owed a real
    body-carrying attempt (so an ids-only wake can never strand a row as
    undeliverable)."""
    assert EmissionOutcome.WAKE_ONLY not in _TERMINAL_EMISSION_OUTCOMES
    view = EmissionView(Carrier.NATIVE, EmissionOutcome.WAKE_ONLY, retryable=False)
    assert is_carrier_exhausted(view) is False
