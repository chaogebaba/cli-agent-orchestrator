"""The seat's carrier, per queue position (WP-ARCH 3b, §A1.5, AC-3b case 17).

**These assert an EMISSION, not merely the absence of a paste.** An arm that
checks only for silence certifies silence, and silence at the seat is #604 — the
failure this whole phase exists to remove. The gate's r1 round found exactly that
hole: the `shadow` position produced no paste AND no native write, and the unit
suite of the day was satisfied because it counted pastes.

The four positions do not share a carrier, and that is the design rather than an
accident:

* **`on`** — the queue's tick owns the seat and emits through ``wake_seat``.
  That path is covered in ``test/app/delivery/test_seat_wake.py``.
* **`off`, `shadow`, `drain`** — the queue does not serve the seat, so the only
  remaining carrier is the F136 chain into ``ring_supervisor_doorbell``, which is
  why A1.5 flips ``supervisor.wake.native``'s shipped default to True. THIS file
  covers those three.

The break r1 shipped was in that chain: ``cc_inbox_path`` is K2's pull-mode file,
``supervisor.mailbox_pull`` ships False, so the runner returned ``no_path``,
``written`` stayed 0, and ``_f136_post_delivery``'s ``written > 0`` gate held the
doorbell shut. The seat was neither pasted nor woken.
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

#: The three positions where the QUEUE does not serve the seat. ``on`` is absent
#: because there the tick is the carrier and its coverage lives with the tick.
NON_QUEUE_POSITIONS = [SwitchPosition.OFF, SwitchPosition.SHADOW, SwitchPosition.DRAIN]


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
        self.submits: list[dict[str, Any]] = []
        self.native_rings: list[tuple[str, int]] = []
        self.pane_writes: list[str] = []

    def submit(self, terminal_id: str, max_row_id: int, **kwargs: Any) -> None:
        """Stand in for the coalesce buffer, and ring THROUGH to the doorbell.

        The runner hands its wake to ``doorbell_coalesce_service.submit``, which
        merges near-simultaneous callbacks and flushes on the delivery loop. A
        unit test has no loop bound, so a real submit would buffer forever and an
        arm that stopped here would prove only that the runner tried. Calling
        through to the ring is what makes the assertion cover the whole chain.
        """
        self.submits.append({"terminal_id": terminal_id, "row": max_row_id, **kwargs})
        from cli_agent_orchestrator.services.doorbell_service import (
            ring_supervisor_doorbell,
        )

        ring_supervisor_doorbell(terminal_id, max_row_id, **kwargs)

    def ring(self, terminal_id: str, max_row_id: int, **_kwargs: Any) -> str:
        self.native_rings.append((terminal_id, max_row_id))
        return "rang"

    def paste(self, terminal_id: str, *_args: Any, **_kwargs: Any) -> None:
        self.pane_writes.append(terminal_id)


def _drive(position: SwitchPosition, recorder: _Recorder) -> None:
    """Run one delivery cycle at ``position`` and record what emitted.

    Counted at the SEAMS — the coalesce submit and the doorbell ring — and never
    from a rendered transcript, which #613 showed can report zero emitters on a
    seat where emitters had in fact fired.
    """
    service = InboxService()
    coalesce = MagicMock()
    coalesce.submit.side_effect = recorder.submit
    with (
        patch(
            "cli_agent_orchestrator.app.delivery.wiring.queue_position",
            return_value=position,
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service",
            coalesce,
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


@pytest.mark.parametrize("position", NON_QUEUE_POSITIONS, ids=lambda p: p.value)
def test_the_seat_is_woken_natively_in_every_non_queue_position(
    seat_db, position: SwitchPosition
) -> None:
    """An EMISSION per position, at the shipped defaults (§A1.5).

    The gate's r1 round is the reason this is parametrised over positions rather
    than asserted once: the `on` arm passed, `shadow` produced nothing at all,
    and no unit case noticed because every one of them counted pastes.

    A run where the ring count is zero fails, and it fails in the direction that
    matters — a paste is an ugly carrier, silence is the bug.
    """
    with seat_db.begin() as db:
        _seat(db)
        _callback(db)

    recorder = _Recorder()
    _drive(position, recorder)

    assert recorder.native_rings, (
        f"the seat received NO native wake under {position.value}: "
        "neither pasted nor woken is #604, not a fix"
    )
    assert recorder.pane_writes == [], "a supervisor-role receiver is never pasted (K8)"


@pytest.mark.parametrize("position", NON_QUEUE_POSITIONS, ids=lambda p: p.value)
def test_the_wake_advances_the_cursor_so_an_acked_id_never_re_rings(
    seat_db, position: SwitchPosition
) -> None:
    """The content channel is optional; the CURSOR is not (#388).

    Dropping the ``no_path`` refusal must not drop the claim/commit that gates an
    acked or aged id. So the same epoch is driven twice: the first cycle rings,
    the second finds nothing above the cursor and does not.
    """
    with seat_db.begin() as db:
        _seat(db)
        _callback(db)

    first = _Recorder()
    _drive(position, first)
    assert len(first.native_rings) == 1

    second = _Recorder()
    _drive(position, second)
    assert second.native_rings == [], "a re-run above an advanced cursor must not re-ring"
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
        _drive(SwitchPosition.SHADOW, recorder)

    assert written, "a configured content channel must still be written"
    assert recorder.native_rings, "and the wake still rings"


class _WrittenResult:
    kind = "written"
    reason = ""


# ---------------------------------------------------------------------------
# §6 — the legacy inbox is READ-ONLY from the flip.
#
# "At the shadow-to-on flip the legacy inbox goes read-only: it stops accepting
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
    from cli_agent_orchestrator.app.delivery.mirror import MirrorWriter

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
        wiring.install_delivery(
            wiring.DeliveryRuntime(
                store=store, clock=clock, position=position, mirror=MirrorWriter(store, clock)
            )
        )

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
    assert store.count(mode=QueueMode.SHADOW) == 0, "no mirror on top of the authority row"
    assert message_id > 0, "the caller still gets an integer handle"


@pytest.mark.parametrize(
    "position",
    [SwitchPosition.OFF, SwitchPosition.SHADOW, SwitchPosition.DRAIN],
    ids=lambda p: p.value,
)
def test_the_other_positions_still_write_the_legacy_row(flip_env, position) -> None:
    """`off` and `drain` write legacy only; `shadow` writes legacy plus a mirror.

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
    # 3a's shadow MIRROR is deliberately not asserted here. It fires from
    # `_create_inbox_message_unfenced`, one level above the choke point this
    # test drives directly, and it has its own coverage in
    # test/app/delivery/test_mirror.py. What matters at this seam is that the
    # legacy row is still written and no LIVE row appears.


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
    assert listed["items"][0]["msg_id"], "a listed row names its queue id for cao diag"

    result = ack_messages(SEAT_TERMINAL, message_id)
    assert result["consumed_through_id"] == message_id

    settled = store.get_by_legacy_id(message_id)
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


def test_k6_the_interim_reconcile_is_muted_when_the_queue_owns_delivery(flip_env) -> None:
    """K6 is a kill-list entry, not a component to integrate (D11).

    It is server-side, so it is a genuine improvement on the dead client-side
    hook, but it repairs a NOTIFICATION path rather than making the durable row's
    observation unconditional, and it drives the legacy inbox. Left running at
    `on` it is a second wake emitter over rows the tick already owns — the
    emitter count case 17 forbids. Caught from a live sandbox log, where its
    daemon announced itself while the queue was serving.
    """
    from cli_agent_orchestrator.services import seat_wake_reconcile

    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)

    install(SwitchPosition.ON)
    assert seat_wake_reconcile.reconcile_seat_wakes() == [], "K6 must be silent at `on`"

    # And it is NOT muted in the positions where the queue does not serve the
    # seat, because there it is still part of the legacy chain.
    install(SwitchPosition.SHADOW)
    assert seat_wake_reconcile._queue_owns_delivery() is False


