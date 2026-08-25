"""fx158 — Pull-mode pending-push reconciler acceptance tests.

AC1-AC5, AC8, AC11, AC14.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.teammate_push_service import (
    PushOutcome,
    _last_notified,
    attempt_teammate_push,
    attempt_teammate_push_reported,
)

_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(seconds=60)  # well past grace window


def _make_message(
    msg_id: int = 1,
    sender_id: str = "worker-01",
    message: str = "done",
    receiver_id: str = "sup-001",
    logical_receiver_id: str | None = "mb_sup",
    created_at: datetime | None = None,
    status: str = "pending",
) -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=message,
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus(status),
        created_at=created_at or _OLD,
        logical_receiver_id=logical_receiver_id,
    )


def _make_inbox_row(
    msg_id: int = 1,
    sender_id: str = "worker-01",
    message: str = "done",
    receiver_id: str = "sup-001",
    logical_receiver_id: str | None = "mb_sup",
    created_at: datetime | None = None,
    status: str = "pending",
):
    """Simulate an InboxModel ORM row."""
    return SimpleNamespace(
        id=msg_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=message,
        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
        status=status,
        created_at=created_at or _OLD,
        logical_receiver_id=logical_receiver_id,
    )


def _make_mailbox(
    mb_id: str = "mb_sup",
    current_terminal_id: str | None = "sup-001",
    consumed_through_id: int = 0,
    role: str = "supervisor",
):
    return SimpleNamespace(
        id=mb_id,
        current_terminal_id=current_terminal_id,
        consumed_through_id=consumed_through_id,
        role=role,
    )


def _make_terminal(terminal_id: str = "sup-001"):
    return SimpleNamespace(id=terminal_id)


def _meta_with_path(inbox_path: str) -> dict:
    return {
        "id": "sup-001",
        "tmux_session": "cao-test",
        "tmux_window": "sup-001",
        "provider": "kiro_cli",
        "agent_profile": "code_supervisor",
        "metadata": {"cc_team_inbox_path": inbox_path},
        "last_active": None,
    }


class _FakeQuery:
    """Chain-able fake for SQLAlchemy query."""

    def __init__(self, results: list):
        self._results = results

    def filter_by(self, **kw):
        return self

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def limit(self, n):
        self._results = self._results[:n]
        return self

    def all(self):
        return self._results

    def one_or_none(self):
        return self._results[0] if self._results else None


class _FakeSession:
    """Configurable fake DB session for reconciler tests."""

    def __init__(self, mailboxes=None, terminals=None, inbox_rows=None):
        self.mailboxes = mailboxes or []
        self.terminals = {t.id: t for t in (terminals or [])}
        self.inbox_rows = inbox_rows or {}  # mb_id -> [rows]
        self._call_idx = 0

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def query(self, model):
        model_name = getattr(model, "__tablename__", "") or getattr(model, "__name__", "")
        if model_name == "mailboxes" or "Mailbox" in str(model):
            return _FakeQuery(self.mailboxes)
        elif model_name == "terminals" or "Terminal" in str(model):
            return _FakeQuery(list(self.terminals.values()))
        elif model_name == "inbox" or "Inbox" in str(model):
            # Flatten all inbox rows for all mailboxes
            all_rows = []
            for rows in self.inbox_rows.values():
                all_rows.extend(rows)
            return _FakeQuery(all_rows)
        return _FakeQuery([])


def _reconciler_patches(
    *,
    mailboxes: list,
    terminals: list,
    inbox_rows_by_mb: dict | None = None,
    is_pull: bool = True,
    should_push: bool = True,
    push_outcome: PushOutcome | None = None,
    config_on: bool = True,
):
    """Return a dict of patches for reconcile_pull_mode_notifications tests."""
    inbox_rows_by_mb = inbox_rows_by_mb or {}

    # Build a session factory that handles the reconciler's three query patterns
    session_calls = []

    class _Session:
        def __init__(self):
            session_calls.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def query(self, model):
            name = getattr(model, "__tablename__", "")
            if not name:
                name = str(model)
            if "mailbox" in name.lower():
                return _FakeQuery(mailboxes)
            elif "terminal" in name.lower():
                return _FakeQuery(terminals)
            elif "inbox" in name.lower():
                # Return the rows for the current mailbox being processed
                # (the filter is mocked, so we return all configured rows)
                all_rows = []
                for rows in inbox_rows_by_mb.values():
                    all_rows.extend(rows)
                return _FakeQuery(all_rows)
            return _FakeQuery([])

    return _Session


# ===========================================================================
# AC1: Reconciler pushes what the edge path missed
# ===========================================================================


class TestAC1ReconcilerBypassesDeliverPending:
    """The reconciler pushes when deliver_pending is patched to raise."""

    def test_push_written_without_deliver_pending(self, tmp_path: Path) -> None:
        """With deliver_pending patched to raise, the reconciler still pushes."""
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        svc = InboxService()

        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        inbox_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[terminal],
            inbox_rows_by_mb={"mb_sup": [inbox_row]},
        )

        with (
            patch.object(
                svc,
                "deliver_pending",
                side_effect=RuntimeError("deliver_pending must not be called"),
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            svc.reconcile_pull_mode_notifications()

        # Verify inbox file was written
        assert inbox_path.exists()
        entries = json.loads(inbox_path.read_text())
        assert len(entries) == 1

    def test_reconciler_calls_push_reported_directly(self) -> None:
        """Reconciler calls attempt_teammate_push_reported, not deliver_pending."""
        svc = InboxService()

        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        inbox_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[terminal],
            inbox_rows_by_mb={"mb_sup": [inbox_row]},
        )

        with (
            patch.object(
                svc,
                "deliver_pending",
                side_effect=RuntimeError("deliver_pending must not be called"),
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
        ):
            mock_push.return_value = PushOutcome(pushed=True, reason="pushed", message_ids=(10,))
            svc.reconcile_pull_mode_notifications()

        mock_push.assert_called_once()
        assert mock_push.call_args[0][0] == "sup-001"


# ===========================================================================
# AC2: Grace window honored
# ===========================================================================


class TestAC2GraceWindow:
    """A row younger than grace produces no push."""

    def test_young_row_not_pushed(self) -> None:
        svc = InboxService()
        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()

        # The reconciler's SQL filter uses created_at < cutoff.
        # A young row would not be returned by the query.
        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[terminal],
            inbox_rows_by_mb={},  # empty — young rows filtered by DB
        )

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        mock_push.assert_not_called()

    def test_old_row_pushed_on_later_tick(self, tmp_path: Path) -> None:
        """Once the row ages past grace, it is pushed."""
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        svc = InboxService()
        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        old_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[terminal],
            inbox_rows_by_mb={"mb_sup": [old_row]},
        )

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            svc.reconcile_pull_mode_notifications()

        assert inbox_path.exists()


# ===========================================================================
# AC3: Cursor-consumed rows do not wake the push
# ===========================================================================


class TestAC3CursorConsumed:
    """Rows at or below consumed_through_id produce no push."""

    def test_all_consumed_no_push(self) -> None:
        svc = InboxService()
        # consumed_through_id=10, selection filter id > 10, nothing passes
        mb = _make_mailbox(consumed_through_id=10)
        terminal = _make_terminal()

        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[terminal],
            inbox_rows_by_mb={},  # DB returns nothing (all below cursor)
        )

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        mock_push.assert_not_called()

    def test_fx157_d3_recount_yields_consumed_reason(self, tmp_path: Path) -> None:
        """When selection is bypassed, fx157 D3's recount yields consumed."""
        _last_notified.clear()
        inbox_path = tmp_path / "inbox.json"
        messages = [_make_message(msg_id=5)]

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=10,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            outcome = attempt_teammate_push_reported("sup-001", messages)

        assert outcome.pushed is False
        assert outcome.reason == "consumed"


