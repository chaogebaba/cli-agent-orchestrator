"""fx168 hot-fix regression tests.

(a) POST insert to idle mailbox-pull supervisor arms the F136 runner
(b) Stale cc_inbox_path + fresh metadata path → runner reconciles
(c) Startup reconciler refreshes stale row when metadata differs

(d) Dead D9 doorbell call removed from deliver_pending — deleted in WP-ARCH 3c
    with the branch it watched; see the block at the foot of the file.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

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
        assert (
            "request_delivery(inbox_msg.receiver_id)" in source
        ), "request_delivery(inbox_msg.receiver_id) missing from endpoint"

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
            tag: str = "forward"

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
            cc_inbox_path: str | None = _STALE_PATH

        with (
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_dl,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock"
            ) as mock_al,
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_session,
            patch("cli_agent_orchestrator.clients.database.claim_unnotified_wake") as mock_claim,
            patch("cli_agent_orchestrator.clients.database.commit_wake") as mock_commit,
            patch("cli_agent_orchestrator.clients.database.get_terminal_metadata") as mock_meta,
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

            # F476: claim returns rows with the stale inbox_path
            from cli_agent_orchestrator.clients.database import WakeClaimResult, WakeCommitResult

            mock_claim.return_value = WakeClaimResult(
                kind="claimed",
                rows=(FakeBatchRow(),),
                claimed_high_water=5328,
                path_version=1,
                reason="ok",
            )
            mock_commit.return_value = WakeCommitResult(
                kind="committed",
                reason="ok",
            )

            # Terminal metadata has fresh path (different from mailbox cc_inbox_path)
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
        """When mailbox cc_inbox_path matches terminal metadata, no stale_path_detected.

        F476 contract: claim → commit → stale-path check → emit. When the
        mailbox path matches metadata, the runner proceeds to write normally.
        """
        from cli_agent_orchestrator.clients.database import WakeClaimResult, WakeCommitResult
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
            tag: str = "forward"

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
            cc_inbox_path: str | None = _FRESH_PATH

        with (
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_dl,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock"
            ) as mock_al,
            patch("cli_agent_orchestrator.clients.database.SessionLocal") as mock_session,
            patch("cli_agent_orchestrator.clients.database.claim_unnotified_wake") as mock_claim,
            patch("cli_agent_orchestrator.clients.database.commit_wake") as mock_commit,
            patch("cli_agent_orchestrator.clients.database.get_terminal_metadata") as mock_meta,
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

            # F476: claim returns one row
            mock_claim.return_value = WakeClaimResult(
                kind="claimed",
                rows=(FakeBatchRow(),),
                claimed_high_water=5328,
                path_version=2,
                reason="ok",
            )
            mock_commit.return_value = WakeCommitResult(
                kind="committed",
                reason="ok",
            )

            # Metadata path MATCHES mailbox cc_inbox_path — no stale heal
            mock_meta.return_value = {
                "metadata": {"cc_team_inbox_path": _FRESH_PATH},
            }

            outcome = service._f136_run_callback_delivery(_TERMINAL_ID)

            # Should proceed past the stale check, not stale_path_detected.
            # WP-ARCH 3c K2 deleted the writer this arm used to patch; ``written``
            # is now the cursor's own count of wake-eligible rows (the runner
            # emits nothing), so the count below still reads 1 and still fails if
            # the claim/commit protocol regresses.
            assert outcome.reason == "ok"
            assert outcome._fx168_stale_heal is None
            assert outcome.written == 1
            mock_claim.assert_called_once()
            mock_commit.assert_called_once()


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

            from cli_agent_orchestrator.api.main import (
                _f150_reconcile_supervisor_inbox_paths_at_startup,
            )

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

            from cli_agent_orchestrator.api.main import (
                _f150_reconcile_supervisor_inbox_paths_at_startup,
            )

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

            from cli_agent_orchestrator.api.main import (
                _f150_reconcile_supervisor_inbox_paths_at_startup,
            )

            await _f150_reconcile_supervisor_inbox_paths_at_startup()

            mock_set_path.assert_not_called()


# ===========================================================================
# (d) FIX 4 — WP-ARCH 3c K2/K3/K8: deleted, and the behaviour it pinned is gone
# ===========================================================================
#
# ``TestFix4DeadD9Removed`` drove ``deliver_pending`` with the role probe
# answering "supervisor" and asserted three things about that branch: it signals
# ``request_delivery``, it does NOT call ``attempt_teammate_push`` (the bypass
# F476 r3 closed), and it does NOT ring the doorbell (the dead D9 call fx168
# removed).
#
# Two of the three are now unwritable — ``teammate_push_service`` and
# ``doorbell_service`` are deleted, so neither patch target imports. The THIRD
# is not merely unwritable but FALSE: 3c removed the ``request_delivery`` arming
# from that branch on purpose. The comment at the call site states the reason —
# the arming existed to wake the F136 runner so it would emit, the runner emits
# nothing now that K2's writer is gone, and the seat's wake belongs to the
# delivery tick, which observes the durable rows on its own schedule. The branch
# returns unconditionally for a supervisor-role receiver and arms nothing.
#
# So this is a retirement, not a relocation, and keeping the arm by dropping its
# two dead patches would have left a green test asserting a call the slice
# deliberately deleted. What the branch DOES guarantee now — the seat is never
# pasted, whatever the switch position — is asserted in
# ``test/app/delivery/test_adoption.py`` against the role probe directly, and
# the row it leaves PENDING is picked up by the adoption arms in the same file.
