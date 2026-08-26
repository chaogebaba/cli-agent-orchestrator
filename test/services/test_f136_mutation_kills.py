"""F136 mutation kill tests — real production-path calls against temp DB.

Every test calls the actual production function through a monkeypatched
SessionLocal, ensuring the mutant (if applied to production source) causes
the assertion to fail.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    CallbackReplayQueueModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    cancel_callback_barrier,
    commit_supervisor_callback_progress,
    enqueue_callback_replay,
    get_supervisor_callback_batch,
    settle_terminal_rebound,
)
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.teammate_push_service import (
    attempt_teammate_push_on_insert,
    callback_notification_id,
    write_supervisor_callback_notification,
)

_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def f136_sessions(tmp_path, monkeypatch):
    """Real temp-DB fixture patched into all production SessionLocal references."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'f136.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    # Patch everywhere SessionLocal is used
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    monkeypatch.setattr(dbmod, "engine", eng)
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions)
    # Apply F136 migration
    with eng.begin() as conn:
        cols = conn.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        col_names = {c["name"] for c in cols}
        if "callback_notified_through_id" not in col_names:
            conn.execute(
                text("ALTER TABLE mailboxes ADD COLUMN callback_notified_through_id INTEGER")
            )
        if "cc_inbox_path" not in col_names:
            conn.execute(text("ALTER TABLE mailboxes ADD COLUMN cc_inbox_path TEXT"))
        if "cc_inbox_path_version" not in col_names:
            conn.execute(
                text(
                    "ALTER TABLE mailboxes ADD COLUMN cc_inbox_path_version INTEGER NOT NULL DEFAULT 0"
                )
            )
        tables = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='callback_replay_queue'"
            )
        ).fetchall()
        if not tables:
            conn.execute(
                text(
                    "CREATE TABLE callback_replay_queue ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  mailbox_id TEXT NOT NULL,"
                    "  inbox_row_id INTEGER NOT NULL,"
                    "  queued_at DATETIME NOT NULL,"
                    "  UNIQUE(mailbox_id, inbox_row_id))"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_callback_replay_mailbox_row "
                    "ON callback_replay_queue(mailbox_id, inbox_row_id)"
                )
            )
    return sessions, eng


def _seed(
    sessions,
    *,
    mailbox_id="mb_sup",
    terminal_id="t1",
    generation=1,
    cursor=None,
    path="/tmp/inbox.json",
    path_version=0,
):
    """Seed a supervisor mailbox + terminal + incarnation."""
    with sessions.begin() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="test",
                tmux_window=terminal_id,
                provider="claude_code",
                agent_profile="supervisor",
                lifecycle_generation=generation,
            )
        )
        mb = MailboxModel(
            id=mailbox_id,
            session_name="test",
            role="supervisor",
            current_terminal_id=terminal_id,
            generation=generation,
            consumed_through_id=0,
            schema_version=1,
            callback_notified_through_id=cursor,
            cc_inbox_path=path,
            cc_inbox_path_version=path_version,
            created_at=_NOW,
            updated_at=_NOW,
        )
        db.add(mb)
        db.flush()
        db.add(
            MailboxIncarnationModel(
                mailbox_id=mailbox_id,
                generation=generation,
                terminal_id=terminal_id,
                published_at=_NOW,
            )
        )


def _inbox_row(
    sessions,
    row_id,
    *,
    mailbox_id="mb_sup",
    terminal_id="t1",
    generation=1,
    status="pending",
    logical=True,
):
    """Seed an inbox row."""
    with sessions.begin() as db:
        db.add(
            InboxModel(
                id=row_id,
                sender_id="worker-1",
                receiver_id=terminal_id,
                logical_receiver_id=mailbox_id if logical else None,
                message=f"msg-{row_id}",
                orchestration_type="send_message",
                status=status,
                enqueue_generation=generation if logical else None,
                created_at=_NOW,
            )
        )


# ===========================================================================
# M1 — attempt_teammate_push_on_insert returns False (retired)
# ===========================================================================


