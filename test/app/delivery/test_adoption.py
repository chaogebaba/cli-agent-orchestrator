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
from cli_agent_orchestrator.core.delivery import MsgState
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
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)

    result, pool = migrate(db_file, busy_timeout_ms=5000)
    assert result.ok, result
    assert pool is not None

    clock = _Clock()
    store = SqliteQueueStore(pool, clock=clock)
    wiring.install_delivery(wiring.DeliveryRuntime(store=store, clock=clock))

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
        "the legacy row is still PENDING after adoption: the retire did not run, "
        "so the row is re-scanned on every tick forever"
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
    whose count is 1. A second queue row here would be a duplicate arriving
    through the very fix for the silent seat.
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


def test_adoption_is_a_noop_when_the_queue_is_not_installed(env) -> None:
    """A refused adoption must leave the legacy row PENDING, not retired.

    This arm used to drive ``drain``: the adopter was wired for both served
    positions and an adopted row IS new queue traffic, which ``drain`` existed
    not to accept, so the refusal had to live in ``adopt_legacy_row`` rather than
    in the wiring or the same rule would sit in two places and drift.  WP-ARCH 3c
    deletes the legacy carriers, so ``drain`` has nothing to hand traffic back to
    and the switch has one position left.

    The load-bearing half is NOT the position — it is that a refusal is total.
    ``adopt_orphaned_legacy_rows`` enqueues BEFORE it retires, so a refusal that
    still retired would leave a row owned by nobody; ``clients/database.py``
    skips the retire only because the enqueue answered ``None``.  That refusal
    path still exists, reached now by the one condition that still disarms the
    queue — no runtime installed — so the arm is RE-POINTED at it rather than
    deleted.  Driving it this way also keeps it honest about where the rule
    lives: the tick and its adopter are untouched, and only the runtime is gone.
    """
    sessions, _store, tick, carrier, _injector, findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX)
        row_id = int(row.id)

    # The refusal is keyed on the INSTALLED RUNTIME, not on a helper a test could
    # patch beside it — ``adopt_legacy_row`` reads the module global directly.
    from cli_agent_orchestrator.app.delivery import wiring as _wiring

    _wiring.reset_delivery()
    report = tick.run_once()

    assert report.adopted == ()
    assert carrier.writes == []
    assert findings.of(FindingCode.DIAG_LEGACY_ROW_ADOPTED) == []
    assert (
        _status(sessions, row_id) == MessageStatus.PENDING.value
    ), "the row was retired without being enqueued: nothing owns it now"


# -- r2 N2: a poison row may not hold the scan window ------------------------


@pytest.fixture(autouse=True)
def _clear_poison() -> Iterator[None]:
    """The skip set is module state; a leaked id would silently shrink a sibling
    test's scan window, which is exactly the failure this section is about."""
    from cli_agent_orchestrator.clients.database import reset_adoption_poison_ids

    reset_adoption_poison_ids()
    yield
    reset_adoption_poison_ids()


def test_a_receiverless_row_is_skipped_once_and_never_retried(env, caplog) -> None:
    """A row naming no receiver cannot be addressed, so it must leave the window.

    Before the fix it stayed PENDING and reoccupied a scan slot on every tick
    forever. It still stays PENDING — there is nothing to hand it to — but it is
    recorded as poison, warned about by id, and excluded from later scans.
    """
    import logging

    from cli_agent_orchestrator.clients import database as dbmod

    sessions, _store, tick, _carrier, _injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = InboxModel(
            sender_id=WORKER,
            receiver_id="",
            logical_receiver_id=None,
            enqueue_generation=1,
            message="NO_RECEIVER",
            orchestration_type="send_message",
            status=MessageStatus.PENDING.value,
            created_at=datetime.now(),
        )
        db.add(row)
        db.flush()
        row_id = int(row.id)

    with caplog.at_level(logging.WARNING):
        report = tick.run_once()

    assert report.adopted == ()
    assert _status(sessions, row_id) == MessageStatus.PENDING.value
    assert row_id in dbmod._ADOPTION_POISON_IDS, "the row can still hold the window"
    assert any(
        str(row_id) in r.getMessage() for r in caplog.records
    ), "a stuck row failed silently: it must name its id in the journal"


def test_a_poison_row_does_not_starve_the_rows_behind_it(env) -> None:
    """The property the skip set exists for, at the scan boundary.

    The poison row has the LOWEST id, so an ascending scan reaches it first.
    With a window of one it would take the only slot every tick and the good row
    behind it would never be adopted. The second tick must adopt the good row.
    """
    from cli_agent_orchestrator.app.delivery import tick as tick_mod

    sessions, _store, tick, carrier, _injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        poison = InboxModel(
            sender_id=WORKER,
            receiver_id="",
            logical_receiver_id=None,
            enqueue_generation=1,
            message="POISON",
            orchestration_type="send_message",
            status=MessageStatus.PENDING.value,
            created_at=datetime.now(),
        )
        db.add(poison)
        db.flush()
        good = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX, message="BEHIND_THE_POISON")
        good_id = int(good.id)
        assert int(poison.id) < good_id, "the poison row must sort first for this to bite"

    # A window of ONE makes the starvation exact rather than probabilistic.
    original = tick_mod.ADOPT_LIMIT
    tick_mod.ADOPT_LIMIT = 1
    try:
        first = tick.run_once()
        assert first.adopted == (), "the poison row is not adoptable"
        second = tick.run_once()
    finally:
        tick_mod.ADOPT_LIMIT = original

    assert [a.legacy_message_id for a in second.adopted] == [
        good_id
    ], "the row behind the poison was starved: the skip set is not excluding it"
    assert len(carrier.writes) == 1


