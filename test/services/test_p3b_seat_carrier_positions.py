"""The seat's carrier, per queue position (WP-ARCH 3b, §A1.5, AC-3b case 17).

**WP-ARCH 3c K3b/K3c REVERSED the polarity of the first half of this file.**

3b's rule was "assert an EMISSION, not merely the absence of a paste", because
silence at the seat is #604 and an arm that counts only pastes certifies it. That
still holds — but the emitter moved. The legacy F136 chain's wake ran through the
coalescer into ``ring_supervisor_doorbell``; both are deleted, because a second
carrier over a row the tick already owns is the duplicate delivery this phase
removes. So the arms below now pin the ABSENCE of a legacy emission, and the
positive emission they used to own lives with the carrier that does it:
``test/app/delivery/test_seat_wake.py`` for ``DeliveryTick.serve`` → ``wake_seat``.

* **`on`** — the queue's tick owns the seat and emits through ``wake_seat``.
* **`off`, `drain`** — nothing serves the seat any more. That is not a hole left
  open: those positions are deleted outright in 3c slice 4 (``on`` becomes the
  sole position), and ``CAO_DELIVERY_QUEUE=on`` is what ships today.

What survives here unchanged is the CURSOR: claim/commit must still advance so an
acked or aged id is never re-emitted (#388), and the K2 content-channel write,
which dies in slice 3.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.core.delivery import MsgState, QueueMode, SwitchPosition
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.inbox_service import InboxService

SEAT_TERMINAL = "sup-p3b01"
WORKER_TERMINAL = "wrk-p3b01"

#: The positions where the QUEUE does not serve the seat. ``on`` is absent
#: because there the tick is the carrier and its coverage lives with the tick.
#: ``shadow`` was a third until #738 retired it.
NON_QUEUE_POSITIONS = [SwitchPosition.OFF, SwitchPosition.DRAIN]


@pytest.fixture
def seat_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'p3b_positions.sqlite'}",
        connect_args={"check_same_thread": False},
    )
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
    yield sessions
    engine.dispose()


def _seat(db, *, cc_inbox_path: str | None = None) -> None:
    """A supervisor mailbox at its SHIPPED shape: no ``cc_inbox_path``.

    That is the whole point of the default. ``cc_inbox_path`` is written only
    when ``supervisor.mailbox_pull`` is on, and A1.5 keeps that flag False
    because the paste ban no longer depends on it — so the shipped seat has no
    path, and a carrier that refuses without one reaches nobody.
    """
    db.add(
        TerminalModel(
            id=SEAT_TERMINAL,
            tmux_session="cao-p3b",
            tmux_window=SEAT_TERMINAL,
            provider="claude_code",
            agent_profile="chao_supervisor",
            init_state="ready",
        )
    )
    row = MailboxModel(
        id="mb_p3b_sup",
        session_name="cao-p3b",
        role="supervisor",
        current_terminal_id=SEAT_TERMINAL,
        generation=1,
        consumed_through_id=0,
        schema_version=1,
        cc_inbox_path=cc_inbox_path,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=row.id,
            generation=1,
            terminal_id=SEAT_TERMINAL,
            published_at=datetime.now(),
        )
    )


def _callback(db, *, sender: str = WORKER_TERMINAL) -> InboxModel:
    row = InboxModel(
        sender_id=sender,
        receiver_id=SEAT_TERMINAL,
        logical_receiver_id="mb_p3b_sup",
        enqueue_generation=1,
        message="LIVE_P3B_NATIVE_CALLBACK",
        orchestration_type="send_message",
        status="pending",
        created_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


class _Recorder:
    """Counts what each seam did, so an arm can assert an emission happened."""

    def __init__(self) -> None:
        self.native_rings: list[tuple[str, int]] = []
        self.pane_writes: list[str] = []

    def ring(self, terminal_id: str, max_row_id: int, **_kwargs: Any) -> str:
        self.native_rings.append((terminal_id, max_row_id))
        return "rang"

    def paste(self, terminal_id: str, *_args: Any, **_kwargs: Any) -> None:
        self.pane_writes.append(terminal_id)


def _drive(position: SwitchPosition, recorder: _Recorder) -> None:
    """Run one delivery cycle at ``position`` and record what emitted.

    Counted at the SEAMS — the doorbell ring and the pane write — and never from a
    rendered transcript, which #613 showed can report zero emitters on a seat
    where emitters had in fact fired. (The coalesce seam that used to sit between
    the runner and the ring is deleted; 3c K3c.)
    """
    service = InboxService()
    with (
        patch(
            "cli_agent_orchestrator.app.delivery.wiring.queue_position",
            return_value=position,
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
            side_effect=recorder.ring,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.send_prepared_input",
            side_effect=recorder.paste,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
            return_value={
                "tmux_session": "cao-p3b",
                "tmux_window": SEAT_TERMINAL,
                "lifecycle_generation": 1,
                "recovery_state": None,
                "metadata": {},
            },
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", MagicMock()),
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager", MagicMock()),
    ):
        outcome = service._f136_run_callback_delivery(SEAT_TERMINAL)
        service._f136_post_delivery(SEAT_TERMINAL, outcome)


def _wake_cursor(sessions) -> int:
    with sessions.begin() as db:
        mb = db.query(MailboxModel).filter_by(id="mb_p3b_sup").one()
        return int(mb.callback_notified_through_id or 0)


@pytest.mark.parametrize("position", NON_QUEUE_POSITIONS, ids=lambda p: p.value)
def test_the_legacy_chain_emits_nothing_in_any_position(seat_db, position: SwitchPosition) -> None:
    """WP-ARCH 3c K3c: the F136 chain rings NOBODY.

    Its ring ran through the coalescer, which is deleted, and the delivery tick
    is the seat's single carrier. So the assertion that used to demand an
    emission here now demands its absence — a ring reintroduced on this path is
    the duplicate delivery over one id that 3c exists to remove.

    This is NOT a silence certificate for the seat: the positive emission is
    owned by ``test/app/delivery/test_seat_wake.py`` (``DeliveryTick.serve`` ->
    ``wake_seat``), and these non-queue positions are themselves deleted in
    slice 4.
    """
    with seat_db.begin() as db:
        _seat(db)
        _callback(db)

    recorder = _Recorder()
    _drive(position, recorder)

    assert recorder.native_rings == [], (
        f"the legacy F136 chain rang the seat under {position.value}: "
        "that is a second carrier over a row the tick owns"
    )
    assert recorder.pane_writes == [], "a supervisor-role receiver is never pasted (K8)"


@pytest.mark.parametrize("position", NON_QUEUE_POSITIONS, ids=lambda p: p.value)
def test_the_run_advances_the_cursor_so_an_acked_id_is_never_reclaimed(
    seat_db, position: SwitchPosition
) -> None:
    """The ring is gone; the CURSOR is not (#388).

    Deleting the wake transport must not delete the claim/commit that gates an
    acked or aged id, so the same epoch is driven twice: the first cycle advances
    the cursor past the row, the second finds nothing above it and writes
    nothing.
    """
    with seat_db.begin() as db:
        _seat(db)
        row = _callback(db)
        row_id = int(row.id)

    first = _Recorder()
    _drive(position, first)
    assert _wake_cursor(seat_db) == row_id, "the first cycle must advance the wake cursor"

    before = _wake_cursor(seat_db)
    second = _Recorder()
    _drive(position, second)
    assert _wake_cursor(seat_db) == before, "a re-run must not re-claim an id below the cursor"
    assert second.native_rings == []
    assert second.pane_writes == []


def test_a_seat_with_a_content_channel_still_writes_the_file(seat_db, tmp_path) -> None:
    """The K2 path is UNCHANGED where it is configured.

    The r2 fix makes the file optional, not gone: a deployment that opted into
    ``supervisor.mailbox_pull`` still gets its ``team-lead.json`` written, and
    the wake still rings. K2 dies in 3c, not here.
    """
    inbox_path = tmp_path / "team-lead.json"
    with seat_db.begin() as db:
        _seat(db, cc_inbox_path=str(inbox_path))
        _callback(db)

    recorder = _Recorder()
    written: list[Any] = []

    # Patched on the module the runner imports FROM, since the import is inside
    # the function body.
    with patch(
        "cli_agent_orchestrator.services.teammate_push_service."
        "write_supervisor_callback_notification",
        side_effect=lambda **kwargs: written.append(kwargs) or _WrittenResult(),
    ):
        _drive(SwitchPosition.DRAIN, recorder)

    assert written, "a configured content channel must still be written"
    assert recorder.native_rings == [], "but 3c K3c leaves the wake to the tick"


class _WrittenResult:
    kind = "written"
    reason = ""


# ---------------------------------------------------------------------------
# §6 — the legacy inbox is READ-ONLY from the flip.
#
# "At the flip to `on` the legacy inbox goes read-only: it stops accepting
# inserts, existing rows drain through the old path, and new rows go to
# delivery_msg. Dual-write is excluded, a dual-written row being a fifth carrier
# that would reproduce #506 inside the fix."
#
# r1 kept the legacy insert AND wrote a mode=live queue row, which is exactly
# the excluded fifth carrier. These count ROWS, in both tables, per position.
# ---------------------------------------------------------------------------


@pytest.fixture
def flip_env(tmp_path, monkeypatch):
    """One SQLite file holding both the legacy tables and the queue's."""
    from cli_agent_orchestrator.adapters.store.migrator import migrate
    from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
    from cli_agent_orchestrator.app.delivery import wiring

    db_file = tmp_path / "flip.sqlite"
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

    class _Clock:
        def now(self):
            from datetime import timezone

            return datetime.now(timezone.utc)

    clock = _Clock()
    store = SqliteQueueStore(pool, clock=clock)

    def install(position: SwitchPosition) -> None:
        wiring.install_delivery(wiring.DeliveryRuntime(store=store, clock=clock, position=position))

    yield sessions, store, install
    wiring.reset_delivery()
    pool.close_all()
    engine.dispose()


def _legacy_row_count(sessions) -> int:
    with sessions() as db:
        return db.query(InboxModel).count()


def _send(sessions) -> Any:
    from cli_agent_orchestrator.clients.database import _insert_routed_inbox_row
    from cli_agent_orchestrator.models.inbox import OrchestrationType

    with sessions.begin() as db:
        row = _insert_routed_inbox_row(
            db,
            sender_id=WORKER_TERMINAL,
            receiver_id=SEAT_TERMINAL,
            logical_receiver_id="mb_p3b_sup",
            message="FLIP_PROBE",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        )
        return int(row.id)


def test_on_produces_zero_legacy_inbox_inserts(flip_env) -> None:
    """§6's requirement, counted in the table it is about.

    One message at `on` writes ONE queue row and NO inbox row. r1 wrote both,
    which is the dual-written fifth carrier the blueprint excludes.
    """
    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
    install(SwitchPosition.ON)

    before = _legacy_row_count(sessions)
    message_id = _send(sessions)

    assert _legacy_row_count(sessions) == before, "the legacy inbox must accept no inserts at `on`"
    assert store.count(mode=QueueMode.LIVE) == 1
    assert message_id > 0, "the caller still gets an integer handle"


@pytest.mark.parametrize(
    "position",
    [SwitchPosition.OFF, SwitchPosition.DRAIN],
    ids=lambda p: p.value,
)
def test_the_other_positions_still_write_the_legacy_row(flip_env, position) -> None:
    """`off` and `drain` write the legacy row and no queue row.

    `drain` is deliberately in this list rather than with `on`. §6: "the tick
    keeps claiming, injecting and reclaiming the mode='live' rows already in
    delivery_msg while new enqueues go to the legacy inbox, and `drain` accepts
    no new queue rows, so it empties on its own budget."
    """
    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
    install(position)

    _send(sessions)

    assert _legacy_row_count(sessions) == 1
    assert store.count(mode=QueueMode.LIVE) == 0, "only `on` writes the authority row"
    assert store.count() == 0, "and no other row: #738 left no observational writer"


def test_the_window_dedup_is_carried_with_all_five_conjuncts(flip_env) -> None:
    """D13's F475 check, carried into the queue because legacy has no row to find.

    At `on` the legacy predicate would scan an empty inbox and never suppress
    anything, so the queue carries the window itself. Asserted at the STORE,
    because that is where the carried predicate lives — driving it through the
    choke point would also exercise legacy's own eligibility guard
    (`_f475_should_dedup`), which decides whether a content hash is computed at
    all and is deliberately unchanged by this phase.

    All five conjuncts: same sender, same receiver, matching hash, `park_warm`
    not true, `barrier_id` null.
    """
    from datetime import timezone

    sessions, store, install = flip_env
    install(SwitchPosition.ON)
    now = datetime.now(timezone.utc)

    from cli_agent_orchestrator.core.delivery import EnqueueDraft

    store.enqueue(
        EnqueueDraft(
            idempotency_key="dedup-1",
            receiver_id="mb_p3b_sup",
            sender_id=WORKER_TERMINAL,
            mode=QueueMode.LIVE,
            content_hash="hash-a",
            legacy_message_id=1,
        )
    )

    hit = store.find_recent_duplicate(
        sender_id=WORKER_TERMINAL,
        receiver_id="mb_p3b_sup",
        content_hash="hash-a",
        window_s=60,
        now=now,
    )
    assert hit is not None and hit.legacy_message_id == 1

    # Each conjunct, negated one at a time.
    assert (
        store.find_recent_duplicate(
            sender_id="someone-else",
            receiver_id="mb_p3b_sup",
            content_hash="hash-a",
            window_s=60,
            now=now,
        )
        is None
    )
    assert (
        store.find_recent_duplicate(
            sender_id=WORKER_TERMINAL,
            receiver_id="mb_other",
            content_hash="hash-a",
            window_s=60,
            now=now,
        )
        is None
    )
    assert (
        store.find_recent_duplicate(
            sender_id=WORKER_TERMINAL,
            receiver_id="mb_p3b_sup",
            content_hash="hash-b",
            window_s=60,
            now=now,
        )
        is None
    )
    assert (
        store.find_recent_duplicate(
            sender_id=WORKER_TERMINAL,
            receiver_id="mb_p3b_sup",
            content_hash="hash-a",
            window_s=60,
            now=now,
            park_warm=True,
        )
        is None
    )
    assert (
        store.find_recent_duplicate(
            sender_id=WORKER_TERMINAL,
            receiver_id="mb_p3b_sup",
            content_hash="hash-a",
            window_s=60,
            now=now,
            barrier_id=7,
        )
        is None
    )
    # And the window itself: the same content an hour later is ordinary traffic.
    assert (
        store.find_recent_duplicate(
            sender_id=WORKER_TERMINAL,
            receiver_id="mb_p3b_sup",
            content_hash="hash-a",
            window_s=1,
            now=now + timedelta(hours=1),
        )
        is None
    )


def test_the_seat_drains_the_queue_at_on_and_the_ack_closes_the_epoch(flip_env) -> None:
    """§5b's consumer contract, end to end over the queue.

    The half of §6 that is easy to forget: making the inbox read-only is only
    safe if the DRAIN follows the rows. A seat woken at `on` calls
    ``list_messages`` and then ``ack_messages``, and if those still read the
    inbox it would look for its messages in a table nothing writes any more and
    drain nothing — silent loss arriving through the flip.

    The ack also closes the covering epoch, which is what makes I4 hold: a
    consumed epoch is terminal, so a later arrival opens a NEW one and #568's
    re-announcement is unreachable rather than filtered.
    """
    from cli_agent_orchestrator.services.mailbox_service import ack_messages, list_messages

    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
    install(SwitchPosition.ON)

    message_id = _send(sessions)
    digest = store.build_digest(
        "mb_p3b_sup",
        tuple(m.msg_id for m in store.pending_for_receiver("mb_p3b_sup")),
        now=store._clock.now(),
    )

    listed = list_messages("mb_p3b_sup")
    assert [item["id"] for item in listed["items"]] == [message_id]
    assert listed["items"][0]["message"] == "FLIP_PROBE"
    msg_id = listed["items"][0]["msg_id"]
    assert msg_id, "a listed row names its queue id for cao diag"

    result = ack_messages(SEAT_TERMINAL, message_id)
    assert result["consumed_through_id"] == message_id

    # Read by the queue id the listing itself carried. The legacy-id lookup this
    # used to go through was the mirror's join key and went with it (#738); the
    # id a seat is handed is the one a reader has.
    settled = store.get(msg_id)
    assert settled is not None and settled.state is MsgState.DELIVERED
    closed = store.digest_at("mb_p3b_sup", digest.epoch)
    assert closed is not None and closed.consumed_via == "mcp_ack"
    assert not closed.open, "a consumed epoch is terminal (I4)"


# ---------------------------------------------------------------------------
# D8 — the completion-cancel, from its production trigger.
#
# r1 implemented the store rule and the wiring hook and left NO production call
# site, so #435's mechanism existed but never fired. The trigger is the single
# status egress every origin passes through, edge-triggered into COMPLETED.
# ---------------------------------------------------------------------------


def test_a_receiver_completion_cancels_its_flagged_steers(flip_env) -> None:
    """#435's mechanism, driven from the status edge rather than called directly.

    `supersede_key` cannot reach this case: the aged steer is addressed to the
    WORKER and the completion callback to the supervisor, so no newer row ever
    lands in the worker's mailbox to supersede it.

    The limits D8 states are asserted alongside, because they are what keeps the
    rule honest: `ready` rows only, and only rows that opted in.
    """
    from cli_agent_orchestrator.core.delivery import EnqueueDraft
    from cli_agent_orchestrator.services.queue_carrier import (
        forget_terminal_status,
        note_terminal_status,
    )

    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
    install(SwitchPosition.ON)
    forget_terminal_status(SEAT_TERMINAL)

    steer = store.enqueue(
        EnqueueDraft(
            idempotency_key="steer",
            receiver_id="mb_p3b_sup",
            mode=QueueMode.LIVE,
            cancel_on_complete=True,
            legacy_message_id=101,
        )
    )
    plain = store.enqueue(
        EnqueueDraft(
            idempotency_key="plain",
            receiver_id="mb_p3b_sup",
            mode=QueueMode.LIVE,
            legacy_message_id=102,
        )
    )

    # The edge: anything but COMPLETED does nothing at all.
    note_terminal_status(SEAT_TERMINAL, "processing")
    assert store.get(steer.msg_id).state is MsgState.READY  # type: ignore[union-attr]

    note_terminal_status(SEAT_TERMINAL, "completed")

    assert store.get(steer.msg_id).state is MsgState.SUPERSEDED  # type: ignore[union-attr]
    assert store.get(plain.msg_id).state is MsgState.READY, (  # type: ignore[union-attr]
        "a row that did not opt in is untouched"
    )


def test_the_completion_cancel_fires_once_per_edge_not_per_publish(flip_env) -> None:
    """D8: evaluated once per completion EVENT, not as a standing predicate.

    That distinction is what keeps its stated limit true — a steer reclaimed to
    `ready` AFTER the completion is not retroactively cancelled — so a second
    publish of the same latched status must not re-run the rule.
    """
    from cli_agent_orchestrator.core.delivery import EnqueueDraft
    from cli_agent_orchestrator.services.queue_carrier import (
        forget_terminal_status,
        note_terminal_status,
    )

    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
    install(SwitchPosition.ON)
    forget_terminal_status(SEAT_TERMINAL)

    note_terminal_status(SEAT_TERMINAL, "completed")

    later = store.enqueue(
        EnqueueDraft(
            idempotency_key="later-steer",
            receiver_id="mb_p3b_sup",
            mode=QueueMode.LIVE,
            cancel_on_complete=True,
            legacy_message_id=201,
        )
    )
    # Same status published again: no new edge, so no second evaluation.
    note_terminal_status(SEAT_TERMINAL, "completed")

    assert store.get(later.msg_id).state is MsgState.READY  # type: ignore[union-attr]


def test_the_write_through_row_satisfies_the_public_message_shape(flip_env) -> None:
    """The detached row must carry the defaults an INSERT would have applied.

    Caught live rather than by construction: the `on` arm enqueued the queue row
    correctly and then returned a validation error to the caller, because
    `created_at` is a server-side column default and there is no INSERT to apply
    it. A send that stores the message and reports failure is worse than either
    outcome alone, so the shape is asserted here.
    """
    from cli_agent_orchestrator.clients.database import _inbox_message_from_row

    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
    install(SwitchPosition.ON)

    from cli_agent_orchestrator.clients.database import _insert_routed_inbox_row
    from cli_agent_orchestrator.models.inbox import OrchestrationType

    with sessions.begin() as db:
        row = _insert_routed_inbox_row(
            db,
            sender_id=WORKER_TERMINAL,
            receiver_id=SEAT_TERMINAL,
            logical_receiver_id="mb_p3b_sup",
            message="SHAPE_PROBE",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        )
        message = _inbox_message_from_row(row)

    assert message.id > 0
    assert message.created_at is not None
    assert message.message == "SHAPE_PROBE"
    assert message.status is MessageStatus.PENDING


# WP-ARCH 3c K6: ``test_k6_the_interim_reconcile_is_muted_when_the_queue_owns_delivery``
# pinned the mute on ``services/seat_wake_reconcile``. The module is DELETED —
# a mute is a flag in front of a second emitter, and 3c removes the emitter — so
# there is nothing left to mute or to assert a mute on.


# ---------------------------------------------------------------------------
# #741 — the mute is ROW-SCOPED, because the read-only inbox is not an EMPTY one.
#
# The box live round under `on` found the seat silent and every live queue row
# dead with attempts=0. Its log carries the mechanism verbatim: three
# `delivery write-through failed; the caller falls back to the legacy insert`
# warnings over `sqlite3.OperationalError: database is locked`, raised where the
# queue's own connection takes BEGIN IMMEDIATE while the caller still holds an
# open write transaction on the SAME file. The fallback wrote a legacy row, and
# a terminal-wide mute then left that row with NO carrier at all.
#
# These arms fix the SCOPE of the mute, not the lock. The lock is a genuine
# race that the fallback exists to survive; what may not survive it is silence.
# ---------------------------------------------------------------------------


def _doorbell_decision(terminal_id: str, row_id: int) -> str:
    """The REAL ``ring_supervisor_doorbell``, driven to its first decision.

    Deliberately not the ``_drive`` harness above: that one patches the doorbell
    with a recorder, so the module's own mute never runs and an arm written on it
    would certify a mute it never reached. The K3 surface is the subject here, so
    it is the thing that has to be called.

    F747 (#747) took the ``supervisor.doorbell`` default OUT of that call site
    (the registry is the tier that owns it now, and it ships False), so a stub
    that echoes the CALL-SITE default answers ``None`` and the ring is skipped at
    the outer gate before the ``queue_owns`` MUTE this helper exists to probe is
    ever consulted. The shipped posture is OFF and is pinned where it belongs,
    in ``test_f747_native_default.py``; this arm is about the mute, not the flag,
    so it says which posture it needs -- the same shape as ``_seat_meta_and_flags``
    in ``test_f803_wake_only_no_consume.py``.
    """
    from cli_agent_orchestrator.services import doorbell_service

    with patch.object(
        doorbell_service.ConfigService,
        "get",
        side_effect=lambda key, default=None: True if key == "supervisor.doorbell" else default,
    ):
        return doorbell_service.ring_supervisor_doorbell(terminal_id, row_id)


def test_a_legacy_row_at_on_keeps_its_carrier(seat_db) -> None:
    """The `on` arm the position sweep above could not have: a row in the LEGACY
    inbox while the switch says the queue owns delivery.

    Two surfaces, because one alone would not have caught the round's shape:
    ``deliver_pending`` is what moves the row, and K3's ring is what wakes the
    seat about it. Both were muted terminal-wide, so the row had no carrier at
    all -- which is #604 with a switch in front of it.
    """
    with seat_db.begin() as db:
        _seat(db)
        row = _callback(db)
        row_id = int(row.id)

    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.queue_position",
        return_value=SwitchPosition.ON,
    ):
        skips: list[str] = []
        service = InboxService()
        with (
            patch.object(
                InboxService, "_log_delivery_skip", side_effect=lambda t, r: skips.append(r)
            ),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", MagicMock()),
            patch("cli_agent_orchestrator.services.inbox_service.provider_manager", MagicMock()),
        ):
            service.deliver_pending(SEAT_TERMINAL)
        assert "queue_owns_delivery" not in skips, (
            "a row the queue does not own lost its only carrier at `on`: the legacy "
            "inbox is read-only from the flip, not empty (#741)"
        )

        # The doorbell flag is pinned ON by the helper so this reaches the mute;
        # the returned value is the queue_owns decision, not the shipped posture.
        assert (
            _doorbell_decision(SEAT_TERMINAL, row_id) != "skipped_disabled"
        ), "K3 stayed muted for a row the tick will never serve"


