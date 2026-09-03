"""An in-memory queue for the ``app/delivery`` tests (WP-ARCH phase 3a).

A double rather than the real store, deliberately, and the division of labour is
worth stating because it is what keeps both halves honest.
``test/adapters/test_queue_store.py`` tests the STATEMENTS against real SQLite —
the ``mode`` conjunct, the untouched deadline, the single-transaction re-parent.
This file tests the DECISIONS: which legacy status becomes which terminal state,
what a veto records, what the switch being off means.  Running those against a
database would test both at once and tell you less about either when one broke.

It implements the ``QueueStore`` port structurally, which is the whole reason the
port is a Protocol: an adapter satisfies one by shape, so this is a plain class
and nothing here inherits from ``core``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cli_agent_orchestrator.core.delivery import (
    TERMINAL_STATES,
    DeadLetter,
    DeadReason,
    DeliveryAttempt,
    EnqueueDraft,
    MsgState,
    QueueMessage,
    QueueMode,
    QueueOccupancy,
    SeatDigest,
    compute_dead_by,
)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.value = start if start is not None else datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class InMemoryQueueStore:
    """``core.ports.QueueStore`` over dictionaries.

    ``raise_on`` is the seam that proves the "never breaks what it observes"
    rule: a test sets it to a method name and asserts the caller still returns
    normally.  Without it that rule could only be tested by breaking a real
    database, which is a much blunter instrument and would not distinguish "the
    hook swallowed it" from "the write happened to succeed".
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.rows: dict[str, QueueMessage] = {}
        self.by_key: dict[str, str] = {}
        self.attempts: dict[str, list[DeliveryAttempt]] = {}
        self.dead: dict[str, DeadLetter] = {}
        self.digests: dict[tuple[str, int], SeatDigest] = {}
        self.raise_on: set[str] = set()
        self._counter = 0

    def _guard(self, name: str) -> None:
        if name in self.raise_on:
            raise RuntimeError(f"injected failure in {name}")

    def enqueue(self, draft: EnqueueDraft) -> QueueMessage:
        self._guard("enqueue")
        existing = self.by_key.get(draft.idempotency_key)
        if existing is not None:
            return self.rows[existing]
        self._counter += 1
        now = self._clock.now()
        message = QueueMessage(
            # 26 characters, like a real ULID: "MSG" plus 23 digits. Getting the
            # width wrong here truncates every id to the same value and the whole
            # store silently collapses to one row.
            msg_id=f"MSG{self._counter:023d}",
            idempotency_key=draft.idempotency_key,
            payload_digest=str(hash(draft.payload)),
            receiver_id=draft.receiver_id,
            sender_id=draft.sender_id,
            kind=draft.kind,
            payload=draft.payload,
            state=MsgState.READY,
            mode=draft.mode,
            max_attempts=draft.max_attempts,
            available_at=now,
            dead_by=compute_dead_by(
                created_at=now, available_at=now, expire_after_s=draft.expire_after_s
            ),
            expire_after_s=draft.expire_after_s,
            supersede_key=draft.supersede_key,
            content_hash=draft.content_hash,
            park_warm=draft.park_warm,
            barrier_id=draft.barrier_id,
            barrier_member_key=draft.barrier_member_key,
            enqueue_generation=draft.enqueue_generation,
            cancel_on_complete=draft.cancel_on_complete,
            is_notice=draft.is_notice,
            legacy_message_id=draft.legacy_message_id,
            created_at=now,
        )
        self.rows[message.msg_id] = message
        self.by_key[message.idempotency_key] = message.msg_id
        return message

    def claim(
        self,
        *,
        lease_owner: str,
        now: datetime,
        limit: int = 1,
        receiver_id: str | None = None,
    ) -> list[QueueMessage]:
        # The mode filter is reproduced here for the same reason the real
        # statement carries it: a double that could hand out a shadow row would
        # let an app-level test pass against behaviour the store forbids.
        due = [
            row
            for row in self.rows.values()
            if row.state is MsgState.READY
            and row.mode is QueueMode.LIVE
            and row.available_at <= now
            and (receiver_id is None or row.receiver_id == receiver_id)
        ]
        return due[:limit]

    def ack(self, msg_id: str, claim_id: int, *, now: datetime) -> bool:
        row = self.rows.get(msg_id)
        if row is None or row.claim_id != claim_id:
            return False
        self.rows[msg_id] = row.model_copy(
            update={"state": MsgState.DELIVERED, "terminated_at": now}
        )
        return True

    def reclaim(self, *, now: datetime) -> tuple[int, int]:
        return (0, 0)

    def get(self, msg_id: str) -> QueueMessage | None:
        return self.rows.get(msg_id)

    def get_by_idempotency_key(self, key: str) -> QueueMessage | None:
        msg_id = self.by_key.get(key)
        return None if msg_id is None else self.rows[msg_id]

    def get_by_legacy_id(self, legacy_message_id: int) -> QueueMessage | None:
        self._guard("get_by_legacy_id")
        for row in self.rows.values():
            if row.legacy_message_id == legacy_message_id:
                return row
        return None

    def attempts_for(self, msg_id: str) -> list[DeliveryAttempt]:
        return list(self.attempts.get(msg_id, []))

    def dead_letter(self, msg_id: str) -> DeadLetter | None:
        return self.dead.get(msg_id)

    def occupancy(self) -> QueueOccupancy:
        return QueueOccupancy(
            live_non_terminal=sum(
                1
                for row in self.rows.values()
                if row.mode is QueueMode.LIVE and row.state not in TERMINAL_STATES
            )
        )

    def count(self, *, mode: QueueMode | None = None) -> int:
        return sum(1 for row in self.rows.values() if mode is None or row.mode is mode)

    def record_attempt(self, attempt: DeliveryAttempt) -> None:
        self._guard("record_attempt")
        stored = self.attempts.setdefault(attempt.msg_id, [])
        key = (attempt.claim_id, attempt.carrier)
        if any((a.claim_id, a.carrier) == key for a in stored):
            return
        stored.append(attempt)

    def settle(
        self,
        msg_id: str,
        *,
        state: MsgState,
        now: datetime,
        reason: DeadReason | None = None,
        attempts: int | None = None,
    ) -> bool:
        self._guard("settle")
        row = self.rows.get(msg_id)
        if row is None or row.terminal:
            return False
        update: dict[str, object] = {"state": state, "terminated_at": now}
        if attempts is not None:
            update["attempts"] = attempts
        self.rows[msg_id] = row.model_copy(update=update)
        if state is MsgState.DEAD and reason is not None:
            self.dead[msg_id] = DeadLetter(
                msg_id=msg_id,
                idempotency_key=row.idempotency_key,
                receiver_id=row.receiver_id,
                payload=row.payload,
                attempts=row.attempts,
                reason=reason,
                mode=row.mode,
                died_at=now,
            )
        return True

    def mark_dialog_hold(self, msg_id: str, *, held_since: datetime | None) -> None:
        row = self.rows.get(msg_id)
        if row is not None:
            self.rows[msg_id] = row.model_copy(update={"held_since": held_since})

    def open_digest(self, receiver_id: str) -> SeatDigest | None:
        candidates = [
            digest
            for (owner, _), digest in self.digests.items()
            if owner == receiver_id and digest.open
        ]
        return max(candidates, key=lambda d: d.epoch) if candidates else None

    def reparent(
        self,
        msg_id: str,
        *,
        new_receiver_id: str,
        now: datetime,
        message_prefix: str = "",
    ) -> bool:
        return False


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> InMemoryQueueStore:
    return InMemoryQueueStore(clock)
