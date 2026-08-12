"""fx157 AC7–AC9: send-time recount in attempt_teammate_push."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.teammate_push_service import (
    _last_notified,
    attempt_teammate_push,
)

_NOW = datetime(2025, 1, 1, 0, 0, 0)


def _make_message(msg_id: int = 1, sender_id: str = "worker-01", message: str = "done") -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=sender_id,
        receiver_id="sup-001",
        message=message,
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=_NOW,
    )


def _meta_with_path(inbox_path: str) -> dict:
    return {
        "id": "sup-001",
        "tmux_session": "cao-test",
        "tmux_window": "sup-001",
        "provider": "codex",
        "agent_profile": "code_supervisor",
        "metadata": {"cc_team_inbox_path": inbox_path},
        "last_active": None,
    }


class TestAC7ConsumedRowSuppressed:
    """attempt_teammate_push returns False for a fully-consumed batch."""

    def test_all_below_cursor_suppressed(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [_make_message(msg_id=3), _make_message(msg_id=5)]
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.update_terminal_metadata"
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=10,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            result = attempt_teammate_push("sup-001", messages)
        assert result is False
        assert not inbox_path.exists()

    def test_last_notified_not_advanced(self, tmp_path: Path) -> None:
        """last_notified_inbox_id must not advance on suppressed batch."""
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [_make_message(msg_id=3)]
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.update_terminal_metadata"
            ) as mock_update,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=5,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            attempt_teammate_push("sup-001", messages)
        mock_update.assert_not_called()


class TestAC8MixedBatchFiltered:
    """Mixed batch: only above-cursor messages are counted in the entry."""

    def test_mixed_batch_filtered_count(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [
            _make_message(msg_id=2),
            _make_message(msg_id=4),
            _make_message(msg_id=7, sender_id="kiro_dev", message="result"),
            _make_message(msg_id=9, sender_id="kiro_dev", message="more"),
        ]
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.update_terminal_metadata"
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=5,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            result = attempt_teammate_push("sup-001", messages)
        assert result is True
        entries = json.loads(inbox_path.read_text())
        assert len(entries) == 1
        # Only 2 messages above cursor (ids 7 and 9)
        assert "2 message(s) ready" in entries[0]["text"]


class TestAC9FailOpenNoMailbox:
    """No mailbox for the terminal → accessor returns None, legacy behavior."""

    def test_no_mailbox_falls_through(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "inbox.json"
        _last_notified.clear()
        messages = [_make_message(msg_id=1)]
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.update_terminal_metadata"
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=None,
            ),
        ):
            mock_meta.return_value = _meta_with_path(str(inbox_path))
            result = attempt_teammate_push("sup-001", messages)
        assert result is True
        assert inbox_path.exists()

    def test_accessor_exception_returns_none(self) -> None:
        """The accessor returns None (not raises) when DB is unavailable."""
        from cli_agent_orchestrator.clients.database import get_mailbox_consumption_cursor

        with patch(
            "cli_agent_orchestrator.clients.database.SessionLocal",
            side_effect=Exception("no db"),
        ):
            result = get_mailbox_consumption_cursor("no-such-terminal")
        assert result is None