def test_the_mute_still_holds_when_the_queue_owns_everything(seat_db) -> None:
    """The other direction, and the one that keeps the fix honest.

    With NO legacy row for the receiver the queue owns everything it is owed, so
    every D6 surface stays quiet -- otherwise this change would have replaced a
    silent seat with the duplicate family the phase exists to close.
    """
    with seat_db.begin() as db:
        _seat(db)

    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.queue_position",
        return_value=SwitchPosition.ON,
    ):
        skips: list[str] = []
        service = InboxService()
        with (
            patch.object(
                InboxService, "_log_delivery_skip", side_effect=lambda t, r: skips.append(r)
            ),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", MagicMock()),
            patch("cli_agent_orchestrator.services.inbox_service.provider_manager", MagicMock()),
        ):
            service.deliver_pending(SEAT_TERMINAL)
        assert skips == ["queue_owns_delivery"], skips
        assert _doorbell_decision(SEAT_TERMINAL, 0) == "skipped_disabled"


def test_the_predicate_names_which_rows_the_queue_owns(seat_db) -> None:
    """The predicate itself, so a regression names the cause and not a symptom."""
    from cli_agent_orchestrator.services.queue_carrier import queue_owns_receiver_delivery

    with seat_db.begin() as db:
        _seat(db)

    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.queue_position",
        return_value=SwitchPosition.ON,
    ):
        assert queue_owns_receiver_delivery(SEAT_TERMINAL) is True

        with seat_db.begin() as db:
            _callback(db)
        assert queue_owns_receiver_delivery(SEAT_TERMINAL) is False

    # #738 retired `shadow`; `drain` is the non-serving position this now asks
    # about. The assertion is unchanged: outside `on` the queue owns nothing.
    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.queue_position",
        return_value=SwitchPosition.DRAIN,
    ):
        assert queue_owns_receiver_delivery(SEAT_TERMINAL) is False


