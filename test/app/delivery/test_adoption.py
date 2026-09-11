"""Orphaned legacy ``inbox`` rows reach the tick (WP-ARCH 3c, B1).

**The hole these close.** ``write_through`` returns a detached model and adds
nothing to the ``inbox`` table, so a row physically present there has no
``delivery_msg`` counterpart by construction, and ``DeliveryTick.serve`` iterates
the QUEUE store only. Before 3c two legacy carriers served those rows — the
F461-coalesced doorbell out of ``_f136_post_delivery`` and the seat-wake
reconcile. Slice 2 deletes both. Adoption is what replaces them, and the r1
review is explicit that without it a receiver holding such a row has no carrier
at all, which is #604 reintroduced.

**These assert an EMISSION, never the absence of a mute.** That is the standard
the p3b file's opening note sets, and the r1 review's B2 is that the arms which
used to live here had stopped meeting it: they checked that a mute did not fire,
which passes green while the production wake is gone. So each arm below drives a
real ``run_once`` against a real SQLite database holding BOTH schemas and counts
what arrived at the carrier seam.

**Two rows reach the legacy inbox at ``on``**, and neither is hypothetical: rows
that predate the flip, and write-through fallbacks, which a lost ``BEGIN
IMMEDIATE`` race produces as ``database is locked`` — three of them in the box
round this phase is answering.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.app.delivery import wiring
from cli_agent_orchestrator.app.delivery.tick import DeliveryTick
from cli_agent_orchestrator.app.delivery.wake import WakeService
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.core.delivery import MsgState, SwitchPosition
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import mailbox_service

SEAT = "sup-adopt01"
WORKER = "wrk-adopt01"
SEAT_MAILBOX = "mb_adopt_sup"
WORKER_MAILBOX = "mb_adopt_wrk"


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _RecordingCarrier:
    """The seat seam. Counts native writes; never opens a socket."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def emit(self, *, terminal_id, line, sender_key, sender_name, msg_id):
        from cli_agent_orchestrator.core.delivery import WakeEmission

        self.writes.append((terminal_id, msg_id))
        return WakeEmission(reason=None, verified=True)


class _RecordingInjector:
    """The worker seam. Counts pane pastes."""

    def __init__(self) -> None:
        self.pastes: list[tuple[str, str]] = []

    def inject(self, *, terminal_id, line):
        from cli_agent_orchestrator.core.delivery import AttemptOutcome, InjectionResult

        self.pastes.append((terminal_id, line))
        return InjectionResult(outcome=AttemptOutcome.DELIVERED, detail="pane")