# ---------------------------------------------------------------------------
# I5 — an emission the stored rows cannot see is the failure I5 exists to end.
#
# The r2 gate's blocker: at the exact head the shadow live arm wrote 500 bytes
# to the seat's socket, left the pane byte-identical, and recorded
# `delivery_attempt=0`. The carrier fired and `cao diag <msg_id>` said nothing
# was ever attempted, which is the pane archaeology #604 was diagnosed through.
#
# This case drives the PRODUCTION chain — the real `_f136_post_delivery`, the
# real `ring_supervisor_doorbell`, the real `_attempt_native_ring` — and stubs
# only the socket itself, so it fails if either the attempt insert or the
# emission is removed.
# ---------------------------------------------------------------------------


class _SocketCapture:
    """Stands in for the seat's Unix socket, and for nothing else."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def write(self, socket_path: str, payload: str, auth_token: Any = None) -> None:
        self.writes.append((socket_path, payload))
        return None


def _registry_record() -> Any:
    from cli_agent_orchestrator.services.cc_session_registry import RegistryRecord

    return RegistryRecord(
        pid=4242,
        session_id="sess-p3b",
        cwd="/tmp/p3b",
        tmux=f"cao-p3b:{SEAT_TERMINAL}.%1",
        version="2.1.5",
        peer_protocol=1,
        messaging_socket_path="/run/p3b/seat.sock",
        proc_start=99,
        status="idle",
        status_updated_at="2026-09-05T00:00:00Z",
        updated_at="2026-09-05T00:00:00Z",
        raw={},
    )


def _drive_shadow_production(capture: _SocketCapture, pastes: list[str]) -> None:
    """One real delivery cycle at `shadow`, with only the socket stubbed.

    `ring_supervisor_doorbell` is deliberately NOT patched here, unlike the
    position arms above: the row this case is about is written inside it, so a
    recorder standing in for the ring would assert against a double and prove
    nothing about the production chain.
    """
    from cli_agent_orchestrator.services import cc_session_registry
    from cli_agent_orchestrator.services.cc_session_registry import ResolveResult

    service = InboxService()
    coalesce = MagicMock()

    def _submit(terminal_id: str, max_row_id: int, **kwargs: Any) -> None:
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        ring_supervisor_doorbell(terminal_id, max_row_id, **kwargs)

    coalesce.submit.side_effect = _submit
    metadata = {
        "tmux_session": "cao-p3b",
        "tmux_window": SEAT_TERMINAL,
        "lifecycle_generation": 1,
        "recovery_state": None,
        "metadata": {},
    }
    with (
        patch(
            "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service",
            coalesce,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
            return_value=metadata,
        ),
        patch.object(
            cc_session_registry, "resolve_target", return_value=ResolveResult(_registry_record())
        ),
        patch.object(cc_session_registry, "check_version_guard", return_value=None),
        patch.object(cc_session_registry, "read_peer_token", return_value="tok"),
        patch.object(cc_session_registry, "verify_wake", return_value=True),
        patch.object(cc_session_registry, "write_to_socket", side_effect=capture.write),
        patch(
            "cli_agent_orchestrator.services.terminal_service.send_prepared_input",
            side_effect=lambda terminal_id, *a, **k: pastes.append(terminal_id),
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", MagicMock()),
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager", MagicMock()),
    ):
        outcome = service._f136_run_callback_delivery(SEAT_TERMINAL)
        service._f136_post_delivery(SEAT_TERMINAL, outcome)


def _seat_wake_attempts(store: Any, msg_id: str) -> list[Any]:
    from cli_agent_orchestrator.core.delivery import CARRIER_SEAT_WAKE

    return [a for a in store.attempts_for(msg_id) if a.carrier == CARRIER_SEAT_WAKE]


def test_the_shadow_seat_wake_writes_exactly_one_attempt_row(flip_env: Any) -> None:
    """One emitted epoch, one socket write, one `delivery_attempt` row, no paste.

    All four are asserted together on purpose. Counting the row alone would pass
    on a build that recorded an attempt nothing emitted, and counting the bytes
    alone is the r2 head — which emitted and recorded nothing, so the emission
    was true and unreadable.
    """
    from cli_agent_orchestrator.clients.database import _create_inbox_message_unfenced
    from cli_agent_orchestrator.core.delivery import AttemptOutcome

    sessions, store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
        db.add(
            TerminalModel(
                id=WORKER_TERMINAL,
                tmux_session="cao-p3b",
                tmux_window=WORKER_TERMINAL,
                provider="claude_code",
                agent_profile="grunt",
                init_state="ready",
            )
        )
    install(SwitchPosition.SHADOW)

    sent = _create_inbox_message_unfenced(WORKER_TERMINAL, SEAT_TERMINAL, "SHADOW_ATTEMPT_PROBE")
    assert store.count(mode=QueueMode.SHADOW) == 1, "3a's mirror must have written the shadow row"
    # Read by the LEGACY id rather than through `pending_for_receiver`, which
    # serves `mode='live'` rows only — a shadow row is unclaimable by
    # construction (D9), and that filter is the reason the r2 arm's row was
    # invisible to every consumer that looked for it the delivery way.
    shadow = store.get_by_legacy_id(int(sent.id))
    assert shadow is not None

    capture = _SocketCapture()
    pastes: list[str] = []
    _drive_shadow_production(capture, pastes)

    assert len(capture.writes) == 1, "the seat's carrier must emit exactly once per epoch"
    attempts = _seat_wake_attempts(store, shadow.msg_id)
    assert len(attempts) == 1, (
        "one emitted epoch owes exactly one delivery_attempt row (I5); "
        f"got {len(attempts)} for msg_id={shadow.msg_id}"
    )
    assert attempts[0].outcome is AttemptOutcome.DELIVERED
    assert pastes == [], "a supervisor-role receiver is never pasted (K8)"


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
    """
    from cli_agent_orchestrator.services import doorbell_service

    with patch.object(
        doorbell_service.ConfigService, "get", side_effect=lambda key, default=None: default
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

    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.queue_position",
        return_value=SwitchPosition.SHADOW,
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


def test_a_legacy_row_at_on_keeps_the_k6_reconcile_carrier(flip_env, monkeypatch) -> None:
    """K6's per-mailbox reconcile must still wake a seat holding a legacy row.

    #741 moved this mute from a sweep-wide `return []` into the per-mailbox
    loop. `test_k6_the_interim_reconcile_is_muted_when_the_queue_owns_delivery`
    covers the muted direction only, so a regression that restored the sweep-wide
    return would pass every shipped test while leaving an idle seat unwoken for
    rows the tick will never serve — #604 with a switch in front of it.
    """
    from cli_agent_orchestrator.services import seat_wake_reconcile
    from cli_agent_orchestrator.services.teammate_push_service import PushOutcome

    pushes: list[tuple[str, tuple[int, ...]]] = []

    def _record(terminal_id, messages, *, mailbox_id=""):
        ids = tuple(int(m.id) for m in messages)
        pushes.append((terminal_id, ids))
        return PushOutcome(pushed=True, reason="pushed", message_ids=ids)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.teammate_push_service." "attempt_teammate_push_reported",
        _record,
    )

    sessions, _store, install = flip_env
    with sessions.begin() as db:
        _seat(db)
        row = _callback(db)
        row_id = int(row.id)

    # Past the 90 s default grace window, which is what the reconcile adopts.
    later = datetime.now() + timedelta(seconds=600)

    install(SwitchPosition.ON)
    decisions = seat_wake_reconcile.reconcile_seat_wakes(now=later)

    assert decisions, (
        "K6 skipped the whole mailbox at `on` while a pending legacy row sat in "
        "it: the row's tick counterpart does not exist, so nothing else will "
        "wake this seat (#741)"
    )
    decision = decisions[0]
    assert decision.terminal_id == SEAT_TERMINAL
    assert decision.outcome == "woken", decision
    assert pushes == [(SEAT_TERMINAL, (row_id,))], pushes