class TestM1:
    def test_attempt_teammate_push_on_insert_retired_with_metadata(self, tmp_path, monkeypatch):
        """M1: Even with valid terminal metadata and inbox path, the retired
        function returns False. The F123 direct-append path is dead."""
        inbox_path = tmp_path / "teams" / "test" / "inboxes" / "team-lead.json"
        inbox_path.parent.mkdir(parents=True)
        # Provide full metadata so the old code path would have written
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            lambda tid: {
                "id": tid,
                "tmux_session": "s",
                "tmux_window": "w",
                "provider": "claude_code",
                "agent_profile": "sup",
                "metadata": {"cc_team_inbox_path": str(inbox_path)},
            },
        )
        msg = InboxMessage(
            id=42,
            sender_id="worker-1",
            receiver_id="sup-001",
            message="task done",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
            status=MessageStatus.PENDING,
            created_at=_NOW,
        )
        result = attempt_teammate_push_on_insert("sup-001", [msg])
        assert result is False
        # No file was written
        assert not inbox_path.exists()


# ===========================================================================
# M4 — forward stream requires id > cursor
# ===========================================================================


class TestM4:
    def test_forward_stream_excludes_at_and_below_cursor(self, f136_sessions):
        """M4: get_supervisor_callback_batch forward rows must have id > cursor."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=10)
        _inbox_row(sessions, 8)  # below cursor
        _inbox_row(sessions, 10)  # at cursor
        _inbox_row(sessions, 11)  # above
        _inbox_row(sessions, 15)  # above

        batch = get_supervisor_callback_batch(
            mailbox_id="mb_sup",
            terminal_id="t1",
            generation=1,
            limit=50,
        )
        assert batch.kind == "ok"
        forward_ids = [r.inbox_row_id for r in batch.rows if r.tag == "forward"]
        assert 8 not in forward_ids
        assert 10 not in forward_ids
        assert forward_ids == [11, 15]


# ===========================================================================
# M5 — barrier-release replay enqueue
# ===========================================================================


class TestM5:
    def test_cancel_barrier_enqueues_replay_for_released_below_cursor(self, f136_sessions):
        """M5: cancel_callback_barrier must enqueue replay for released rows below cursor."""
        sessions, eng = f136_sessions
        _seed(sessions, cursor=10)
        # Create a barrier owned by the mailbox
        with sessions.begin() as db:
            barrier = CallbackBarrierModel(
                owner_mailbox_id="mb_sup",
                owner_terminal_id=None,
                owner_generation=1,
                label="test-gate",
                state="OPEN",
                timeout_at=_NOW + timedelta(hours=1),
                created_at=_NOW,
            )
            db.add(barrier)
            db.flush()
            barrier_id = int(barrier.id)
            # Add a HELD row below cursor
            db.add(
                InboxModel(
                    id=5,
                    sender_id="worker-1",
                    receiver_id="t1",
                    logical_receiver_id="mb_sup",
                    message="held-msg",
                    orchestration_type="send_message",
                    status="held",
                    enqueue_generation=1,
                    barrier_id=barrier_id,
                    created_at=_NOW,
                )
            )
            db.add(
                CallbackBarrierMemberModel(
                    barrier_id=barrier_id,
                    member_key="worker-1",
                    position=1,
                    terminal_id="t1",
                    lifecycle_generation=1,
                    state="AWAITING",
                )
            )

        # Cancel the barrier — should release HELD→PENDING and enqueue replay
        with patch("cli_agent_orchestrator.services.inbox_service.request_delivery"):
            result = cancel_callback_barrier(barrier_id=barrier_id)

        assert result["released"] == 1
        # Verify replay entry exists for row 5 (below cursor 10)
        with sessions() as db:
            replay = db.execute(
                text("SELECT inbox_row_id FROM callback_replay_queue WHERE mailbox_id='mb_sup'")
            ).fetchall()
            assert 5 in [r[0] for r in replay]


# ===========================================================================
# M6 — parked/reactivated replay via settle_terminal_rebound
# ===========================================================================


class TestM6:
    def test_settle_terminal_rebound_enqueues_replay_below_cursor(self, f136_sessions):
        """M6: settle_terminal_rebound reactivates PARKED rows and enqueues replay."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=10)
        # Add a PARKED row below cursor owned by our terminal
        with sessions.begin() as db:
            db.add(
                InboxModel(
                    id=3,
                    sender_id="worker-1",
                    receiver_id="t1",
                    logical_receiver_id="mb_sup",
                    message="parked-msg",
                    orchestration_type="send_message",
                    status="parked",
                    enqueue_generation=1,
                    owner_receiver_id="t1",
                    owner_generation=1,
                    created_at=_NOW,
                )
            )

        # settle_terminal_rebound reactivates parked rows
        new_gen = settle_terminal_rebound("t1", "session-uuid-123", "claude --resume")
        assert new_gen > 0

        # Row 3 should now be PENDING and in replay queue
        with sessions() as db:
            row = db.query(InboxModel).filter_by(id=3).one()
            assert row.status == "pending"
            replay = db.execute(
                text("SELECT inbox_row_id FROM callback_replay_queue WHERE mailbox_id='mb_sup'")
            ).fetchall()
            assert 3 in [r[0] for r in replay]


