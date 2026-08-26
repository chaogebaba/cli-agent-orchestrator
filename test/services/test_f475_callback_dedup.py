"""F475: atomic callback dedup — rolling 60s window, normalized content hash.

Tests:
  1. Distinct-content from same sender within window: both persist
  2. Identical content: deduped (sequential proves choke-point enforcement)
  3. Boundary-straddling duplicate (0.2s apart across minute edge): deduped
  4. Identical payloads with different attestation blocks: deduped
  5. mb_ path duplicate: deduped through the choke point
  6. Barrier-associated then identical ordinary: delivers
  7. park_warm row then identical ordinary: delivers
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import cli_agent_orchestrator.clients.database as _db_mod
from cli_agent_orchestrator.clients.database import (
    _F475_CALLBACK_DEDUP_WINDOW_S,
    InboxModel,
    _f475_compute_content_hash,
    _f475_normalize_message,
    _f475_should_dedup,
    create_inbox_message,
)
from cli_agent_orchestrator.models.inbox import OrchestrationType


def _get_sl():
    return _db_mod.SessionLocal


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite database with WAL mode."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    import cli_agent_orchestrator.clients.database as db_mod

    db_path = tmp_path / "test.db"
    test_engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(test_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    test_session_local = sessionmaker(bind=test_engine)

    monkeypatch.setattr(db_mod, "engine", test_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", test_session_local)

    db_mod.Base.metadata.create_all(bind=test_engine)

    from cli_agent_orchestrator.clients.database import TerminalModel

    with test_session_local() as db:
        db.add(
            TerminalModel(
                id="worker-1",
                tmux_session="s",
                tmux_window="w1",
                provider="mock",
                agent_profile="developer",
                caller_id="sup-1",
                lifecycle="ephemeral",
                init_state="ready",
            )
        )
        db.add(
            TerminalModel(
                id="sup-1",
                tmux_session="s",
                tmux_window="w0",
                provider="mock",
                agent_profile="supervisor",
                lifecycle="ephemeral",
                init_state="ready",
            )
        )
        db.commit()

    def _mock_meta(terminal_id):
        if terminal_id == "worker-1":
            return {
                "id": "worker-1",
                "caller_id": "sup-1",
                "caller_mailbox_id": "mb_sup1",
                "metadata": None,
            }
        return None

    monkeypatch.setattr(db_mod, "get_terminal_metadata", _mock_meta)

    @contextmanager
    def _noop_guard(sender_id):
        yield

    from cli_agent_orchestrator.services import stalled_callback_watchdog as wd_mod

    monkeypatch.setattr(wd_mod.stalled_callback_watchdog, "callback_insert_guard", _noop_guard)

    yield test_engine


class TestF475DistinctContent:
    """Two distinct messages from same sender within window both persist."""

    def test_distinct_content_both_persist(self):
        msg1 = create_inbox_message("worker-1", "sup-1", "phase one completed")
        msg2 = create_inbox_message("worker-1", "sup-1", "phase two completed")
        assert msg1.id != msg2.id

    def test_identical_content_deduped(self):
        msg1 = create_inbox_message("worker-1", "sup-1", "ORACLE READY")
        msg2 = create_inbox_message("worker-1", "sup-1", "ORACLE READY")
        assert msg1.id == msg2.id


class TestF475BoundaryStraddling:
    """Duplicates 0.2s apart straddling a minute edge are still deduped."""

    def test_boundary_straddling_deduped(self, monkeypatch):
        """Rolling window means no fixed-bucket gaps."""
        import datetime as dt

        # First message at T=59.8 (just before a minute boundary)
        t1 = dt.datetime(2026, 8, 25, 12, 0, 59, 800000, tzinfo=dt.timezone.utc)
        # Second at T=60.0 (just after — different epoch//60 bucket)
        t2 = dt.datetime(2026, 8, 25, 12, 1, 0, 0, tzinfo=dt.timezone.utc)

        call_count = [0]
        times = [t1, t2]

        def _mock_utcnow():
            idx = min(call_count[0], len(times) - 1)
            call_count[0] += 1
            return times[idx]

        monkeypatch.setattr(_db_mod, "_utcnow", _mock_utcnow)

        msg1 = create_inbox_message("worker-1", "sup-1", "BOUNDARY READY")
        msg2 = create_inbox_message("worker-1", "sup-1", "BOUNDARY READY")
        # Rolling 60s window: msg2 at T=60 should still see msg1 at T=59.8
        # because 60.0 - 59.8 = 0.2s < 60s window
        assert msg1.id == msg2.id


class TestF475AttestationNormalization:
    """Identical payloads with different attestation timestamps are deduped."""

    def test_different_attestation_timestamps_deduped(self):
        msg1_text = (
            "[FROZEN-PIN-ATTESTATION valid_at=2026-08-26T01:41:47Z pins=1]\n\n"
            "ORACLE READY — ingestion complete."
        )
        msg2_text = (
            "[FROZEN-PIN-ATTESTATION valid_at=2026-08-26T01:41:48Z pins=1]\n\n"
            "ORACLE READY — ingestion complete."
        )
        msg1 = create_inbox_message("worker-1", "sup-1", msg1_text)
        msg2 = create_inbox_message("worker-1", "sup-1", msg2_text)
        assert msg1.id == msg2.id

    def test_normalize_strips_attestation(self):
        raw = "[FROZEN-PIN-ATTESTATION valid_at=2026-08-26T01:41:47Z pins=1]\n\nREADY"
        assert _f475_normalize_message(raw) == "READY"

    def test_normalize_no_attestation_unchanged(self):
        raw = "ORACLE READY"
        assert _f475_normalize_message(raw) == "ORACLE READY"


class TestF475MailboxPath:
    """mb_ path duplicate is deduped through the choke point."""

    def test_mailbox_addressed_duplicate_deduped(self, monkeypatch):
        from cli_agent_orchestrator.clients.database import MailboxModel

        with _get_sl()() as db:
            db.add(
                MailboxModel(
                    id="mb_sup1",
                    session_name="s",
                    role="supervisor",
                    current_terminal_id="sup-1",
                    generation=1,
                    consumed_through_id=0,
                )
            )
            db.commit()

        msg1 = create_inbox_message("worker-1", "mb_sup1", "READY VIA MAILBOX")
        msg2 = create_inbox_message("worker-1", "mb_sup1", "READY VIA MAILBOX")
        assert msg1.id == msg2.id


class TestF475BarrierParkWarmIsolation:
    """Barrier/park_warm rows don't poison ordinary callbacks."""

    def test_park_warm_row_does_not_suppress_ordinary(self):
        msg_warm = create_inbox_message("worker-1", "sup-1", "READY", park_warm=True)
        msg_ordinary = create_inbox_message("worker-1", "sup-1", "READY")
        assert msg_warm.id != msg_ordinary.id

    def test_barrier_associated_then_identical_ordinary_delivers(self):
        """A real barrier-associated callback followed by identical ordinary: both persist.

        The barrier row gets barrier_id set (post-association), so its dedup_key
        is NULL. The subsequent ordinary callback gets a dedup_key but finds no
        matching non-barrier row → inserts.
        """
        # First: dispatch a barrier task from sup-1 to worker-1
        import datetime as dt

        from cli_agent_orchestrator.clients.database import (
            CallbackBarrierMemberModel,
            CallbackBarrierModel,
        )

        with _get_sl()() as db:
            barrier = CallbackBarrierModel(
                owner_terminal_id="sup-1",
                owner_generation=1,
                label="test-gate",
                timeout_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            )
            db.add(barrier)
            db.flush()
            member = CallbackBarrierMemberModel(
                barrier_id=barrier.id,
                member_key="worker-1",
                terminal_id="worker-1",
                state="AWAITING",
                position=0,
                lifecycle_generation=0,
            )
            db.add(member)
            db.commit()

        # Worker callback arrives → gets barrier-associated (barrier_id != NULL)
        msg_barrier = create_inbox_message("worker-1", "sup-1", "TASK DONE")
        with _get_sl()() as db:
            row = db.query(InboxModel).filter_by(id=msg_barrier.id).first()
            assert row.barrier_id is not None
            # Should have NULL dedup_key (barrier-bound)
            assert row.callback_dedup_key is None

        # Later identical ordinary callback (no barrier now) must deliver
        msg_ordinary = create_inbox_message("worker-1", "sup-1", "TASK DONE")
        assert msg_barrier.id != msg_ordinary.id


