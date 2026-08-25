"""fx168 hot-fix regression tests.

(a) POST insert to idle mailbox-pull supervisor arms the F136 runner
(b) Stale cc_inbox_path + fresh metadata path → runner reconciles
(c) Startup reconciler refreshes stale row when metadata differs
(d) Dead D9 doorbell call removed from deliver_pending
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.inbox_service import CallbackRunOutcome


_NOW = datetime(2026, 8, 13, 2, 27, 34, tzinfo=timezone.utc)
_TERMINAL_ID = "69200c40"
_MAILBOX_ID = "mb_d176ebe0"
_SENDER_ID = "612b7a5c"
_STALE_PATH = "~/.claude/teams/session-old/inboxes/team-lead.json"
_FRESH_PATH = "~/.claude/teams/session-new/inboxes/team-lead.json"


def _msg(msg_id: int = 5332, sender: str = _SENDER_ID) -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=sender,
        receiver_id=_TERMINAL_ID,
        message="callback result",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=_NOW,
    )


# ===========================================================================
# (a) FIX 1: POST insert arms F136 runner via request_delivery
# ===========================================================================


class TestFix1PostArmsF136:
    """POST /terminals/{id}/inbox/messages calls request_delivery after insert."""

    def test_request_delivery_called_after_create_inbox_message(self):
        """Verify the request_delivery call exists in the direct-terminal path.

        The endpoint has two branches: mb_* (mailbox) and direct terminal.
        FIX 1 adds request_delivery in the direct-terminal branch, after
        create_inbox_message and before deliver_pending.
        """
        import inspect
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        source = inspect.getsource(create_inbox_message_endpoint)
        # Find the fx168 FIX-1 comment — this marks our addition
        assert "fx168 FIX-1" in source, "FIX-1 comment missing from endpoint"
        # The request_delivery call should reference inbox_msg.receiver_id
        assert "request_delivery(inbox_msg.receiver_id)" in source, (
            "request_delivery(inbox_msg.receiver_id) missing from endpoint"
        )

    def test_request_delivery_wrapped_in_try_except(self):
        """request_delivery is wrapped in try/except to not break the endpoint."""
        import inspect
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        source = inspect.getsource(create_inbox_message_endpoint)
        # Find the request_delivery section — it should be inside a try block
        idx_request = source.find("request_delivery(")
        # Look backwards for "try:" before request_delivery
        section_before = source[:idx_request]
        last_try = section_before.rfind("try:")
        # And "except" after it
        section_after = source[idx_request:]
        first_except = section_after.find("except Exception")
        assert last_try > 0, "request_delivery should be in a try block"
        assert first_except > 0, "request_delivery should have an except clause"


# ===========================================================================
# (b) FIX 2: Stale cc_inbox_path → runner reconciles via _f136_post_delivery
# ===========================================================================


class TestFix2StalePathSelfHeal:
    """F136 runner detects stale cc_inbox_path and reconciles in post_delivery."""

    def test_stale_path_detected_returns_needs_immediate_wake(self):
        """When batch.inbox_path != terminal metadata path, outcome has stale_path_detected."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        service = InboxService.__new__(InboxService)
        service._tnf_lock = threading.Lock()
        service._terminal_not_found_streaks = {}

        @dataclass
        class FakeBatchRow:
            inbox_row_id: int = 5332
            sender_id: str = _SENDER_ID
            message: str = "done"
            created_at: datetime = _NOW

        @dataclass
        class FakeBatch:
            kind: str = "ok"
            rows: tuple = ()
            has_more: bool = False
            cursor: int = 5328
            inbox_path: str = _STALE_PATH
            path_version: int = 1
            bootstrap_mode: str | None = None
            reason: str = "ok"

        @dataclass
        class FakeMailboxInc:
            mailbox_id: str = _MAILBOX_ID
            terminal_id: str = _TERMINAL_ID

        @dataclass
        class FakeMailbox:
            id: str = _MAILBOX_ID
            generation: int = 56
            session_name: str = "cao-test"
            role: str = "supervisor"

        with (
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_dl,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock"
            ) as mock_al,
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_session,
            patch(
                "cli_agent_orchestrator.clients.database.get_supervisor_callback_batch"
            ) as mock_batch,
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata"
            ) as mock_meta,
        ):
            # Setup locks
            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_dl.return_value = mock_lock
            mock_al.return_value = mock_lock

            # Setup DB session for incarnation/mailbox lookup
            mock_db = MagicMock()
            mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter_by.return_value.one_or_none.side_effect = [
                FakeMailboxInc(),  # MailboxIncarnationModel query
                FakeMailbox(),  # MailboxModel query
            ]

            # Batch has stale path and one row
            mock_batch.return_value = FakeBatch(rows=(FakeBatchRow(),))

            # Terminal metadata has fresh path
            mock_meta.return_value = {
                "metadata": {"cc_team_inbox_path": _FRESH_PATH},
            }

            outcome = service._f136_run_callback_delivery(_TERMINAL_ID)

            assert outcome.reason == "stale_path_detected"
            assert outcome.needs_immediate_wake is True
            assert outcome._fx168_stale_heal is not None
            assert outcome._fx168_stale_heal == (_MAILBOX_ID, _TERMINAL_ID, 56, _FRESH_PATH)

    def test_post_delivery_calls_set_path_on_stale_heal(self):
        """_f136_post_delivery calls set_supervisor_callback_inbox_path with heal data."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        service = InboxService.__new__(InboxService)
        service._delivery_loop = None
        service._delivery_tasks = set()
        service._tnf_lock = threading.Lock()
        service._terminal_not_found_streaks = {}

        outcome = CallbackRunOutcome(
            needs_immediate_wake=True,
            reason="stale_path_detected",
            _fx168_stale_heal=(_MAILBOX_ID, _TERMINAL_ID, 56, _FRESH_PATH),
        )

        with patch(
            "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path"
        ) as mock_set_path:
            from cli_agent_orchestrator.services.mailbox_service import PathUpdateResult

            mock_set_path.return_value = PathUpdateResult(kind="updated", path_version=2)

            service._f136_post_delivery(_TERMINAL_ID, outcome)

            mock_set_path.assert_called_once_with(
                mailbox_id=_MAILBOX_ID,
                terminal_id=_TERMINAL_ID,
                generation=56,
                path=_FRESH_PATH,
            )

    def test_no_heal_when_paths_match(self):
        """When batch.inbox_path matches terminal metadata, no stale_path_detected."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        service = InboxService.__new__(InboxService)
        service._tnf_lock = threading.Lock()
        service._terminal_not_found_streaks = {}

        @dataclass
        class FakeBatchRow:
            inbox_row_id: int = 5332
            sender_id: str = _SENDER_ID
            message: str = "done"
            created_at: datetime = _NOW

        @dataclass
        class FakeBatch:
            kind: str = "ok"
            rows: tuple = ()
            has_more: bool = False
            cursor: int = 5328
            inbox_path: str = _FRESH_PATH
            path_version: int = 2
            bootstrap_mode: str | None = None
            reason: str = "ok"

        @dataclass
        class FakeMailboxInc:
            mailbox_id: str = _MAILBOX_ID
            terminal_id: str = _TERMINAL_ID

        @dataclass
        class FakeMailbox:
            id: str = _MAILBOX_ID
            generation: int = 56
            session_name: str = "cao-test"
            role: str = "supervisor"

        with (
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_dl,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock"
            ) as mock_al,
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_session,
            patch(
                "cli_agent_orchestrator.clients.database.get_supervisor_callback_batch"
            ) as mock_batch,
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.write_supervisor_callback_notification"
            ) as mock_write,
            patch(
                "cli_agent_orchestrator.clients.database.commit_supervisor_callback_progress"
            ) as mock_commit,
        ):
            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_dl.return_value = mock_lock
            mock_al.return_value = mock_lock

            mock_db = MagicMock()
            mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter_by.return_value.one_or_none.side_effect = [
                FakeMailboxInc(),
                FakeMailbox(),
            ]

            mock_batch.return_value = FakeBatch(rows=(FakeBatchRow(),))

            # Metadata path MATCHES batch path — no stale heal
            mock_meta.return_value = {
                "metadata": {"cc_team_inbox_path": _FRESH_PATH},
            }

            mock_write.return_value = MagicMock(kind="written")
            mock_commit.return_value = MagicMock(kind="advanced")

            outcome = service._f136_run_callback_delivery(_TERMINAL_ID)

            # Should proceed to normal write, not stale_path_detected
            assert outcome.reason != "stale_path_detected"
            assert outcome._fx168_stale_heal is None