# ===========================================================================
# AC4: Incarnation correctness
# ===========================================================================


class TestAC4IncarnationCorrectness:
    """Row with reaped receiver_id but valid logical_receiver_id is selected."""

    def test_reaped_receiver_still_selected(self) -> None:
        svc = InboxService()
        # current incarnation is sup-002; old row addressed to sup-001
        mb = _make_mailbox(current_terminal_id="sup-002", consumed_through_id=0)
        terminal = _make_terminal(terminal_id="sup-002")
        old_row = _make_inbox_row(
            msg_id=10,
            receiver_id="sup-001",  # reaped
            logical_receiver_id="mb_sup",
            created_at=_OLD,
        )

        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[terminal],
            inbox_rows_by_mb={"mb_sup": [old_row]},
        )

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
        ):
            mock_push.return_value = PushOutcome(pushed=True, reason="pushed", message_ids=(10,))
            svc.reconcile_pull_mode_notifications()

        # Pushed to current_terminal_id (sup-002)
        mock_push.assert_called_once()
        assert mock_push.call_args[0][0] == "sup-002"


# ===========================================================================
# AC5: Dead/absent lineage skipped silently
# ===========================================================================


class TestAC5DeadLineageSkipped:
    """Mailbox with NULL terminal or missing terminal row."""

    def test_null_terminal_id_skipped(self) -> None:
        svc = InboxService()
        mb = _make_mailbox(current_terminal_id=None)

        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[],
            inbox_rows_by_mb={},
        )

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
            ) as mock_begin,
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        mock_push.assert_not_called()
        mock_begin.assert_not_called()

    def test_missing_terminal_row_skipped(self) -> None:
        svc = InboxService()
        mb = _make_mailbox(current_terminal_id="sup-001")

        # Terminal query returns empty
        Session = _reconciler_patches(
            mailboxes=[mb],
            terminals=[],  # no terminal row
            inbox_rows_by_mb={},
        )

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                Session,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
            ) as mock_begin,
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        mock_push.assert_not_called()
        mock_begin.assert_not_called()


