"""The per-id quarantine for `SUBMISSION_UNCERTAIN` (WP-HERDR r2, review r1 §2).

These are the reviewer's reproducer turned into suite tests. r1 claimed the
no-second-submission property and did not have it: `reclaim` re-offered an
uncertain row for free, the injector's block was keyed by TERMINAL rather than
by id, and the block cleared on a pane-state advance — which is the evidence the
first copy had LANDED. The same digest reached the worker twice, journalled
`delivered` the second time.

Blueprint amendment (7) rules the quarantine is PER ID and holds through ANY
path. It therefore lives in the store, where ids exist, and the tests below are
written against the REAL `SqliteQueueStore` for the same reason the phase's own
store tests are: what is asserted is a property of a column and a WHERE clause,
and a double would confirm whatever it was told.

Part A and Part B below correspond to the review's Parts A and B; the review's
Part D (the seq-less wedge) is covered in `test_herdr_prompt_injector.py`, where
its whole mechanism — the injector's per-terminal marker — no longer exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeadReason,
    DeliveryAttempt,
    EnqueueDraft,
    MsgState,
)
from cli_agent_orchestrator.core.timing import DELIVERY_LEASE_S

T0 = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, connection_pool = migrate(tmp_path / "q.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok, result
    assert connection_pool is not None
    yield connection_pool
    connection_pool.close_all()


@pytest.fixture
def store(pool: ConnectionPool) -> SqliteQueueStore:
    # A FakeClock pinned at T0, because every assertion below is about WHEN a
    # row becomes claimable again and the enqueue stamp has to sit on the same
    # timeline as the reclaim stamp.
    return SqliteQueueStore(pool, clock=FakeClock(T0))


def _leased_with(
    store: SqliteQueueStore,
    outcome: AttemptOutcome,
    *,
    key: str = "k1",
    payload: str = "DIGEST-ONE",
) -> str:
    """Enqueue, claim, record ``outcome`` against that claim; return the msg id."""
    msg = store.enqueue(EnqueueDraft(idempotency_key=key, receiver_id="mb_worker", payload=payload))
    claimed = store.claim(lease_owner="tick", now=T0, limit=5)
    claim = next(m for m in claimed if m.msg_id == msg.msg_id)
    store.record_attempt(
        DeliveryAttempt(
            msg_id=msg.msg_id,
            claim_id=claim.claim_id,
            carrier="pane",
            started_at=T0,
            outcome=outcome,
            detail="herdr:agent_prompt_stalled",
        )
    )
    return msg.msg_id


# ------------------------------------------------ Part A: reclaim never re-offers


def test_an_uncertain_row_is_never_offered_again(store: SqliteQueueStore) -> None:
    """The review's Part A, inverted into the property it should have had.

    r1: ``reoffered=1``, same msg_id claimable again, ``attempts=0`` — so the row
    came back roughly every 65 s for ~26 rounds, each one a chance to submit the
    same text a second time.
    """
    msg_id = _leased_with(store, AttemptOutcome.SUBMISSION_UNCERTAIN)

    after_lease = T0 + timedelta(seconds=DELIVERY_LEASE_S + 60)
    result = store.reclaim(now=after_lease)

    assert result.quarantined == 1
    assert result.reoffered == 0
    assert result.incremented == 0

    # Not claimable now, and not at any point before the row's own deadline.
    for offset in (30, 600, 1699):
        again = store.claim(
            lease_owner="tick", now=after_lease + timedelta(seconds=offset), limit=5
        )
        assert [m.msg_id for m in again] == [], f"claimable again at +{offset}s"


def test_an_uncertain_row_keeps_its_lifetime_and_dies_at_dead_by(
    store: SqliteQueueStore,
) -> None:
    """Quarantine ends where the row already ended: ``dead_by``, once-only (D12).

    Not a new bound, which is the reason it needed no new state and no schema
    change — and the reason it cannot outlive the row.
    """
    msg_id = _leased_with(store, AttemptOutcome.SUBMISSION_UNCERTAIN)
    before = store.get(msg_id)
    assert before is not None
    dead_by = before.dead_by

    store.reclaim(now=T0 + timedelta(seconds=DELIVERY_LEASE_S + 60))
    quarantined = store.get(msg_id)
    assert quarantined is not None
    assert quarantined.dead_by == dead_by, "dead_by is stamped once and never rewritten"
    assert quarantined.available_at > dead_by, "unclaimable for the rest of its life"

    result = store.reclaim(now=dead_by + timedelta(seconds=1))
    assert [d.msg_id for d in result.dead] == [msg_id]
    assert result.dead[0].reason is DeadReason.MAX_LIFETIME
    settled = store.get(msg_id)
    assert settled is not None and settled.state is MsgState.DEAD


def test_a_quarantined_row_can_still_be_settled_by_its_receiver(
    store: SqliteQueueStore,
) -> None:
    """Quarantine blocks re-DELIVERY, never acknowledgement.

    An uncertain submission that did in fact land must close normally when the
    worker drains its mailbox, or every stall would dead-letter a message the
    agent actually answered. ``settle_through`` ignores state and claim, which is
    what makes that work without a second mechanism.
    """
    msg = store.enqueue(
        EnqueueDraft(idempotency_key="k9", receiver_id="mb_worker", legacy_message_id=41)
    )
    claimed = store.claim(lease_owner="tick", now=T0, limit=5)
    store.record_attempt(
        DeliveryAttempt(
            msg_id=msg.msg_id,
            claim_id=claimed[0].claim_id,
            carrier="pane",
            started_at=T0,
            outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
            detail="herdr:submitted_while_working",
        )
    )
    store.reclaim(now=T0 + timedelta(seconds=DELIVERY_LEASE_S + 60))

    settled = store.settle_through("mb_worker", up_to_id=41, now=T0 + timedelta(seconds=200))
    assert settled == (msg.msg_id,)
    row = store.get(msg.msg_id)
    assert row is not None and row.state is MsgState.DELIVERED


def test_only_the_uncertain_outcome_is_quarantined(store: SqliteQueueStore) -> None:
    """Every other non-delivery outcome still comes back, on its own budget.

    The quarantine is not a general "hold anything that did not deliver" rule —
    that would strand a dialog-held row and a pane-absent row, both of which are
    conditions that clear, and both of which D12 bounds by other means.
    """
    for i, outcome in enumerate(
        (
            AttemptOutcome.VETO_DIALOG,
            AttemptOutcome.VETO_UNVERIFIED,
            AttemptOutcome.PANE_ABSENT,
            AttemptOutcome.WAKE_UNREACHABLE,
        )
    ):
        fresh_store = store
        msg_id = _leased_with(fresh_store, outcome, key=f"key-{i}")
        result = fresh_store.reclaim(now=T0 + timedelta(seconds=DELIVERY_LEASE_S + 60 + i))
        assert result.quarantined == 0, f"{outcome} must not be quarantined"
        assert result.reoffered == 1, f"{outcome} must still be re-offered"
        assert fresh_store.get(msg_id) is not None


# ------------------ Part B: the exact interleaving, end to end, both carriers


def test_the_review_r1_interleaving_submits_the_digest_exactly_once(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1 §2's three-step duplicate, replayed through the real machinery.

    1. herdr writes the text and its Enter but sees no ``working``/``blocked``
       inside its five-second gate (a cold provider call, a slow model), so it
       answers ``agent_prompt_stalled`` -> ``SUBMISSION_UNCERTAIN``, carrying no
       sequence.
    2. The lease expires and ``reclaim`` runs.
    3. The agent has meanwhile STARTED on the text it did receive.

    At r1 step 3 submitted the same line again and journalled it ``delivered``.
    The store now refuses to offer the row at all, so the tick has nothing to
    serve and the injector is never called a second time — which is the property
    stated as the count that matters: ONE prompt on the wire for one id.
    """
    from cli_agent_orchestrator.adapters.herdr.client import PromptSubmission
    from cli_agent_orchestrator.app.delivery.tick import DeliveryTick
    from cli_agent_orchestrator.app.delivery.wake import WakeService
    from cli_agent_orchestrator.core.delivery import ReceiverResolution
    from cli_agent_orchestrator.services import mailbox_service
    from cli_agent_orchestrator.services import queue_carrier as qc
    from cli_agent_orchestrator.services.queue_carrier import HerdrPromptInjector

    clock = FakeClock(T0)
    store = SqliteQueueStore(pool, clock=clock)

    submitted: list[str] = []

    class Client:
        async def prompt_agent(self, *, target: str, text: str, wait_timeout_ms: int = 0):  # type: ignore[no-untyped-def]
            submitted.append(text)
            # Step 1, and step 3 would be a DELIVERED here — the test asserts
            # this method is never reached a second time at all.
            return PromptSubmission(
                outcome=AttemptOutcome.SUBMISSION_UNCERTAIN, detail="herdr:agent_prompt_stalled"
            )

        async def agent_state(self, *, target: str):  # type: ignore[no-untyped-def]
            raise AssertionError("r2 does no state read outside the client")

    monkeypatch.setattr(mailbox_service, "probe_supervisor_role", lambda _t: False)
    monkeypatch.setattr(
        HerdrPromptInjector, "_resolve", staticmethod(lambda _t: ("%3", "/run/sock"))
    )
    monkeypatch.setattr(qc, "_herdr_client", lambda _p: Client())

    class Directory:
        def resolve(self, receiver_id: str) -> ReceiverResolution:
            return ReceiverResolution(
                receiver_id=receiver_id,
                terminal_id="t-live",
                is_supervisor=False,
                pane_present=True,
                display_name="w",
            )

    class Findings:
        def record(self, code, **kwargs):  # type: ignore[no-untyped-def]
            return {}

        def list_findings(self, *, state=None, code=None):  # type: ignore[no-untyped-def]
            return []

    class NoSeat:
        def emit(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("no seat row here")

    directory = Directory()
    wake = WakeService(
        store=store,
        directory=directory,
        carrier=NoSeat(),
        injector=HerdrPromptInjector(),
        clock=clock,
    )
    tick = DeliveryTick(
        store=store,
        wake=wake,
        directory=directory,
        findings=Findings(),  # type: ignore[arg-type]
        clock=clock,
    )

    msg = store.enqueue(
        EnqueueDraft(idempotency_key="dup-1", receiver_id="mb_worker", payload="digest #7")
    )

    # Step 1 — the stall.
    tick.run_once(now=clock.now())
    assert len(submitted) == 1

    # Steps 2 and 3 — twenty lease periods, far past the point at which r1's
    # marker would have cleared on the agent's own transition.
    for _ in range(20):
        clock.advance(seconds=DELIVERY_LEASE_S + 10)
        tick.run_once(now=clock.now())

    # The COUNT is the property. What the tick submits is the composed digest
    # line covering the claimed ids, not the raw payload, so the assertion is on
    # how many times anything reached herdr for this one row.
    assert len(submitted) == 1, f"the digest reached herdr {len(submitted)} times"
    assert submitted[0].startswith("[cao] digest")
    row = store.get(msg.msg_id)
    assert row is not None
    attempts = store.attempts_for(msg.msg_id)
    uncertain = [a for a in attempts if a.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN]
    assert len(uncertain) == 1, "one uncertain attempt, never a second submission"