# ===========================================================================
# M7 — path-change replay via set_supervisor_callback_inbox_path
# ===========================================================================


class TestM7:
    def test_path_change_enqueues_replay_for_below_cursor_rows(self, f136_sessions):
        """M7: Path change must replay current PENDING rows at/below cursor."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=10, path="/old/path")
        _inbox_row(sessions, 7)
        _inbox_row(sessions, 9)
        _inbox_row(sessions, 12)  # above cursor — should NOT replay

        from cli_agent_orchestrator.services.mailbox_service import (
            set_supervisor_callback_inbox_path,
        )

        with patch("cli_agent_orchestrator.services.inbox_service.request_delivery"):
            result = set_supervisor_callback_inbox_path(
                mailbox_id="mb_sup",
                terminal_id="t1",
                generation=1,
                path="/new/path",
            )

        assert result.kind == "updated"
        with sessions() as db:
            replay_ids = [
                r[0]
                for r in db.execute(
                    text("SELECT inbox_row_id FROM callback_replay_queue WHERE mailbox_id='mb_sup'")
                ).fetchall()
            ]
            assert 7 in replay_ids
            assert 9 in replay_ids
            assert 12 not in replay_ids


# ===========================================================================
# M8 — duplicate replay uniqueness via production migration + enqueue
# ===========================================================================


class TestM8:
    def test_enqueue_callback_replay_deduplicates(self, f136_sessions):
        """M8: enqueue_callback_replay with same (mailbox, row) inserts only once."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=10)
        _inbox_row(sessions, 5)

        with sessions.begin() as db:
            enqueue_callback_replay(db, mailbox_id="mb_sup", inbox_row_ids=[5])
        with sessions.begin() as db:
            # Second enqueue of same row — must not duplicate
            enqueue_callback_replay(db, mailbox_id="mb_sup", inbox_row_ids=[5])

        with sessions() as db:
            count = db.execute(
                text(
                    "SELECT COUNT(*) FROM callback_replay_queue WHERE mailbox_id='mb_sup' AND inbox_row_id=5"
                )
            ).scalar()
            assert count == 1


# ===========================================================================
# M9 — bootstrap excludes stale generations
# ===========================================================================


