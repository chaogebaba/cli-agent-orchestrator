"""F175 regression tests — teammate-push append storm and metadata clobber.

Proves:
1. Storm scenario: N sweeps with the same unconsumed row produce exactly 1 inbox
   file entry (not N duplicates).
2. Metadata-clobber scenario: supervisor whole-dict replace of terminal metadata
   between sweeps does NOT reset the dedup high-water (no re-append).
3. Deterministic msg_id: _build_entry with mailbox context produces stable uuid5.
4. File-level dedup in _write_inbox_entry: duplicate msg_id appends are idempotent.

All tests MUST FAIL when the fix is reverted.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch, MagicMock

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.teammate_push_service import (
    F136_CALLBACK_NAMESPACE,
    _build_entry,
    _last_notified,
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


@pytest.fixture(autouse=True)
def _clean_last_notified():
    """Clear in-memory dedup dict before each test."""
    _last_notified.pop(TERMINAL_ID, None)
    yield
    _last_notified.pop(TERMINAL_ID, None)


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
# Test 1: Storm scenario — N sweeps, 1 unconsumed row → exactly 1 entry
# ---------------------------------------------------------------------------


class TestStormScenario:
    """Supervisor busy, N sweep invocations for the same unconsumed row."""

    def test_n_sweeps_produce_exactly_one_entry(self, inbox_dir):
        """Simulate 30 reconciler sweeps while the supervisor is busy.

        After fix: exactly 1 inbox file entry (first sweep writes, rest dedup).
        Before fix: 30 entries (uuid4 msg_id + metadata-clobbered high-water).
        """
        msg = _make_message(msg_id=5360)
        meta = _metadata_with_inbox(str(inbox_dir))

        # Patch DB accessors — simulate no prior notification
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=meta,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_last_notified_inbox_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ) as mock_set,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,  # not consumed yet
            ),
        ):
            outcomes = []
            for sweep in range(30):
                outcome = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
                outcomes.append(outcome)

            # First sweep should push
            assert outcomes[0].pushed is True
            assert outcomes[0].reason == "pushed"

            # All subsequent should be deduped (already_notified via in-memory fallback)
            for i, oc in enumerate(outcomes[1:], 1):
                assert oc.pushed is False, f"sweep {i} should not push"
                assert oc.reason == "already_notified"

            # Inbox file has exactly 1 entry
            entries = json.loads(inbox_dir.read_text())
            assert len(entries) == 1

    def test_file_level_dedup_prevents_duplicates_even_without_highwater(self, inbox_dir):
        """Even if in-memory dict is cleared between sweeps (simulating restart),
        the file-level msg_id dedup prevents duplicate appends.
        """
        msg = _make_message(msg_id=5360)
        meta = _metadata_with_inbox(str(inbox_dir))

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=meta,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_last_notified_inbox_id",
                return_value=0,  # Always returns 0 (simulates DB clobber)
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
        ):
            # First push — writes to file
            _last_notified.pop(TERMINAL_ID, None)
            o1 = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            assert o1.pushed is True

            # Simulate loss of in-memory state (restart)
            _last_notified.pop(TERMINAL_ID, None)

            # Second push — file-level dedup catches it via deterministic msg_id
            o2 = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            # Should still "succeed" (idempotent) but file has only 1 entry
            assert o2.pushed is True  # _write_inbox_entry returns True for already-present

            entries = json.loads(inbox_dir.read_text())
            assert len(entries) == 1, f"Expected 1 entry, got {len(entries)}"


# ---------------------------------------------------------------------------
# Test 2: Metadata clobber scenario — supervisor replace between sweeps
# ---------------------------------------------------------------------------


class TestMetadataClobberScenario:
    """Supervisor update_metadata (whole-dict replace) cannot reset dedup."""

    def test_supervisor_replace_does_not_reset_highwater(self, inbox_dir):
        """After first push writes to dedicated column, supervisor's
        update_terminal_metadata(id, {cc_team_inbox_path: ..., task: ...})
        does NOT erase last_notified_inbox_id because it lives in a separate column.
        """
        msg = _make_message(msg_id=5360)
        meta = _metadata_with_inbox(str(inbox_dir))

        # Track the stored value
        stored_notified = [0]

        def mock_get_notified(tid):
            return stored_notified[0]

        def mock_set_notified(tid, val):
            stored_notified[0] = val
            return True

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=meta,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_last_notified_inbox_id",
                side_effect=mock_get_notified,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
                side_effect=mock_set_notified,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
        ):
            # First sweep pushes
            _last_notified.pop(TERMINAL_ID, None)
            o1 = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            assert o1.pushed is True
            assert stored_notified[0] == 5360

            # Simulate supervisor clobber: in the old code this would have
            # reset metadata["last_notified_inbox_id"] to None. With dedicated
            # column, stored_notified stays at 5360.
            # (We don't touch stored_notified — that's the point)

            # Second sweep — should be deduped
            _last_notified.pop(TERMINAL_ID, None)
            o2 = attempt_teammate_push_reported(TERMINAL_ID, [msg], mailbox_id=MAILBOX_ID)
            assert o2.pushed is False
            assert o2.reason == "already_notified"

            entries = json.loads(inbox_dir.read_text())
            assert len(entries) == 1


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
    """Doorbell high-water stored in dedicated column, not metadata_json."""

    def test_persist_and_read_doorbell_row_id(self):
        """Accessors use dedicated DB column path."""
        from cli_agent_orchestrator.services.doorbell_service import (
            _get_last_doorbell_row_id,
            _last_doorbell_row_id,
            _persist_last_doorbell_row_id,
        )

        tid = "doorbell-test-01"
        _last_doorbell_row_id.pop(tid, None)

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_last_doorbell_row_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.set_terminal_last_doorbell_row_id",
            ) as mock_set,
        ):
            assert _get_last_doorbell_row_id(tid) == 0
            _persist_last_doorbell_row_id(tid, 999)
            mock_set.assert_called_once_with(tid, 999)
            assert _last_doorbell_row_id[tid] == 999

        _last_doorbell_row_id.pop(tid, None)
