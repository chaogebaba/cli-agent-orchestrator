"""``cao diag <msg_id>`` (WP-ARCH phase 3a, I5).

I5 is "one query returns a msg_id's full history", and the history has two halves
a reader needs together: the queue row with its attempts says what the delivery
machinery did, and the ``worker_event`` rows carrying the same id say what the
server decided around it.  Either alone is the pane archaeology this replaces —
attempts without decisions do not say why an injection was tried, and decisions
without attempts do not say whether it landed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock, draft

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.adapters.store.state import SqliteStateStore
from cli_agent_orchestrator.app.diag.report import DiagSources, message_payload, render_message
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeadReason,
    DeliveryAttempt,
    EnqueueDraft,
    MsgState,
    QueueMode,
)
from cli_agent_orchestrator.core.ids import is_ulid
from cli_agent_orchestrator.core.timing import DELIVERY_MAX_LIFETIME_S

NOW = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    """Declared here, not inherited: ``test/adapters/conftest.py`` is a sibling.

    Importing the CLASS across packages is fine and is what keeps one definition
    of a driven clock; only the fixture has to be re-declared where pytest can
    see it.
    """
    return FakeClock()


@pytest.fixture
def wired(tmp_path: Path, clock: FakeClock) -> Iterator[tuple[DiagSources, SqliteQueueStore]]:
    result, pool = migrate(tmp_path / "diag.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    queue = SqliteQueueStore(pool, clock=clock)
    sources = DiagSources(
        events=SqliteEventStore(pool, clock=clock),
        states=SqliteStateStore(pool),
        findings=SqliteFindingStore(pool, clock=clock),
        queue=queue,
    )
    yield sources, queue
    pool.close_all()


def test_the_view_carries_the_row_its_attempts_and_its_events(
    wired: tuple[DiagSources, SqliteQueueStore], clock: FakeClock
) -> None:
    sources, queue = wired
    row = queue.enqueue(
        EnqueueDraft(
            idempotency_key="legacy-inbox:41",
            receiver_id="mb_supervisor",
            sender_id="t-worker",
            payload="a steer",
            legacy_message_id=41,
        )
    )
    queue.record_attempt(
        DeliveryAttempt(
            msg_id=row.msg_id,
            claim_id=1,
            carrier="claude_code",
            started_at=clock.now(),
            outcome=AttemptOutcome.PANE_ABSENT,
            detail="legacy outcome=interrupted reason=proven_absent",
        )
    )
    sources.events.append(draft("t-supervisor", msg_id=row.msg_id))

    payload = message_payload(sources, row.msg_id, now=NOW)
    assert payload["header"]["found"] is True
    assert payload["header"]["mode"] == QueueMode.SHADOW.value
    assert payload["header"]["legacy_message_id"] == 41
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["outcome"] == AttemptOutcome.PANE_ABSENT.value
    assert len(payload["events"]) == 1


def test_the_deadline_is_printed_beside_creation(
    wired: tuple[DiagSources, SqliteQueueStore],
) -> None:
    """The cheapest possible audit of the once-only stamp.

    The gap between the two is ``DELIVERY_MAX_LIFETIME_S``, or the caller's
    shorter expiry, and nothing else.  An operator reading a row whose gap has
    grown is looking at a deadline that was recomputed.
    """
    sources, queue = wired
    row = queue.enqueue(EnqueueDraft(idempotency_key="k1", receiver_id="mb_x"))
    payload = message_payload(sources, row.msg_id, now=NOW)
    created = datetime.fromisoformat(payload["header"]["created_at"])
    deadline = datetime.fromisoformat(payload["header"]["dead_by"])
    assert (deadline - created).total_seconds() == DELIVERY_MAX_LIFETIME_S


def test_a_dead_row_names_its_reason(
    wired: tuple[DiagSources, SqliteQueueStore], clock: FakeClock
) -> None:
    """D12: no row reaches ``delivery_dead`` without saying why."""
    sources, queue = wired
    row = queue.enqueue(EnqueueDraft(idempotency_key="k1", receiver_id="mb_x"))
    queue.settle(row.msg_id, state=MsgState.DEAD, now=clock.now(), reason=DeadReason.MAX_LIFETIME)
    payload = message_payload(sources, row.msg_id, now=NOW)
    assert payload["header"]["dead_reason"] == DeadReason.MAX_LIFETIME.value
    assert DeadReason.MAX_LIFETIME.value in render_message(sources, row.msg_id, now=NOW)


def test_an_unknown_id_says_so_rather_than_rendering_an_empty_row(
    wired: tuple[DiagSources, SqliteQueueStore],
) -> None:
    sources, _ = wired
    assert "no row with this id" in render_message(sources, "01ABCDEF" + "0" * 18, now=NOW)


def test_a_view_with_no_queue_says_the_queue_was_not_consulted(
    wired: tuple[DiagSources, SqliteQueueStore],
) -> None:
    """ "Not consulted" and "empty" are different facts, and only one is honest.

    A caller that opened no queue store must not have the view report an empty
    queue as a finding about the queue.
    """
    sources, _ = wired
    without = DiagSources(events=sources.events, states=sources.states, findings=sources.findings)
    assert "not consulted" in render_message(without, "01ABCDEF" + "0" * 18, now=NOW)


def test_the_text_view_renders_only_from_the_payload(
    wired: tuple[DiagSources, SqliteQueueStore], clock: FakeClock
) -> None:
    """Two reads of a live database can return different rows.

    A text view that disagreed with its own ``--json`` would be worse than either
    alone, which is why both come from one builder.
    """
    sources, queue = wired
    row = queue.enqueue(EnqueueDraft(idempotency_key="k1", receiver_id="mb_super", sender_id="t-w"))
    text = render_message(sources, row.msg_id, now=NOW)
    assert "mb_super" in text
    assert "t-w" in text
    assert row.msg_id in text


def test_the_cli_routes_a_ulid_to_the_message_view_and_anything_else_to_terminal() -> None:
    """The discriminator is a property of the value, not a prefix convention.

    A ``msg_id`` is a ULID minted by ``core/ids.py``; a ``terminal_id`` is an
    opaque fork string and is not.  So ``cao diag <id>`` needs neither a flag nor
    a naming rule an operator has to remember.
    """
    from cli_agent_orchestrator.core.ids import new_ulid

    minted = new_ulid()
    assert is_ulid(minted)
    assert not is_ulid("cao-term-7f3a")
    assert not is_ulid("mb_supervisor")

    import click

    from cli_agent_orchestrator.cli.commands.diag import diag

    ctx = click.Context(diag)
    assert diag.resolve_command(ctx, [minted])[0] == "msg"
    assert diag.resolve_command(ctx, ["cao-term-7f3a"])[0] == "terminal"
    assert diag.resolve_command(ctx, ["findings"])[0] == "findings"