# ===========================================================================
# (c) FIX 3: Startup reconciler refreshes stale row
# ===========================================================================


class TestFix3StartupReconciler:
    """Startup reconciler refreshes when metadata path differs from mailbox row."""

    @pytest.mark.asyncio
    async def test_reconciles_stale_path(self):
        """When cc_inbox_path differs from metadata, set_supervisor_callback_inbox_path is called."""
        @dataclass
        class FakeMailbox:
            current_terminal_id: str = _TERMINAL_ID
            id: str = _MAILBOX_ID
            generation: int = 56
            cc_inbox_path: str = _STALE_PATH

        with (
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_sl,
            patch("cli_agent_orchestrator.clients.database.get_terminal_metadata") as mock_meta,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path"
            ) as mock_set_path,
        ):
            mock_db = MagicMock()
            mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter.return_value.join.return_value.all.return_value = [
                FakeMailbox()
            ]

            mock_meta.return_value = {
                "metadata": {"cc_team_inbox_path": _FRESH_PATH},
            }

            from cli_agent_orchestrator.services.mailbox_service import PathUpdateResult
            mock_set_path.return_value = PathUpdateResult(kind="updated", path_version=2)

            from cli_agent_orchestrator.api.main import _f150_reconcile_supervisor_inbox_paths_at_startup
            await _f150_reconcile_supervisor_inbox_paths_at_startup()

            mock_set_path.assert_called_once_with(
                mailbox_id=_MAILBOX_ID,
                terminal_id=_TERMINAL_ID,
                generation=56,
                path=_FRESH_PATH,
            )

    @pytest.mark.asyncio
    async def test_skips_when_metadata_absent(self):
        """When terminal metadata has no cc_team_inbox_path, skip (no-op)."""
        @dataclass
        class FakeMailbox:
            current_terminal_id: str = _TERMINAL_ID
            id: str = _MAILBOX_ID
            generation: int = 56
            cc_inbox_path: str = _STALE_PATH

        with (
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_sl,
            patch("cli_agent_orchestrator.clients.database.get_terminal_metadata") as mock_meta,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path"
            ) as mock_set_path,
        ):
            mock_db = MagicMock()
            mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter.return_value.join.return_value.all.return_value = [
                FakeMailbox()
            ]

            # No cc_team_inbox_path in metadata
            mock_meta.return_value = {"metadata": {}}

            from cli_agent_orchestrator.api.main import _f150_reconcile_supervisor_inbox_paths_at_startup
            await _f150_reconcile_supervisor_inbox_paths_at_startup()

            mock_set_path.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_paths_already_match(self):
        """When cc_inbox_path already matches metadata, skip."""
        @dataclass
        class FakeMailbox:
            current_terminal_id: str = _TERMINAL_ID
            id: str = _MAILBOX_ID
            generation: int = 56
            cc_inbox_path: str = _FRESH_PATH

        with (
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_sl,
            patch("cli_agent_orchestrator.clients.database.get_terminal_metadata") as mock_meta,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path"
            ) as mock_set_path,
        ):
            mock_db = MagicMock()
            mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter.return_value.join.return_value.all.return_value = [
                FakeMailbox()
            ]

            # Metadata path matches — should skip
            mock_meta.return_value = {
                "metadata": {"cc_team_inbox_path": _FRESH_PATH},
            }

            from cli_agent_orchestrator.api.main import _f150_reconcile_supervisor_inbox_paths_at_startup
            await _f150_reconcile_supervisor_inbox_paths_at_startup()

            mock_set_path.assert_not_called()