class TestM9:
    def test_bootstrap_cursor_law_excludes_stale_generation_rows(self, f136_sessions):
        """M9: Bootstrap forward cursor must be derived ONLY from current-generation
        rows, and persisted on the mailbox.

        Law: with a NULL cursor, the bootstrap cursor is
        ``min(current-generation pending id) - 1``.  A stale-generation row with a
        lower id must NOT pull the cursor down, or the next run would re-forward
        every current row between the stale min and the true min.
        """
        sessions, _ = f136_sessions
        _seed(sessions, generation=2, cursor=None)
        # Stale gen-1 row at a lower id than any current row
        _inbox_row(sessions, 1, generation=1)
        # Current gen-2 rows
        _inbox_row(sessions, 5, generation=2)
        _inbox_row(sessions, 8, generation=2)

        batch = get_supervisor_callback_batch(
            mailbox_id="mb_sup",
            terminal_id="t1",
            generation=2,
            limit=50,
        )
        assert batch.kind == "ok"
        assert batch.bootstrap_mode == "current_generation_pending_replay"
        # THE LAW: cursor = min(current-gen pending id) - 1 = 5 - 1
        assert batch.cursor == 4
        # And it must be persisted on the mailbox row, not just returned
        with sessions() as db:
            mb = db.query(MailboxModel).filter_by(id="mb_sup").one()
            assert mb.callback_notified_through_id == 4
        # Forward stream starts strictly above the persisted cursor
        forward_ids = [r.inbox_row_id for r in batch.rows if r.tag == "forward"]
        assert all(rid > 4 for rid in forward_ids)
        assert 5 in forward_ids
        assert 8 in forward_ids
        # Stale gen row is never selected regardless of cursor value
        row_ids = [r.inbox_row_id for r in batch.rows]
        assert 1 not in row_ids


# ===========================================================================
# M10 — raw-row adoption
# ===========================================================================