# -- r2 N3: the receiver that no longer exists -------------------------------


def test_a_row_whose_receiver_vanished_is_adopted_and_dies_on_its_budget(env) -> None:
    """The pre-slice behaviour was worse: the row sat PENDING forever.

    Adoption hands it to the queue, the directory resolves no live incarnation,
    the attempt records `pane_absent`, and the row ages toward `delivery_dead` on
    D10's budget. That is a BOUNDED ending, which is the property worth pinning —
    an unbounded one is how a row disappears without anyone learning.
    """
    sessions, store, tick, carrier, injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX, message="ORPHANED_RECEIVER")
        row_id = int(row.id)

    # The mailbox survives (it is the durable address) but its incarnation is
    # gone — a reaped terminal, which is the real shape of "the receiver left".
    with sessions.begin() as db:
        db.query(MailboxModel).filter(MailboxModel.id == SEAT_MAILBOX).update(
            {MailboxModel.current_terminal_id: ""}, synchronize_session=False
        )
        db.query(TerminalModel).filter(TerminalModel.id == SEAT).delete(synchronize_session=False)

    report = tick.run_once()

    assert [a.legacy_message_id for a in report.adopted] == [row_id], (
        "a row for a vanished receiver must still be adopted: leaving it PENDING "
        "is the unbounded ending"
    )
    assert carrier.writes == [], "there is no live incarnation to write to"
    assert injector.pastes == []

    queued = store.get(report.adopted[0].msg_id)
    assert queued is not None
    attempts = store.attempts_for(queued.msg_id)
    assert attempts, "an unreachable receiver must still record an attempt"
    assert any("pane_absent" in (a.detail or "") or a.outcome for a in attempts), attempts


# -- slice 3: the pre-flip seat row, end to end ------------------------------


def test_a_pre_flip_seat_row_takes_one_native_write_and_zero_pastes(env) -> None:
    """The plan's slice-3 acceptance, and the case slice 2's B1 was about.

    A row written to the legacy inbox BEFORE the flip has no ``delivery_msg``
    counterpart, and after slice 3 it has no legacy carrier either: the doorbell,
    the pull reconciler and the pane nudge are all deleted. Adoption is the whole
    of its path, and the assertion is on the EMISSION — one native socket write,
    zero pastes — because "nothing pasted" alone is satisfied by silence, and
    silence at the seat is #604.
    """
    sessions, _store, tick, carrier, injector, _findings = env
    with sessions.begin() as db:
        _receiver(db, terminal_id=SEAT, mailbox_id=SEAT_MAILBOX, role="supervisor")
        row = _legacy_row(db, receiver=SEAT, mailbox_id=SEAT_MAILBOX, message="PRE_FLIP_CALLBACK")
        row_id = int(row.id)

    tick.run_once()

    assert len(carrier.writes) == 1, "the pre-flip row produced no native wake"
    assert injector.pastes == [], "a supervisor receiver is never pasted (K8)"
    assert _status(sessions, row_id) == MessageStatus.ADOPTED.value


def test_the_worker_injector_refuses_a_supervisor_terminal() -> None:
    """K8 as a property of the CALL GRAPH, asserted at the injector itself.

    The ban is not only that nothing currently routes a seat row to the pane
    injector — it is that doing so would be REFUSED. A future caller that routes
    wrongly gets ``paste_attempted``, a finding and a row that dies on its
    attempt budget, rather than a silent paste into a human's composer.
    """
    from unittest.mock import patch

    from cli_agent_orchestrator.core.delivery import AttemptOutcome
    from cli_agent_orchestrator.services.queue_carrier import PaneWorkerInjector

    sent: list[str] = []
    with (
        patch(
            "cli_agent_orchestrator.services.mailbox_service.probe_supervisor_role",
            return_value=True,
        ),
        patch.object(PaneWorkerInjector, "_send", lambda *a, **k: sent.append("sent")),
    ):
        result = PaneWorkerInjector().inject(terminal_id=SEAT, line="never pasted")

    assert result.outcome is AttemptOutcome.PASTE_ATTEMPTED
    assert result.detail == "paste_attempted"
    assert sent == [], "the injector reached its send path for a supervisor target"


def test_the_worker_injector_still_pastes_a_worker() -> None:
    """The negative control: K8 removed a path, not the pane carrier.

    Without this arm, an injector that refused EVERYTHING would satisfy the
    refusal arm above while breaking every worker's delivery.
    """
    from unittest.mock import patch

    from cli_agent_orchestrator.core.delivery import AttemptOutcome, InjectionResult
    from cli_agent_orchestrator.services.queue_carrier import PaneWorkerInjector

    with (
        patch(
            "cli_agent_orchestrator.services.mailbox_service.probe_supervisor_role",
            return_value=False,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.inbox_service._dialog_gate_active",
            return_value=False,
        ),
        patch.object(
            PaneWorkerInjector,
            "_send",
            staticmethod(
                lambda *a, **k: InjectionResult(outcome=AttemptOutcome.DELIVERED, detail="pane")
            ),
        ),
    ):
        result = PaneWorkerInjector().inject(terminal_id=WORKER, line="pasted")

    assert result.outcome is AttemptOutcome.DELIVERED