# ---------------------------------------------------------------------------
# #741 r3 — the POSITIVE carrier arms for K2 and K6.
#
# The r1 EMPIRICAL adjudication ruled the existing `on` coverage a SHOULD-level
# hole: `test_a_legacy_row_at_on_keeps_its_carrier` drives `deliver_pending` and
# K3, and `test_the_predicate_names_which_rows_the_queue_owns` drives the
# predicate in isolation, but NOTHING put a pending legacy row at `on` through
# K2's `write_supervisor_callback_notification` or K6's per-mailbox reconcile.
# Those two call sites are where #741 moved the mute from a module-wide return
# to a receiver-scoped question, so a regression that reverts either one to the
# coarse `queue_owns_delivery()` would have been invisible: the predicate would
# still be correct and the row would still have no carrier.
#
# Both arms therefore call the REAL public entry point and assert an EMISSION,
# in the same spirit as the file's opening note — an arm that checks only for
# the absence of a mute certifies nothing about whether anything was carried.
# ---------------------------------------------------------------------------


def test_a_legacy_row_at_on_keeps_the_k2_content_carrier(flip_env, tmp_path) -> None:
    """K2's writer must WRITE for a row the queue does not own.

    `write_supervisor_callback_notification` is the single public native
    callback writer, and 3b muted it terminal-wide on `queue_owns_delivery()`.
    At `on` the legacy inbox stops accepting inserts but does not become empty,
    so that mute stranded every row still in it: K2 returned
    `skipped/queue_owns_delivery` and the durable native entry was never
    written. The assertion is on the written FILE, not merely on the return
    kind, because a writer that reports success and leaves no entry is the same
    silent seat with a friendlier log line.
    """
    from cli_agent_orchestrator.clients.database import _inbox_message_from_row
    from cli_agent_orchestrator.services.teammate_push_service import (
        write_supervisor_callback_notification,
    )

    sessions, _store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
        row = _callback(db)
        message = _inbox_message_from_row(row)

    inbox_path = tmp_path / "k2-native-inbox.json"

    install(SwitchPosition.ON)
    result = write_supervisor_callback_notification(
        inbox_path=inbox_path,
        mailbox_id="mb_p3b_sup",
        message=message,
    )

    assert result.reason != "queue_owns_delivery", (
        "K2 was muted for a row the queue does not own: the legacy inbox is "
        "read-only from the flip, not empty, so this row's only content "
        "carrier just refused it (#741)"
    )
    assert result.kind == "written", result
    assert inbox_path.exists(), "K2 reported a carry and wrote no durable entry"

    entries = json.loads(inbox_path.read_text())
    assert len(entries) == 1, entries