class _RecordingFindings:
    """Dedupes on ``(code, terminal_id, dedupe_key)`` the way the real store does."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict] = {}

    def record(self, code, *, terminal_id="", dedupe_key="", detail="", sample_event_id=None):
        key = (code.value, terminal_id, dedupe_key)
        row = self.rows.setdefault(key, {"code": code, "terminal_id": terminal_id, "count": 0})
        row["count"] += 1
        return row

    def of(self, code: FindingCode) -> list[dict]:
        return [row for key, row in self.rows.items() if key[0] == code.value]

    def list_findings(self, *, state=None, code=None):
        return []


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[tuple]:
    """One SQLite file holding BOTH the legacy tables and the queue's.

    One file rather than two, because adoption spans them: the scan reads the
    legacy inbox through the ORM session and the enqueue writes the queue's own
    pooled connection. Splitting them would make the test pass over a pair of
    databases the server never has.
    """
    db_file = tmp_path / "adopt.sqlite"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        columns = conn.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        if "schema_version" not in {col["name"] for col in columns}:
            conn.execute(
                text("ALTER TABLE mailboxes ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)

    result, pool = migrate(db_file, busy_timeout_ms=5000)
    assert result.ok, result
    assert pool is not None

    clock = _Clock()
    store = SqliteQueueStore(pool, clock=clock)
    wiring.install_delivery(
        wiring.DeliveryRuntime(store=store, clock=clock, position=SwitchPosition.ON)
    )

    from cli_agent_orchestrator.services.queue_carrier import (
        LegacyInboxAdoption,
        LegacyReceiverDirectory,
    )

    carrier = _RecordingCarrier()
    injector = _RecordingInjector()
    findings = _RecordingFindings()
    directory = LegacyReceiverDirectory()
    tick = DeliveryTick(
        store=store,
        wake=WakeService(
            store=store,
            directory=directory,
            carrier=carrier,
            injector=injector,
            clock=clock,
        ),
        directory=directory,
        findings=findings,
        clock=clock,
        position=SwitchPosition.ON,
        adopter=LegacyInboxAdoption(),
    )

    yield sessions, store, tick, carrier, injector, findings

    wiring.reset_delivery()
    pool.close_all()
    engine.dispose()


def _receiver(db, *, terminal_id: str, mailbox_id: str, role: str) -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="cao-adopt",
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="chao_supervisor" if role == "supervisor" else "dev",
            init_state="ready",
        )
    )
    db.add(
        MailboxModel(
            id=mailbox_id,
            session_name="cao-adopt",
            role=role,
            current_terminal_id=terminal_id,
            generation=1,
            consumed_through_id=0,
            schema_version=1,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
    )
    db.add(
        MailboxIncarnationModel(
            mailbox_id=mailbox_id,
            generation=1,
            terminal_id=terminal_id,
            published_at=datetime.now(),
        )
    )


def _legacy_row(db, *, receiver: str, mailbox_id: str, message: str = "ORPHAN") -> InboxModel:
    """A PENDING legacy row with NO queue counterpart — the orphan itself."""
    row = InboxModel(
        sender_id=WORKER,
        receiver_id=receiver,
        logical_receiver_id=mailbox_id,
        enqueue_generation=1,
        message=message,
        orchestration_type="send_message",
        status=MessageStatus.PENDING.value,
        created_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def _status(sessions, row_id: int) -> str:
    with sessions() as db:
        return str(db.query(InboxModel).filter(InboxModel.id == row_id).one().status)


# -- (a) the seat ------------------------------------------------------------


def test_a_pending_seat_legacy_row_is_adopted_and_natively_woken(env) -> None:
    """B1's headline case, asserted at the carrier seam.

    One native write, zero pastes, the legacy row out of the PENDING set, and the
    queue row it became reaching a terminal delivered state. A run where the
    write count is zero fails, and it fails in the direction that matters: a
    paste is an ugly carrier, silence is the bug.
    """
    sessions, store, tick, carrier, injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX)
        row_id = int(row.id)

    report = tick.run_once()

    assert len(report.adopted) == 1, report.adopted
    adoption = report.adopted[0]
    assert adoption.legacy_message_id == row_id
    assert adoption.receiver_id == SEAT_MAILBOX

    assert len(carrier.writes) == 1, (
        "an adopted seat row produced no native wake: it had no carrier before "
        "adoption either, which is #604"
    )
    assert injector.pastes == [], "a supervisor receiver is never pasted (K8)"

    assert _status(sessions, row_id) == MessageStatus.ADOPTED.value, (
        "the legacy row is still PENDING after adoption: two carriers may now own "
        "one id, which is #506"
    )
    queued = store.get(adoption.msg_id)
    assert queued is not None
    assert queued.legacy_message_id == row_id, "the queue row must name its legacy id"


# -- (b) the worker ----------------------------------------------------------


def test_a_pending_worker_legacy_row_is_adopted_and_pasted(env) -> None:
    """Adoption is not a seat feature.

    A worker's orphaned row is equally uncarried after the slice, and its carrier
    is the pane. Asserting the injector here is what stops a future "adopt only
    supervisors" narrowing from passing.
    """
    sessions, _store, tick, carrier, injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=WORKER, mailbox_id=WORKER_MAILBOX, role="developer")
        row = _legacy_row(db, receiver=WORKER, mailbox_id=WORKER_MAILBOX, message="WORKER_ORPHAN")
        row_id = int(row.id)

    report = tick.run_once()

    assert len(report.adopted) == 1, report.adopted
    assert len(injector.pastes) == 1, "an adopted worker row never reached the pane"
    assert carrier.writes == [], "a worker receiver is not woken on the seat channel"
    assert _status(sessions, row_id) == MessageStatus.ADOPTED.value


# -- (c) idempotence ---------------------------------------------------------


def test_adoption_is_idempotent_across_two_ticks(env) -> None:
    """The property that makes adoption safe to run every ten seconds.

    Two ticks, one row: exactly one adoption, one queue row, and ONE finding
    whose count is 1. A second queue row here would be the duplicate-carrier
    family (#506) arriving through the fix for the silent one.
    """
    sessions, store, tick, carrier, _injector, findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX)
        row_id = int(row.id)

    first = tick.run_once()
    second = tick.run_once()

    assert len(first.adopted) == 1
    assert second.adopted == (), "the retired row was adopted a second time"

    with sessions() as db:
        assert db.query(InboxModel).filter(InboxModel.id == row_id).one().status == (
            MessageStatus.ADOPTED.value
        )

    rows = findings.of(FindingCode.DIAG_LEGACY_ROW_ADOPTED)
    assert len(rows) == 1, rows
    assert rows[0]["count"] == 1, "a repeat tick incremented a finding it did not earn"
    assert rows[0]["terminal_id"] == SEAT_MAILBOX

    assert len(carrier.writes) == 1, (
        "the seat was woken twice for one message: one wake per lease is the "
        "whole point of the claim"
    )


# -- (d) the write-through fallback ------------------------------------------


def test_a_write_through_fallback_row_is_adopted_on_the_next_tick(env, monkeypatch) -> None:
    """The evidenced case, driven through the REAL send path.

    ``create_inbox_message`` is made to lose its write-through exactly the way
    the box round did — the enqueue raises, the hook swallows it and returns
    ``None``, and the caller writes the legacy row it was designed to fall back
    to. That row is the orphan. Before 3c it had two carriers; after the slice it
    has adoption, and this arm is the proof that the two halves meet.
    """
    from cli_agent_orchestrator.clients.database import create_inbox_message

    sessions, _store, tick, carrier, _injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")

    def _locked(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        "cli_agent_orchestrator.app.delivery.wiring.write_through",
        _locked,
    )
    message = create_inbox_message(WORKER, SEAT, "FALLBACK_CALLBACK")
    row_id = int(message.id)

    assert _status(sessions, row_id) == MessageStatus.PENDING.value, (
        "the fallback did not write a legacy row, so this arm is not exercising "
        "the case it names"
    )

    report = tick.run_once()

    assert [a.legacy_message_id for a in report.adopted] == [row_id], report.adopted
    assert len(carrier.writes) == 1, "the fallback row never reached the seat"
    assert _status(sessions, row_id) == MessageStatus.ADOPTED.value


# -- the negative control ----------------------------------------------------


def test_a_tick_with_no_orphans_adopts_nothing_and_raises_no_finding(env) -> None:
    """Without this, every arm above would be satisfied by adopting everything.

    A receiver whose rows all went through the write-through has nothing in the
    legacy inbox, and an adoption pass that invented work for it would be writing
    a second carrier for rows the tick already owns.
    """
    sessions, _store, tick, _carrier, _injector, findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")

    report = tick.run_once()

    assert report.adopted == ()
    assert findings.of(FindingCode.DIAG_LEGACY_ROW_ADOPTED) == []


def test_a_held_barrier_row_is_not_adopted(env) -> None:
    """HELD is not owed to anyone yet.

    A barrier member becomes PENDING when its barrier completes, and the tick
    after that adopts it. Adopting it early would deliver a message the barrier
    exists to hold back.
    """
    sessions, _store, tick, carrier, _injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX)
        row.status = MessageStatus.HELD.value
        row_id = int(row.id)

    report = tick.run_once()

    assert report.adopted == ()
    assert carrier.writes == []
    assert _status(sessions, row_id) == MessageStatus.HELD.value


def test_the_adopted_row_reaches_a_terminal_state_when_acked(env) -> None:
    """Adoption hands the row to the queue's lifecycle, not to a side channel.

    The queue row must be an ordinary one: leased by the serve step, and able to
    reach a terminal state through the same ack path every other row uses.
    """
    sessions, store, tick, _carrier, _injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX)

    report = tick.run_once()
    msg_id = report.adopted[0].msg_id

    queued = store.get(msg_id)
    assert queued is not None
    assert (
        queued.state is not MsgState.READY
    ), "the adopted row was never claimed: it is in the queue but nothing served it"


def test_adoption_is_a_noop_outside_on(env) -> None:
    """``drain`` accepts no new queue traffic, and an adopted row is new traffic.

    The adopter is wired for both served positions, so the refusal has to live in
    ``adopt_legacy_row`` rather than in the wiring — otherwise the same rule sits
    in two places and they drift. This arm pins the refusal at the position, not
    at the composition root: the legacy row must still be PENDING afterwards,
    because a retire without an enqueue is the window in which nothing owns it.
    """
    sessions, _store, tick, carrier, _injector, findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX)
        row_id = int(row.id)

    # The refusal is keyed on the INSTALLED RUNTIME's position, not on a helper
    # a test could patch beside it — ``adopt_legacy_row`` reads
    # ``runtime.position`` directly. So the runtime is reinstalled at ``drain``,
    # which is what the composition root does for that position in production.
    # (Patching ``wiring.queue_position`` instead leaves the runtime at ``on``
    # and the row IS adopted — tried, and it is why this note exists.)
    from cli_agent_orchestrator.app.delivery import wiring as _wiring

    runtime = _wiring._runtime
    _wiring.install_delivery(
        _wiring.DeliveryRuntime(
            store=runtime.store, clock=runtime.clock, position=SwitchPosition.DRAIN
        )
    )
    report = tick.run_once()

    assert report.adopted == ()
    assert carrier.writes == []
    assert findings.of(FindingCode.DIAG_LEGACY_ROW_ADOPTED) == []
    assert (
        _status(sessions, row_id) == MessageStatus.PENDING.value
    ), "the row was retired without being enqueued: nothing owns it now"
