"""F175 regression tests — teammate-push deterministic identity and file-level dedup.

F476 update: Client-side high-water dedup (last_notified_inbox_id) has been removed.
Server-side wake cursor (claim_unnotified_wake / commit_wake) replaces it.
Remaining tests validate:
3. Deterministic msg_id: _build_entry with mailbox context produces stable uuid5.
4. File-level dedup in _write_inbox_entry: duplicate msg_id appends are idempotent.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.teammate_push_service import (
    F136_CALLBACK_NAMESPACE,
    _build_entry,
    _write_inbox_entry,
    attempt_teammate_push_reported,
    callback_notification_id,
)

TERMINAL_ID = "f175test"
MAILBOX_ID = "mb-f175-test"


def _make_message(msg_id: int = 5360, sender_id: str = "kiro_dev-71f9d3d9") -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=sender_id,
        receiver_id=TERMINAL_ID,
        message="FX170-S2 completion callback: done.",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=datetime(2026, 8, 13, 6, 27, 0, tzinfo=timezone.utc),
        logical_receiver_id=MAILBOX_ID,
    )


@pytest.fixture
def inbox_dir(tmp_path):
    """Return a temp inbox dir + path."""
    inbox_path = tmp_path / "inboxes" / "team-lead.json"
    inbox_path.parent.mkdir(parents=True)
    return inbox_path


def _metadata_with_inbox(inbox_path: str) -> Dict[str, Any]:
    return {
        "id": TERMINAL_ID,
        "tmux_session": "cao-test",
        "tmux_window": "w0",
        "provider": "kiro_cli",
        "metadata": {"cc_team_inbox_path": inbox_path},
    }


# ---------------------------------------------------------------------------
# Test 1: File-level dedup — deterministic msg_id prevents duplicate appends
# ---------------------------------------------------------------------------


class TestFileLevelDedup:
    """File-level dedup via deterministic msg_id prevents duplicate entries."""

    def test_file_level_dedup_prevents_duplicates(self, inbox_dir):
        """Even with no in-memory state, deterministic msg_id prevents file duplicates."""
        msg = _make_message(msg_id=5360)
        meta = _metadata_with_inbox(str(inbox_dir))

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=meta,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
        ):
            # First push — writes to file
            o1 = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            assert o1.pushed is True

            # Second push — file-level dedup catches it via deterministic msg_id
            o2 = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            assert o2.pushed is True  # _write_inbox_entry returns True for already-present

            entries = json.loads(inbox_dir.read_text())
            assert len(entries) == 1, f"Expected 1 entry, got {len(entries)}"


# ---------------------------------------------------------------------------
# Test 2: Consumption cursor dedup
# ---------------------------------------------------------------------------


class TestConsumptionCursorDedup:
    """Messages below consumption cursor are filtered out."""

    def test_consumed_messages_not_pushed(self, inbox_dir):
        """Messages with id <= consumption cursor are not pushed."""
        msg = _make_message(msg_id=5360)
        meta = _metadata_with_inbox(str(inbox_dir))

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=meta,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=6000,  # above the message id
            ),
        ):
            outcome = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            assert outcome.pushed is False
            assert outcome.reason == "consumed"


# ---------------------------------------------------------------------------
# Test 3: Deterministic msg_id
# ---------------------------------------------------------------------------


class TestDeterministicMsgId:
    """_build_entry produces stable uuid5 when mailbox context is provided."""

    def test_same_inputs_same_msg_id(self):
        e1 = _build_entry("worker-01", "hello", 1, mailbox_id="mb-1", first_row_id=100)
        e2 = _build_entry("worker-01", "hello", 1, mailbox_id="mb-1", first_row_id=100)
        assert e1["msg_id"] == e2["msg_id"]
        # Must be the deterministic uuid5
        assert e1["msg_id"] == callback_notification_id("mb-1", 100)

    def test_different_row_different_msg_id(self):
        e1 = _build_entry("worker-01", "hello", 1, mailbox_id="mb-1", first_row_id=100)
        e2 = _build_entry("worker-01", "hello", 1, mailbox_id="mb-1", first_row_id=101)
        assert e1["msg_id"] != e2["msg_id"]

    def test_no_mailbox_context_falls_back_to_uuid4(self):
        e1 = _build_entry("worker-01", "hello", 1)
        e2 = _build_entry("worker-01", "hello", 1)
        # Without mailbox context, each call produces a unique uuid4
        assert e1["msg_id"] != e2["msg_id"]


# ---------------------------------------------------------------------------
# Test 4: File-level dedup in _write_inbox_entry
# ---------------------------------------------------------------------------


class TestFileLevelDedup:
    """_write_inbox_entry rejects entries with duplicate msg_id."""

    def test_duplicate_msg_id_not_appended(self, inbox_dir):
        entry = _build_entry("w1", "test", 1, mailbox_id="mb-x", first_row_id=42)

        # First write
        assert _write_inbox_entry(inbox_dir, entry) is True
        entries = json.loads(inbox_dir.read_text())
        assert len(entries) == 1

        # Second write with same msg_id — idempotent success, no duplicate
        assert _write_inbox_entry(inbox_dir, entry) is True
        entries = json.loads(inbox_dir.read_text())
        assert len(entries) == 1

    def test_different_msg_id_appended(self, inbox_dir):
        e1 = _build_entry("w1", "test", 1, mailbox_id="mb-x", first_row_id=42)
        e2 = _build_entry("w1", "test", 1, mailbox_id="mb-x", first_row_id=43)

        assert _write_inbox_entry(inbox_dir, e1) is True
        assert _write_inbox_entry(inbox_dir, e2) is True
        entries = json.loads(inbox_dir.read_text())
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Test 5: Doorbell dedup column isolation
# ---------------------------------------------------------------------------


class TestDoorbellColumnIsolation:
    """F476: Doorbell dedup is now server-side; stubs use in-memory dict only."""

    def test_persist_and_read_doorbell_row_id(self):
        """Stubs use in-memory dict (no DB interaction per F476 D8)."""
        from cli_agent_orchestrator.services.doorbell_service import (
            _get_last_doorbell_row_id,
            _last_doorbell_row_id,
            _persist_last_doorbell_row_id,
        )

        tid = "doorbell-test-01"
        _last_doorbell_row_id.pop(tid, None)

        assert _get_last_doorbell_row_id(tid) == 0
        _persist_last_doorbell_row_id(tid, 999)
        assert _last_doorbell_row_id[tid] == 999

        _last_doorbell_row_id.pop(tid, None)
