"""All that is left of the delivery ladder: one DB predicate (WP-ARCH 3c K7).

``test_delivery_service.py`` was F427's canonical roster for a 2022-line module
holding §5c's obligation ladder. Slice 3 reduces that module to
``is_target_confirmed_dead``, so this file reduces with it.

**What went, and why the arms went with it.** Every deleted arm tested a rung,
an obligation transition, an escalation or a sweep — behaviour the queue now owns
through a lease and an attempt budget, with each re-offer a durable row rather
than a decision replayed from memory. Their successor is not another unit file:
it is ``test/app/delivery/`` against the real store.

**What stays here is the one thing that was never part of the ladder.**
``is_target_confirmed_dead`` reads a tombstone. Its caller is
``services/conversation_reconcile.py``, which has nothing to do with delivery,
and the arms below are the ones that were about the predicate rather than about
what the ladder did with its answer.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    PaneExitTombstoneModel,
    TerminalModel,
)
from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        yield session
    engine.dispose()


def _terminal(db, terminal_id: str) -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="cao-k7",
            tmux_window=terminal_id,
            provider="claude_code",
            lifecycle_generation=1,
        )
    )
    db.commit()


def _tombstone(db, terminal_id: str) -> None:
    """The minimum a tombstone row needs; the predicate reads only terminal_id."""
    now = datetime.now(timezone.utc)
    db.add(
        PaneExitTombstoneModel(
            id=f"tomb-{terminal_id}",
            incarnation_id=f"inc-{terminal_id}",
            terminal_id=terminal_id,
            terminal_generation=1,
            session_name="cao-k7",
            session_incarnation="sess-1",
            scope="window_gone",
            proc_status="not_applicable",
            exit_evidence_status="unavailable_no_waiter",
            memory_status="not_applicable",
            writer="observation",
            schema_version=1,
            complete=True,
            observed_at=now,
            written_at=now,
        )
    )
    db.commit()


def test_a_terminal_with_no_tombstone_is_not_confirmed_dead(db) -> None:
    """The default answer, and the one that matters for safety.

    ``confirmed_dead`` gates settlement, so a FALSE POSITIVE settles a live
    terminal's messages. A terminal nobody has entombed must therefore read as
    alive, whatever else is unknown about it.
    """
    _terminal(db, "t-alive")

    assert is_target_confirmed_dead("t-alive", db) is False


def test_a_terminal_with_a_tombstone_is_confirmed_dead(db) -> None:
    _terminal(db, "t-dead")
    _tombstone(db, "t-dead")

    assert is_target_confirmed_dead("t-dead", db) is True


def test_an_unknown_terminal_is_not_confirmed_dead(db) -> None:
    """Unknown is not dead.

    D6 is a DB-ONLY check with no tmux call, so "no row anywhere" means the
    predicate has no evidence — and answering ``True`` on no evidence is how a
    live receiver's messages get settled out from under it.
    """
    assert is_target_confirmed_dead("t-never-existed", db) is False


def test_a_tombstone_for_another_terminal_does_not_leak(db) -> None:
    """The filter is on the terminal id.

    Cheap to get wrong and expensive to have wrong: a predicate that answered
    from ANY tombstone would report every terminal dead the moment one pane
    exited.
    """
    _terminal(db, "t-one")
    _terminal(db, "t-two")
    _tombstone(db, "t-one")

    assert is_target_confirmed_dead("t-one", db) is True
    assert is_target_confirmed_dead("t-two", db) is False


def test_the_session_is_required(db) -> None:
    """S2: ``db`` has no default, so no caller can open its own session by accident.

    The predicate is called from inside a transaction that already holds the
    rows; a defaulted session would open a SECOND connection to the same SQLite
    file and take a lock against its own caller.
    """
    import inspect

    signature = inspect.signature(is_target_confirmed_dead)
    assert signature.parameters["db"].default is inspect.Parameter.empty


def test_the_module_is_only_this(db) -> None:
    """K7's reduction, as a fact a regression would have to change.

    A future edit that re-grows this module — restoring a rung, an obligation
    helper, a sweep — is re-creating the second escalation authority the phase
    removed. Pinning the export list is the cheapest way to make that edit
    announce itself.
    """
    from cli_agent_orchestrator.services import delivery_service

    assert delivery_service.__all__ == ["is_target_confirmed_dead"]
    public = [n for n in vars(delivery_service) if not n.startswith("_")]
    assert set(public) >= {"is_target_confirmed_dead"}
    assert "convergence_tick" not in public
    assert "attempt_rung1" not in public
    assert "attempt_rung2" not in public