class TestF475Helpers:
    """Unit tests for helper functions."""

    def test_content_hash_differs_for_different_content(self):
        h1 = _f475_compute_content_hash("hello world")
        h2 = _f475_compute_content_hash("hello world!")
        assert h1 != h2

    def test_content_hash_same_for_same_content(self):
        h1 = _f475_compute_content_hash("ORACLE READY")
        h2 = _f475_compute_content_hash("ORACLE READY")
        assert h1 == h2

    def test_dedup_window_constant(self):
        assert _F475_CALLBACK_DEDUP_WINDOW_S == 60

    def test_should_dedup_false_for_assign(self):
        with patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"caller_id": "sup-1", "caller_mailbox_id": None},
        ):
            assert not _f475_should_dedup(
                "worker-1", "sup-1", OrchestrationType.ASSIGN, False, None
            )

    def test_should_dedup_true_for_callback_to_caller(self):
        with patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"caller_id": "sup-1", "caller_mailbox_id": None},
        ):
            assert _f475_should_dedup(
                "worker-1", "sup-1", OrchestrationType.SEND_MESSAGE, False, None
            )

    def test_should_dedup_true_for_callback_to_mailbox(self):
        with patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"caller_id": "sup-1", "caller_mailbox_id": "mb_sup1"},
        ):
            assert _f475_should_dedup(
                "worker-1", "mb_sup1", OrchestrationType.SEND_MESSAGE, False, None
            )
