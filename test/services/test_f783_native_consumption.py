"""F783 #640: a SUCCESSFUL native seat-wake IS consumption — the doorbell,
re-push nag, coalesced digest and drain-hook body all stay silent for a
natively-delivered id; a FAILED native send keeps today's fallback.

User decision (2026-09-06, "Native only; others silent"): a native
agent-message that reaches the seat's TUI is guaranteed-rendered, so it counts
as consumed the moment the socket write succeeds — even under the spinner. This
suite pins the six acceptance criteria in the lane spec.

Fixtures use an in-memory SQLite bound through both the ``database`` and
``mailbox_service`` module symbols (the established pattern in this tree).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryEmissionModel,
    DeliveryLedgerModel,
    DeliveryObligationModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    create_delivery_ledger_row,
    hook_claim_ids,
)
from cli_agent_orchestrator.clients.delivery_ledger import Carrier, EmissionOutcome
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import (
    consume_on_native_delivery,
    list_messages,
    record_native_delivery_failure,
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


def _add_row(
    db: Any,
    row_id: int,
    *,
    status: str = MessageStatus.PENDING.value,
    with_ledger: bool = True,
) -> InboxModel:
    """Insert one inbox row scoped to the supervisor mailbox, plus (by default)
    its F642 ledger row with the full applicable-carrier set — the same shape
    ``_insert_routed_inbox_row`` produces in production. The F413 ORM listener
    auto-creates the OPEN delivery_obligation for a mailbox-scoped row, so this
    helper does not insert one itself."""
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
    if with_ledger:
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
        return db.query(InboxModel.status).filter(InboxModel.id == row_id).scalar()


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


# ── AC1: native success → row consumed + cursor advanced + no longer pending ──


def test_ac1_native_success_consumes_row_and_advances_cursor(db_env):
    with db_env() as db:
        _add_row(db, 10)
        db.commit()

    out = consume_on_native_delivery(10)

    assert out["consumed"] is True
    assert out["status_flipped"] is True
    assert out["cursor_advanced"] is True
    assert out["emission_won"] is True
    assert _status(db_env, 10) == MessageStatus.DELIVERED.value
    assert _cursor(db_env) == 10
    # A NATIVE emission recorded SUCCEEDED — the audit record + the hook mute.
    assert _emissions(db_env, 10) == {Carrier.NATIVE.value: EmissionOutcome.SUCCEEDED.value}
    # AC1: `messages list --status pending` no longer lists X (status filter).
    listed = [int(i["id"]) for i in list_messages(SEAT, status=MessageStatus.PENDING)["items"]]
    assert 10 not in listed


def test_ac1_failure_reason_marks_native_consumed(db_env):
    with db_env() as db:
        _add_row(db, 11)
        db.commit()
    consume_on_native_delivery(11)
    with db_env() as db:
        row = db.query(InboxModel).filter(InboxModel.id == 11).one()
        assert row.failure_reason == "native_consumed"


def test_ac1_settles_open_obligation(db_env):
    with db_env() as db:
        _add_row(db, 12)
        db.commit()
    # The F413 listener created an OPEN obligation for this mailbox-scoped row.
    with db_env() as db:
        pre = (
            db.query(DeliveryObligationModel)
            .filter(DeliveryObligationModel.inbox_row_id == 12)
            .one()
        )
        assert pre.state == "OPEN"
    consume_on_native_delivery(12)
    with db_env() as db:
        obl = (
            db.query(DeliveryObligationModel)
            .filter(DeliveryObligationModel.inbox_row_id == 12)
            .one()
        )
        assert obl.state == "ACKED"
        assert obl.terminal_reason == "native_consumed"


# ── AC2: native success → hook `--claim hook` returns nothing for X ───────────


def test_ac2_hook_claim_returns_nothing_for_natively_consumed_id(db_env):
    with db_env() as db:
        _add_row(db, 20)
        db.commit()
    consume_on_native_delivery(20)
    # The hook's read-as-claim, server-side, must exclude the consumed id both
    # because its status is no longer pending AND because a NATIVE emission
    # already holds the id (hook_claim_ids excludes non-hook-claimed ids).
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[20])
        db.commit()
    assert won == []


def test_ac2_hook_claim_wins_unconsumed_sibling(db_env):
    """Control: an id that was NOT natively consumed is still won by the hook."""
    with db_env() as db:
        _add_row(db, 21)
        _add_row(db, 22)
        db.commit()
    consume_on_native_delivery(21)
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[21, 22])
        db.commit()
    assert won == [22]


# ── AC3: native FAILURE → X stays pending, fallback intact + typed reason ─────


def test_ac3_failure_keeps_row_pending_with_typed_reason(db_env):
    with db_env() as db:
        _add_row(db, 30)
        db.commit()

    record_native_delivery_failure(30, "socket_unpublished")

    # Row is untouched — still pending, cursor unmoved — so the doorbell,
    # re-push and hook run exactly as at base.
    assert _status(db_env, 30) == MessageStatus.PENDING.value
    assert _cursor(db_env) == 0
    # A typed, auditable failure marker rides on the ledger + a trace row.
    assert _emissions(db_env, 30) == {Carrier.NATIVE.value: EmissionOutcome.FAILED.value}
    with db_env() as db:
        trace = (
            db.query(InboxMessageTraceEventModel)
            .filter(
                InboxMessageTraceEventModel.message_id == 30,
                InboxMessageTraceEventModel.kind == "f783.native_delivery_failed",
            )
            .one()
        )
        assert trace.reason == "socket_unpublished"
    # AC3: the hook still WINS the id after a failed native send (fallback owns it).
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[30])
        db.commit()
    assert won == [30]


def test_ac3_failed_row_still_lists_pending(db_env):
    with db_env() as db:
        _add_row(db, 31)
        db.commit()
    record_native_delivery_failure(31, "wake_unverified")
    listed = [int(i["id"]) for i in list_messages(SEAT, status=MessageStatus.PENDING)["items"]]
    assert 31 in listed


# ── AC4: cursor safety — older pending id must not be jumped ──────────────────


def test_ac4_cursor_does_not_jump_over_older_pending(db_env):
    """X-1 pending & X natively delivered → cursor stays at 0 (does not jump past
    X-1); X is consumed individually by its status flip."""
    with db_env() as db:
        _add_row(db, 40)  # older, stays pending
        _add_row(db, 41)  # natively delivered
        db.commit()

    out = consume_on_native_delivery(41)

    assert out["status_flipped"] is True
    assert out["cursor_advanced"] is False  # NOT advanced — 40 is still pending
    assert _cursor(db_env) == 0
    assert _status(db_env, 41) == MessageStatus.DELIVERED.value
    assert _status(db_env, 40) == MessageStatus.PENDING.value
    # 41 is consumed individually: not pending, hook cannot win it.
    with db_env() as db:
        won = hook_claim_ids(db, candidate_ids=[40, 41])
        db.commit()
    assert won == [40]  # only the still-pending older id


def test_ac4_cursor_advances_past_both_when_older_consumed(db_env):
    """X-1 later consumed → cursor advances past both (contiguity restored)."""
    with db_env() as db:
        _add_row(db, 50)
        _add_row(db, 51)
        db.commit()

    consume_on_native_delivery(51)  # cursor pinned at 0 (50 pending)
    assert _cursor(db_env) == 0

    consume_on_native_delivery(50)  # now 50 & 51 both consumed → sweep past both
    assert _cursor(db_env) == 51


def test_ac4_advance_is_contiguous_from_prior_cursor(db_env):
    """With the cursor already at 60, consuming 62 while 61 is pending pins the
    cursor at 60 (never jumps to 62 over the pending 61)."""
    with db_env() as db:
        db.query(MailboxModel).filter(MailboxModel.id == MB).update(
            {MailboxModel.consumed_through_id: 60}
        )
        _add_row(db, 61)
        _add_row(db, 62)
        db.commit()

    consume_on_native_delivery(62)
    assert _cursor(db_env) == 60  # 61 still pending — no jump


# ── AC5: idempotence — a second consume is a no-op ────────────────────────────


def test_ac5_second_consume_is_noop(db_env):
    with db_env() as db:
        _add_row(db, 70)
        db.commit()

    first = consume_on_native_delivery(70)
    second = consume_on_native_delivery(70)

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["status_flipped"] is False  # already delivered
    assert second["emission_won"] is False  # NATIVE claim already held
    # Exactly ONE native emission row (idempotent claim).
    with db_env() as db:
        n = (
            db.query(DeliveryEmissionModel)
            .filter(
                DeliveryEmissionModel.message_id == 70,
                DeliveryEmissionModel.carrier == Carrier.NATIVE.value,
            )
            .count()
        )
    assert n == 1


def test_ac5_consume_never_clobbers_terminal_state(db_env):
    """A row already superseded is NOT flipped back to delivered by consumption
    (never clobber a terminal message state)."""
    with db_env() as db:
        _add_row(db, 71, status=MessageStatus.SUPERSEDED.value)
        db.commit()
    out = consume_on_native_delivery(71)
    assert out["status_flipped"] is False
    assert _status(db_env, 71) == MessageStatus.SUPERSEDED.value


def test_absent_row_is_safe(db_env):
    out = consume_on_native_delivery(9999)
    assert out["consumed"] is False
    assert out.get("reason") == "row_absent"


# ── AC2/AC6 via the REAL HTTP claim path (supervisor req 1 & 2) ───────────────
#
# These drive the same route the root drain hook hits —
# ``GET /messages?to=<seat>&status=pending&claim=hook`` — through a FastAPI
# TestClient, NOT a direct service call, so the endpoint's status filter and the
# real ``hook_claim_ids`` claim both run. A WRITE scope is granted via the
# dependency override (the ``--claim`` path requires write/admin).


@pytest.fixture
def http_client(db_env):
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.security import auth

    class _HostClient(TestClient):
        def request(self, method, url, **kwargs):
            headers = kwargs.get("headers") or {}
            headers.setdefault("Host", "localhost")
            kwargs["headers"] = headers
            return super().request(method, url, **kwargs)

    async def _scopes():
        return [auth.SCOPE_WRITE, auth.SCOPE_READ]

    app.dependency_overrides[auth.get_current_scopes] = _scopes
    try:
        yield _HostClient(app)
    finally:
        app.dependency_overrides.pop(auth.get_current_scopes, None)


def _claim_hook_via_http(client, seat: str) -> list[int]:
    resp = client.get(
        "/messages",
        params={"to": seat, "status": "pending", "claim": "hook"},
    )
    assert resp.status_code == 200, resp.text
    return [int(i["id"]) for i in resp.json()["items"]]


def test_ac2_http_claim_hook_empty_for_natively_consumed(http_client):
    """REAL CLI path: the hook's `--status pending --claim hook` GET returns an
    EMPTY claim for a natively-consumed id (server-side, both the status filter
    and hook_claim_ids exclude it)."""
    # seed + consume
    from cli_agent_orchestrator.clients.database import SessionLocal

    with SessionLocal() as db:
        _add_row(db, 80)
        db.commit()
    consume_on_native_delivery(80)

    won = _claim_hook_via_http(http_client, SEAT)
    assert won == []


def test_ac2_http_claim_hook_wins_unconsumed(http_client):
    from cli_agent_orchestrator.clients.database import SessionLocal

    with SessionLocal() as db:
        _add_row(db, 81)
        _add_row(db, 82)
        db.commit()
    consume_on_native_delivery(81)

    won = _claim_hook_via_http(http_client, SEAT)
    assert won == [82]


def test_ac2_audit_row_intact_after_native_consume(http_client):
    """req 2: the id is ABSENT from the plain `--status pending` listing, yet
    `list_messages(status=None)` still shows it DELIVERED with reason
    native_consumed — the durable inbox row stays as an audit record."""
    from cli_agent_orchestrator.clients.database import SessionLocal

    with SessionLocal() as db:
        _add_row(db, 83)
        db.commit()
    consume_on_native_delivery(83)

    # (a) absent from plain --status pending listing (no claim), via HTTP.
    resp = http_client.get("/messages", params={"to": SEAT, "status": "pending"})
    assert resp.status_code == 200, resp.text
    assert 83 not in [int(i["id"]) for i in resp.json()["items"]]

    # (b) audit row intact: an unfiltered listing shows it delivered + reason.
    full = list_messages(SEAT, status=None)
    match = [i for i in full["items"] if int(i["id"]) == 83]
    assert len(match) == 1
    assert match[0]["status"] == MessageStatus.DELIVERED.value
    assert match[0]["failure_reason"] == "native_consumed"


def test_ac3_http_failed_native_still_claimable_by_hook(http_client):
    """req 4 at the HTTP arm: after a FAILED native send the id is STILL pending
    and the hook WINS it (fallback owns it) — today's behaviour, unchanged."""
    from cli_agent_orchestrator.clients.database import SessionLocal

    with SessionLocal() as db:
        _add_row(db, 84)
        db.commit()
    record_native_delivery_failure(84, "socket_unpublished")

    won = _claim_hook_via_http(http_client, SEAT)
    assert won == [84]