def test_k2_stays_muted_when_the_queue_owns_every_row(flip_env, tmp_path) -> None:
    """The other direction: with no legacy row, K2 must stay silent.

    Without this arm the repair above would be satisfied by deleting the mute,
    which restores the duplicate-carrier family (#506) this phase closes.
    """
    from datetime import timezone

    from cli_agent_orchestrator.models.inbox import InboxMessage, OrchestrationType
    from cli_agent_orchestrator.services.teammate_push_service import (
        write_supervisor_callback_notification,
    )

    sessions, _store, install = flip_env
    with sessions.begin() as db:
        _seat(db)

    # A message the QUEUE holds: shaped like a delivered row, absent from the
    # legacy inbox, which is exactly the disjointness the predicate relies on.
    message = InboxMessage(
        id=9001,
        sender_id=WORKER_TERMINAL,
        receiver_id=SEAT_TERMINAL,
        message="QUEUE_OWNED",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    inbox_path = tmp_path / "k2-muted-inbox.json"

    install(SwitchPosition.ON)
    result = write_supervisor_callback_notification(
        inbox_path=inbox_path,
        mailbox_id="mb_p3b_sup",
        message=message,
    )

    assert result.kind == "skipped"
    assert result.reason == "queue_owns_delivery"
    assert not inbox_path.exists(), "the muted writer still produced a second carrier"


# WP-ARCH 3c K6: ``test_a_legacy_row_at_on_keeps_the_k6_reconcile_carrier`` proved
# the #741 row-scoped mute still woke a seat holding a pre-flip legacy row. With
# ``services/seat_wake_reconcile`` deleted, a legacy row's carrier at `on` is the
# delivery tick like every other row's; the arm covering that is
# ``test_a_legacy_row_at_on_keeps_its_carrier`` above, and the tick's own seat
# wake is ``test/app/delivery/test_seat_wake.py``.