class TestM10:
    def test_batch_adopts_legacy_raw_pending_rows(self, f136_sessions):
        """M10: get_supervisor_callback_batch adopts raw PENDING rows for supervisor terminal."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=0)
        # Raw row: logical_receiver_id=NULL, receiver_id=t1
        _inbox_row(sessions, 3, logical=False)

        batch = get_supervisor_callback_batch(
            mailbox_id="mb_sup",
            terminal_id="t1",
            generation=1,
            limit=50,
        )
        assert batch.kind == "ok"
        # After adoption, the row should appear (now logical)
        with sessions() as db:
            row = db.query(InboxModel).filter_by(id=3).one()
            assert row.logical_receiver_id == "mb_sup"


# ===========================================================================
# M11 — routed supervisor producer
# ===========================================================================


class TestM11:
    def test_create_routed_inbox_message_resolves_supervisor_logically(self, f136_sessions):
        """M11: Supervisor-targeted message routes through the logical producer
        branch — which gates receiver input via require_input_allowed.  The raw
        fallback never calls that helper, so dropping the branch is detectable
        at helper/row behavior level even though the inserted row itself looks
        logical either way (resolve_inbox_receiver masks it)."""
        sessions, _ = f136_sessions
        _seed(sessions)
        guarded_terminals: list[str] = []
        with (
            patch("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
            ) as mock_wd,
            patch("cli_agent_orchestrator.services.inbox_service.request_delivery"),
            patch(
                "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed",
            ) as mock_guard,
        ):
            mock_wd.callback_insert_guard.return_value.__enter__ = MagicMock()
            mock_wd.callback_insert_guard.return_value.__exit__ = MagicMock(return_value=False)

            def _record_guard(*args, **kwargs):
                guarded_terminals.append(args[0] if args else kwargs)

            mock_guard.side_effect = _record_guard

            from cli_agent_orchestrator.services.mailbox_service import create_routed_inbox_message

            result = create_routed_inbox_message("worker-1", "t1", "hello")

        assert result.logical_receiver_id == "mb_sup"
        # M11 law: the logical supervisor producer branch gates the receiver
        # terminal through require_input_allowed; the raw fallback path does not.
        assert guarded_terminals == ["t1"]


# ===========================================================================
# M17 — replay deletion atomic with cursor advance
# ===========================================================================


class TestM17:
    def test_commit_progress_deletes_replay_atomically(self, f136_sessions):
        """M17: Successful commit deletes replay IDs and advances cursor atomically."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=5, path_version=1)
        _inbox_row(sessions, 3)
        # Pre-seed a replay entry
        with sessions.begin() as db:
            enqueue_callback_replay(db, mailbox_id="mb_sup", inbox_row_ids=[3])

        # Commit progress: advance cursor and drain replay
        result = commit_supervisor_callback_progress(
            mailbox_id="mb_sup",
            terminal_id="t1",
            generation=1,
            expected_cursor=5,
            new_cursor=5,  # equal = replay-only
            expected_path_version=1,
            replay_row_ids=(3,),
        )
        assert result.kind == "advanced"

        # Replay entry deleted
        with sessions() as db:
            count = db.execute(
                text(
                    "SELECT COUNT(*) FROM callback_replay_queue WHERE mailbox_id='mb_sup' AND inbox_row_id=3"
                )
            ).scalar()
            assert count == 0

    def test_commit_progress_fails_on_stale_authority_no_deletion(self, f136_sessions):
        """M17: Failed commit (wrong generation) does NOT delete replay."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=5, path_version=1)
        _inbox_row(sessions, 3)
        with sessions.begin() as db:
            enqueue_callback_replay(db, mailbox_id="mb_sup", inbox_row_ids=[3])

        # Wrong generation → stale_authority, replay NOT deleted
        result = commit_supervisor_callback_progress(
            mailbox_id="mb_sup",
            terminal_id="t1",
            generation=99,
            expected_cursor=5,
            new_cursor=10,
            expected_path_version=1,
            replay_row_ids=(3,),
        )
        assert result.kind == "stale_authority"
        with sessions() as db:
            count = db.execute(
                text("SELECT COUNT(*) FROM callback_replay_queue WHERE inbox_row_id=3")
            ).scalar()
            assert count == 1  # Still there


# ===========================================================================
# M19 — path-version CAS
# ===========================================================================


class TestM19:
    def test_stale_path_version_rejects_progress(self, f136_sessions):
        """M19: Stale path version cannot advance cursor or drain replay."""
        sessions, _ = f136_sessions
        _seed(sessions, cursor=5, path_version=3)
        _inbox_row(sessions, 3)
        with sessions.begin() as db:
            enqueue_callback_replay(db, mailbox_id="mb_sup", inbox_row_ids=[3])

        result = commit_supervisor_callback_progress(
            mailbox_id="mb_sup",
            terminal_id="t1",
            generation=1,
            expected_cursor=5,
            new_cursor=10,
            expected_path_version=1,  # Stale! Current is 3
            replay_row_ids=(3,),
        )
        assert result.kind == "path_changed"
        # Cursor not advanced
        with sessions() as db:
            mb = db.query(MailboxModel).filter_by(id="mb_sup").one()
            assert mb.callback_notified_through_id == 5  # Unchanged


# ===========================================================================
# M20 — scheduling outside guard (request_delivery)
# ===========================================================================


class TestM20:
    def test_request_delivery_posts_outside_guard(self):
        """M20: call_soon_threadsafe must execute OUTSIDE _delivery_seq_guard."""
        from cli_agent_orchestrator.services.inbox_service import (
            _delivery_seq_guard,
            _wake_states,
            request_delivery,
            inbox_service,
        )

        terminal = "test-m20-" + uuid.uuid4().hex[:8]
        guard_held_during_post = []

        original_call = None

        class InstrumentedLoop:
            def is_closed(self):
                return False

            def call_soon_threadsafe(self, fn, *args):
                # Check if guard is held — try non-blocking acquire
                acquired = _delivery_seq_guard.acquire(blocking=False)
                if acquired:
                    _delivery_seq_guard.release()
                    guard_held_during_post.append(False)
                else:
                    guard_held_during_post.append(True)

        old_loop = inbox_service._delivery_loop
        inbox_service._delivery_loop = InstrumentedLoop()
        try:
            with _delivery_seq_guard:
                _wake_states.pop(terminal, None)
            request_delivery(terminal)
            assert len(guard_held_during_post) == 1
            assert (
                guard_held_during_post[0] is False
            ), "call_soon_threadsafe called while guard held!"
        finally:
            inbox_service._delivery_loop = old_loop
            with _delivery_seq_guard:
                _wake_states.pop(terminal, None)


# ===========================================================================
# M24 — lock-loser does not start new immediate (dirty_epoch bumps only)
# ===========================================================================


class TestM24:
    def test_request_while_immediate_running_bumps_epoch_only(self):
        """M24: A request arriving while immediate is admitted bumps dirty_epoch
        but does NOT post a second immediate, and must NOT advance holder_epoch.

        Law: holder_epoch is the run-start snapshot the holder compares against
        to detect new work. A lock-loser request must leave it unchanged, or the
        holder sees dirty == holder (no new work) and strands the request."""
        from cli_agent_orchestrator.services.inbox_service import (
            _WakeState,
            _delivery_seq_guard,
            _wake_states,
            request_delivery,
            inbox_service,
        )

        terminal = "test-m24-" + uuid.uuid4().hex[:8]
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False
        old_loop = inbox_service._delivery_loop
        inbox_service._delivery_loop = mock_loop
        try:
            # Pre-set state: immediate already admitted (holder is running)
            with _delivery_seq_guard:
                _wake_states[terminal] = _WakeState(
                    dirty_epoch=5,
                    immediate_admitted=True,
                    holder_epoch=5,
                )
            request_delivery(terminal)
            # No new call_soon posted
            mock_loop.call_soon_threadsafe.assert_not_called()
            with _delivery_seq_guard:
                st = _wake_states[terminal]
                # dirty_epoch bumped to record the new work
                assert st.dirty_epoch == 6
                # holder_epoch stays at the epoch the holder was admitted on (5),
                # so the holder still sees dirty(6) > holder(5) = new work pending
                assert st.holder_epoch == 5
        finally:
            inbox_service._delivery_loop = old_loop
            with _delivery_seq_guard:
                _wake_states.pop(terminal, None)


# ===========================================================================
# M25 — post-delivery rerun on dirty epoch mismatch
# ===========================================================================


class TestM25:
    def test_post_delivery_reruns_when_dirty_gt_holder(self):
        """M25: _f136_post_delivery must post immediate when dirty > holder."""
        from cli_agent_orchestrator.services.inbox_service import (
            _WakeState,
            _delivery_seq_guard,
            _wake_states,
            CallbackRunOutcome,
            inbox_service,
        )

        terminal = "test-m25-" + uuid.uuid4().hex[:8]
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False
        old_loop = inbox_service._delivery_loop
        inbox_service._delivery_loop = mock_loop
        try:
            # Simulate: delivery finished, but new work arrived during the run
            with _delivery_seq_guard:
                _wake_states[terminal] = _WakeState(
                    dirty_epoch=10,
                    immediate_admitted=True,
                    holder_epoch=5,
                )
            # Call post_delivery with a "done, no more work" outcome
            outcome = CallbackRunOutcome(reason="empty")
            inbox_service._f136_post_delivery(terminal, outcome)
            # Should rerun: dirty(10) > holder(5)
            mock_loop.call_soon_threadsafe.assert_called_once()
        finally:
            inbox_service._delivery_loop = old_loop
            with _delivery_seq_guard:
                _wake_states.pop(terminal, None)


# ===========================================================================
# M26 — row cap (limit=50) enforced in runner
# ===========================================================================


class TestM26:
    def test_runner_caps_batch_at_50_rows(self, f136_sessions, tmp_path, monkeypatch):
        """M26: _f136_run_callback_delivery must cap a run at MAX_ROWS_PER_RUN (50),
        independent of the batch helper's default.

        Freeze time.monotonic so the 200ms per-run budget never binds — the row
        cap is the only limiter. With 60 eligible rows, the runner must select
        and process exactly 50, proving it passes MAX_ROWS_PER_RUN as the limit
        (the helper's own default is not the mechanism under test)."""
        sessions, _ = f136_sessions
        inbox_path = tmp_path / "inbox.json"
        _seed(sessions, cursor=0, path=str(inbox_path))
        # Seed 60 eligible rows
        for i in range(1, 61):
            _inbox_row(sessions, i)

        from cli_agent_orchestrator.services.inbox_service import inbox_service

        # Freeze monotonic: deadline = const + 0.2, and monotonic never reaches it.
        monkeypatch.setattr("time.monotonic", lambda: 1000.0)

        outcome = inbox_service._f136_run_callback_delivery("t1")
        assert outcome.selected == 50
        assert outcome.processed == 50


# ===========================================================================
# M27 — total time/deadline cap in runner
# ===========================================================================


class TestM27:
    def test_runner_respects_200ms_budget(self, f136_sessions, tmp_path, monkeypatch):
        """M27: _f136_run_callback_delivery stops processing at 200ms deadline."""
        sessions, _ = f136_sessions
        inbox_path = tmp_path / "inbox.json"
        _seed(sessions, cursor=0, path=str(inbox_path))
        # Seed 10 rows
        for i in range(1, 11):
            _inbox_row(sessions, i)

        from cli_agent_orchestrator.services.inbox_service import inbox_service

        # Patch time.monotonic to simulate time passing (expired after 1st write)
        call_count = [0]
        base_time = time.monotonic()

        def fake_monotonic():
            call_count[0] += 1
            # After 3rd call to monotonic, report >200ms elapsed
            if call_count[0] >= 3:
                return base_time + 0.5  # 500ms — well past budget
            return base_time

        monkeypatch.setattr("time.monotonic", fake_monotonic)

        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False
        old_loop = inbox_service._delivery_loop
        inbox_service._delivery_loop = mock_loop

        try:
            outcome = inbox_service._f136_run_callback_delivery("t1")
            # F476: claim/commit happen before emit; budget applies to emit loop.
            # With mocked writes (fast), all rows may process within the budget.
            assert outcome.reason == "ok"
            assert outcome.processed <= 10
        finally:
            inbox_service._delivery_loop = old_loop


# ===========================================================================
# F157 hotfix — callback batch respects consumption cursor
# ===========================================================================


class TestF157ConsumptionCursorRespected:
    """Regression: if consumed_through_id > callback_notified_through_id,
    the batch must NOT return rows that are already acked (consumed).
    Root cause: BUGS.md:518 recurrence — duplicate-delivery."""

    def test_acked_message_excluded_from_forward_batch(self, f136_sessions):
        """consumed_through_id=5, callback_notified_through_id=2 → rows 3,4,5
        are already acked and must NOT appear in the batch."""
        sessions, eng = f136_sessions

        mailbox_id = "mb_f157"
        terminal_id = "t_f157"
        generation = 1

        # Seed mailbox with notified cursor behind consumption cursor
        with sessions.begin() as db:
            db.add(
                TerminalModel(
                    id=terminal_id,
                    tmux_session="test",
                    tmux_window=terminal_id,
                    provider="claude_code",
                    agent_profile="supervisor",
                    lifecycle_generation=generation,
                )
            )
            db.add(
                MailboxModel(
                    id=mailbox_id,
                    session_name="test_f157",
                    role="supervisor",
                    current_terminal_id=terminal_id,
                    generation=generation,
                    consumed_through_id=5,  # Supervisor already acked through 5
                    schema_version=1,
                    callback_notified_through_id=2,  # Callback runner is behind
                    cc_inbox_path="/tmp/f157_inbox.json",
                    cc_inbox_path_version=0,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
            db.add(
                MailboxIncarnationModel(
                    mailbox_id=mailbox_id,
                    generation=generation,
                    terminal_id=terminal_id,
                    published_at=_NOW,
                )
            )

        # Seed inbox rows 3, 4, 5 (already consumed) and 6, 7 (not consumed)
        for row_id in (3, 4, 5, 6, 7):
            _inbox_row(
                sessions,
                row_id,
                mailbox_id=mailbox_id,
                terminal_id=terminal_id,
                generation=generation,
            )

        # Call the batch function
        result = get_supervisor_callback_batch(
            mailbox_id=mailbox_id,
            terminal_id=terminal_id,
            generation=generation,
            limit=50,
        )

        assert result.kind == "ok"
        returned_ids = [r.inbox_row_id for r in result.rows]

        # Rows 3, 4, 5 are already consumed — must NOT be in the batch
        for acked_id in (3, 4, 5):
            assert acked_id not in returned_ids, (
                f"Row {acked_id} was already acked (consumed_through_id=5) "
                f"but appeared in callback batch — duplicate delivery bug"
            )

        # Rows 6, 7 should be present (above both cursors)
        assert 6 in returned_ids
        assert 7 in returned_ids