# ===========================================================================
# (d) FIX 4: Dead D9 doorbell removed from deliver_pending
# ===========================================================================


class TestFix4DeadD9Removed:
    """deliver_pending's mailbox-pull branch no longer calls ring_supervisor_doorbell."""

    def test_deliver_pending_mailbox_pull_no_doorbell(self):
        """When is_supervisor_mailbox_pull_terminal=True, ring_supervisor_doorbell is NOT called."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        service = InboxService.__new__(InboxService)
        service._delivery_loop = MagicMock()
        service._delivery_tasks = set()
        service._tnf_lock = threading.Lock()
        service._terminal_not_found_streaks = {}

        with (
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_dl,
            patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages") as mock_pending,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal"
            ) as mock_pull,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push"
            ) as mock_should,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push"
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell"
            ) as mock_doorbell,
            patch(
                "cli_agent_orchestrator.services.inbox_service._delivery_wake_seq", {}
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
                MagicMock(),
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda k, default=None: True if k == "supervisor.wake.native" else default,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages_by_ids"
            ) as mock_recheck,
        ):
            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_dl.return_value = mock_lock

            mock_meta.return_value = {"recovery_state": None}
            mock_pending.return_value = [_msg()]
            mock_pull.return_value = True
            mock_should.return_value = True
            mock_push.return_value = True  # push succeeded
            mock_recheck.return_value = [_msg()]  # F457: rows still pending

            service.deliver_pending(_TERMINAL_ID)

            # Push was called
            mock_push.assert_called_once()
            # Doorbell was NOT called (removed in FIX 4)
            mock_doorbell.assert_not_called()