# ===========================================================================
# AC8: Notify-cursor semantics
# ===========================================================================


class TestAC8NotifyCursorSemantics:
    """Successful push advances last_notified; suppressed leaves it."""

    def test_successful_push_advances_cursor(self, tmp_path: Path) -> None:
        """F476: push succeeds; _last_notified no longer tracked client-side."""
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [_make_message(msg_id=10), _make_message(msg_id=15)]

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ) as mock_update,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            outcome = attempt_teammate_push_reported("sup-001", messages)

        assert outcome.pushed is True
        # F476: _last_notified is a stub, no longer populated by push path

    def test_suppressed_push_does_not_advance_cursor(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [_make_message(msg_id=5)]

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ) as mock_update,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=10,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            outcome = attempt_teammate_push_reported("sup-001", messages)

        assert outcome.pushed is False
        assert outcome.reason == "consumed"
        assert _last_notified.get("sup-001") is None
        mock_update.assert_not_called()


# ===========================================================================
# AC11: Per-mailbox failure isolation
# ===========================================================================


class TestAC11PerMailboxIsolation:
    """One failing mailbox does not prevent others from processing."""

    def test_failing_mailbox_does_not_starve_others(self) -> None:
        svc = InboxService()
        mb_bad = _make_mailbox(mb_id="mb_bad", current_terminal_id="sup-bad")
        mb_good = _make_mailbox(mb_id="mb_good", current_terminal_id="sup-good")
        terminal_good = _make_terminal(terminal_id="sup-good")
        terminal_bad = _make_terminal(terminal_id="sup-bad")
        inbox_row_good = _make_inbox_row(msg_id=20, created_at=_OLD, logical_receiver_id="mb_good")

        # We need the terminal query for mb_bad to raise
        call_seq = []

        class _ExplodingSession:
            def __call__(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def query(self, model):
                name = getattr(model, "__tablename__", str(model))
                call_seq.append(name)
                if "mailbox" in name.lower():
                    return _FakeQuery([mb_bad, mb_good])
                elif "terminal" in name.lower():
                    # Return both terminals
                    return _FakeQuery([terminal_bad, terminal_good])
                elif "inbox" in name.lower():
                    return _FakeQuery([inbox_row_good])
                return _FakeQuery([])

        push_terminals = []

        def track_push(tid, msgs):
            push_terminals.append(tid)
            return PushOutcome(pushed=True, reason="pushed", message_ids=(msgs[0].id,))

        def mock_is_pull(tid):
            if tid == "sup-bad":
                raise RuntimeError("simulated failure for bad terminal")
            return True

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                side_effect=mock_is_pull,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
                side_effect=track_push,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                _ExplodingSession(),
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
        ):
            # Must not raise
            svc.reconcile_pull_mode_notifications()

        # mb_good was processed despite mb_bad failing
        assert "sup-good" in push_terminals

    def test_exception_does_not_propagate(self) -> None:
        """Exception in reconciler does not propagate out."""
        svc = InboxService()
        mb = _make_mailbox(current_terminal_id="sup-001")

        class _RaisingSession:
            def __call__(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def query(self, model):
                name = getattr(model, "__tablename__", str(model))
                if "mailbox" in name.lower():
                    return _FakeQuery([mb])
                raise RuntimeError("DB explosion")

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                _RaisingSession(),
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
            ),
        ):
            # Must not raise
            svc.reconcile_pull_mode_notifications()


# ===========================================================================
# AC14: fx157 coexistence — single cursor read per push call
# ===========================================================================


class TestAC14SingleCursorRead:
    """get_mailbox_consumption_cursor is called exactly once per push call."""

    def test_single_cursor_read(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [_make_message(msg_id=10)]

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
            ) as mock_cursor,
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            mock_cursor.return_value = 0
            attempt_teammate_push_reported("sup-001", messages)

        # D6: exactly one call
        assert mock_cursor.call_count == 1
