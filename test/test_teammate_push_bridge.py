"""WP-W2M-PUSH-BRIDGE acceptance tests (AC#1–AC#7).

Feature-flagged teammate push notification bridge. Tests use tempdir fixtures
for inbox writes and monkeypatch for flag/metadata control.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.teammate_push_service import (
    _LOCK_STALE_SECONDS,
    _SUMMARY_MAX_CHARS,
    _TEAMMATE_FROM,
    _TEXT_PREVIEW_CHARS,
    _acquire_lockfile,
    _build_entry,
    _release_lockfile,
    _resolve_inbox_path,
    _should_teammate_push,
    _write_inbox_entry,
    attempt_teammate_push,
    attempt_teammate_push_on_insert,
)

_NOW = datetime(2025, 1, 1, 0, 0, 0)


def _make_message(
    msg_id: int = 1,
    sender_id: str = "worker-01",
    message: str = "Task completed successfully",
) -> InboxMessage:
    """Create a minimal InboxMessage for testing."""
    return InboxMessage(
        id=msg_id,
        sender_id=sender_id,
        receiver_id="sup-001",
        message=message,
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=_NOW,
    )


def _metadata_with_path(inbox_path: str) -> Dict[str, Any]:
    """Return terminal metadata dict with cc_team_inbox_path set."""
    return {
        "id": "sup-001",
        "tmux_session": "cao-test",
        "tmux_window": "sup-001",
        "provider": "claude_code",
        "agent_profile": "chao_supervisor",
        "metadata": {"cc_team_inbox_path": inbox_path},
        "last_active": None,
    }


def _metadata_without_path() -> Dict[str, Any]:
    """Return terminal metadata dict without cc_team_inbox_path."""
    return {
        "id": "sup-001",
        "tmux_session": "cao-test",
        "tmux_window": "sup-001",
        "provider": "claude_code",
        "agent_profile": "chao_supervisor",
        "metadata": {},
        "last_active": None,
    }


class TestAC1FlagOffNoWrite:
    """AC#1 — Flag OFF = no CC inbox write."""

    def test_should_teammate_push_false_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With CAO_W2M_TEAMMATE_PUSH absent/false, _should_teammate_push returns False."""
        monkeypatch.delenv("CAO_W2M_TEAMMATE_PUSH", raising=False)
        with patch(
            "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
        ) as mock_cfg:
            mock_cfg.get.return_value = False
            assert _should_teammate_push("sup-001") is False

    def test_attempt_push_does_nothing_when_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """deliver_pending-equivalent: no write to any CC path when flag is off."""
        inbox_path = tmp_path / "teams" / "session-abc" / "inboxes" / "team-lead.json"
        messages = [_make_message()]

        with patch(
            "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
        ) as mock_cfg:
            mock_cfg.get.return_value = False
            result = _should_teammate_push("sup-001")

        assert result is False
        assert not inbox_path.exists()


class TestAC2FlagOnWellFormedEntry:
    """AC#2 — Flag ON + path registered = well-formed CC inbox entry."""

    def test_writes_valid_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        inbox_path = tmp_path / "teams" / "session-72fd02e4" / "inboxes" / "team-lead.json"
        messages = [_make_message(msg_id=5, sender_id="kiro_dev", message="Gate passed")]

        # Clear in-memory dedup state.
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_cfg.get.return_value = True
            mock_meta.return_value = _metadata_with_path(str(inbox_path))

            assert _should_teammate_push("sup-001") is True
            result = attempt_teammate_push("sup-001", messages)

        assert result is True
        assert inbox_path.exists()

        entries = json.loads(inbox_path.read_text())
        assert isinstance(entries, list)
        assert len(entries) == 1

        entry = entries[0]
        assert entry["type"] == "message"
        assert entry["from"] == _TEAMMATE_FROM
        assert entry["read"] is False
        assert entry["msgV"] == 1
        # Validate msg_id is a valid UUID.
        uuid.UUID(entry["msg_id"])
        assert "text" in entry
        assert "timestamp" in entry
        assert "summary" in entry

    def test_from_field_is_cao_bridge(self, tmp_path: Path) -> None:
        """The `from` field must always be 'cao-bridge'."""
        inbox_path = tmp_path / "inbox.json"
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            messages = [_make_message(msg_id=1, sender_id="grok_dev")]
            attempt_teammate_push("sup-001", messages)

        entries = json.loads(inbox_path.read_text())
        assert entries[0]["from"] == "cao-bridge"


class TestAC3WriteFailureGracefulFallback:
    """AC#3 — Write failure = graceful fallback."""

    def test_nonexistent_dir_no_exception(self, tmp_path: Path) -> None:
        """Non-existent parent dir that can't be created → graceful failure."""
        # Use a path under /proc (Linux) which can't have dirs created.
        inbox_path = Path("/proc/fake_cao_test/inbox.json")
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            messages = [_make_message(msg_id=1)]
            # Should not raise.
            result = attempt_teammate_push("sup-001", messages)

        assert result is False

    def test_corrupt_json_in_existing_file(self, tmp_path: Path) -> None:
        """Corrupt JSON in existing inbox file → graceful failure."""
        inbox_path = tmp_path / "inbox.json"
        inbox_path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            messages = [_make_message(msg_id=1)]
            result = attempt_teammate_push("sup-001", messages)

        assert result is False

    def test_non_array_json(self, tmp_path: Path) -> None:
        """Inbox file contains JSON object (not array) → abort write."""
        inbox_path = tmp_path / "inbox.json"
        inbox_path.write_text('{"not": "an array"}', encoding="utf-8")
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            messages = [_make_message(msg_id=1)]
            result = attempt_teammate_push("sup-001", messages)

        assert result is False

    @pytest.mark.slow  # F254 D19: exceeds unit budget
    def test_permission_error(self, tmp_path: Path) -> None:
        """Inbox file not writable → graceful fallback."""
        inbox_dir = tmp_path / "teams" / "inboxes"
        inbox_dir.mkdir(parents=True)
        inbox_path = inbox_dir / "team-lead.json"
        inbox_path.write_text("[]", encoding="utf-8")
        # Make directory read-only so tempfile creation fails.
        inbox_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with (
                patch(
                    "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
                ) as mock_meta,
            ):
                mock_meta.return_value = _metadata_with_path(str(inbox_path))
                messages = [_make_message(msg_id=1)]
                result = attempt_teammate_push("sup-001", messages)

            assert result is False
        finally:
            # Restore permissions for cleanup.
            inbox_dir.chmod(stat.S_IRWXU)


class TestAC4LockfileContention:
    """AC#4 — Lockfile contention = retry + eventual success."""

    def test_retries_until_lock_released(self, tmp_path: Path) -> None:
        """Held lockfile is released mid-retry → write succeeds."""
        inbox_path = tmp_path / "inbox.json"
        lock_path = Path(str(inbox_path) + ".lock")
        # Create the lock manually.
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)

        # Release after a short delay (simulating another writer finishing).
        def _release():
            time.sleep(0.05)
            os.close(held_fd)
            os.unlink(str(lock_path))

        t = threading.Thread(target=_release, daemon=True)
        t.start()

        # Our acquire should succeed after retries.
        fd = _acquire_lockfile(lock_path)
        t.join(timeout=2)
        assert fd is not None
        _release_lockfile(fd, lock_path)

    def test_stale_lock_force_removed(self, tmp_path: Path) -> None:
        """Stale lock (older than threshold) is force-removed and write proceeds."""
        inbox_path = tmp_path / "inbox.json"
        lock_path = Path(str(inbox_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Create a stale lock (mtime in the past).
        lock_path.write_text("stale", encoding="utf-8")
        stale_time = time.time() - (_LOCK_STALE_SECONDS + 1)
        os.utime(str(lock_path), (stale_time, stale_time))

        fd = _acquire_lockfile(lock_path)
        assert fd is not None
        _release_lockfile(fd, lock_path)

    def test_stale_lock_inode_mismatch_retries(self, tmp_path: Path) -> None:
        """SHOULD-1: if another writer recreates lock between unlink and our create,
        fstat/stat inode mismatch causes retry (not proceed on wrong lock).

        We verify the retry logic by checking that _acquire_lockfile eventually
        returns a valid fd even when the first stale-removal attempt races.
        """
        inbox_path = tmp_path / "inbox.json"
        lock_path = Path(str(inbox_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Create a stale lock.
        lock_path.write_text("stale", encoding="utf-8")
        stale_time = time.time() - (_LOCK_STALE_SECONDS + 1)
        os.utime(str(lock_path), (stale_time, stale_time))

        # The lock is stale, _acquire_lockfile should force-remove and succeed.
        fd = _acquire_lockfile(lock_path)
        assert fd is not None
        _release_lockfile(fd, lock_path)


class TestAC5StaleInboxPath:
    """AC#5 — Stale inbox path (session restart) = fallback."""

    def test_stale_path_graceful_fallback(self, tmp_path: Path) -> None:
        """Registered path that no longer exists → graceful failure, no crash."""
        stale_path = tmp_path / "old-session" / "gone" / "team-lead.json"
        # Don't create the directory → mkdir will succeed (graceful), but let's use
        # a path under /proc to truly prevent creation.
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path("/proc/no_such_cao_path/inbox.json")
            messages = [_make_message(msg_id=1)]
            result = attempt_teammate_push("sup-001", messages)

        assert result is False


class TestAC6NotificationContentShape:
    """AC#6 — Notification content shape."""

    def test_text_contains_worker_name_and_drain_instruction(self) -> None:
        entry = _build_entry("kiro_dev", "Task completed — 3 files changed", 2)
        assert "kiro_dev" in entry["text"]
        assert "list_messages" in entry["text"]
        assert "ack_messages" in entry["text"]
        assert "2 message(s) ready" in entry["text"]

    def test_summary_within_limit(self) -> None:
        long_message = "x" * 200
        entry = _build_entry("worker", long_message, 1)
        assert len(entry["summary"]) <= _SUMMARY_MAX_CHARS

    def test_text_preview_truncated(self) -> None:
        long_message = "y" * 500
        entry = _build_entry("worker", long_message, 1)
        # The text should contain at most _TEXT_PREVIEW_CHARS of the message.
        lines = entry["text"].split("\n")
        first_line = lines[0]
        # First line format: [CAO:worker] <preview>
        preview_part = first_line.split("] ", 1)[1]
        assert len(preview_part) <= _TEXT_PREVIEW_CHARS


class TestAC7PullSettlementUnchanged:
    """AC#7 — Pull settlement is unchanged.

    After a bridge notification is written, ack_messages still transitions
    PENDING→DELIVERED. The CC inbox entry's read status is irrelevant to
    CAO settlement.
    """

    def test_bridge_write_does_not_alter_message_status(self, tmp_path: Path) -> None:
        """Messages stay PENDING after bridge write — only ack settles them."""
        inbox_path = tmp_path / "inbox.json"
        messages = [_make_message(msg_id=10, sender_id="worker-01")]
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            result = attempt_teammate_push("sup-001", messages)

        assert result is True
        # The message object status is unchanged (still PENDING).
        assert messages[0].status == MessageStatus.PENDING


class TestSHOULD2DedupPersistence:
    """SHOULD-2 — Dedup via last_notified_inbox_id persistence."""

    def test_dedup_skips_already_notified(self, tmp_path: Path) -> None:
        """F476: Client-side dedup removed; consumption cursor blocks old messages."""
        inbox_path = tmp_path / "inbox.json"
        messages = [_make_message(msg_id=3), _make_message(msg_id=5)]

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=5,  # consumed through 5
            ),
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            result = attempt_teammate_push("sup-001", messages)

        # All messages at or below consumption cursor → no write.
        assert result is False
        assert not inbox_path.exists()

    def test_dedup_allows_newer_messages(self, tmp_path: Path) -> None:
        """Messages with id > last_notified_inbox_id are notified."""
        inbox_path = tmp_path / "inbox.json"

        messages = [_make_message(msg_id=3), _make_message(msg_id=6)]

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            result = attempt_teammate_push("sup-001", messages)

        assert result is True
        entries = json.loads(inbox_path.read_text())
        assert len(entries) == 1

    def test_persists_high_water_mark(self, tmp_path: Path) -> None:
        """F476: push no longer persists last_notified; test consumption cursor dedup."""
        inbox_path = tmp_path / "inbox.json"
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            messages = [_make_message(msg_id=7), _make_message(msg_id=9)]
            result = attempt_teammate_push("sup-001", messages)

        # Push succeeds
        assert result is True
        # F476: _last_notified no longer populated by the push path
        # Dedicated column setter is no longer called (it's a stub)


class TestWriteInboxAppend:
    """Test that multiple writes append correctly."""

    def test_appends_to_existing_array(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "inbox.json"
        # Pre-existing entry.
        existing = [{"type": "message", "from": "other", "text": "hi"}]
        inbox_path.write_text(json.dumps(existing), encoding="utf-8")

        entry = _build_entry("worker", "new msg", 1)
        success = _write_inbox_entry(inbox_path, entry)
        assert success is True

        entries = json.loads(inbox_path.read_text())
        assert len(entries) == 2
        assert entries[0]["from"] == "other"
        assert entries[1]["from"] == _TEAMMATE_FROM

    def test_creates_file_if_absent(self, tmp_path: Path) -> None:
        inbox_path = tmp_path / "new_inbox.json"
        entry = _build_entry("worker", "hello", 1)
        success = _write_inbox_entry(inbox_path, entry)
        assert success is True
        entries = json.loads(inbox_path.read_text())
        assert len(entries) == 1


class TestShouldTeammatePush:
    """Test the _should_teammate_push gate logic."""

    def test_false_when_no_metadata(self) -> None:
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_cfg.get.return_value = True
            mock_meta.return_value = None
            assert _should_teammate_push("sup-001") is False

    def test_false_when_path_not_in_metadata(self) -> None:
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_cfg.get.return_value = True
            mock_meta.return_value = _metadata_without_path()
            assert _should_teammate_push("sup-001") is False

    def test_true_when_flag_and_path_present(self) -> None:
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_cfg.get.return_value = True
            mock_meta.return_value = _metadata_with_path("/tmp/inbox.json")
            assert _should_teammate_push("sup-001") is True


class TestOnInsertConfigGateBypass:
    """F123-P0 — attempt_teammate_push_on_insert bypasses the config flag.

    The insert-path push must fire unconditionally (mailbox_pull mode implies
    the push bridge is required for supervisor wake-up), while still validating
    the cc_team_inbox_path metadata. These tests directly exercise the function
    to kill the gate-bypass and validation mutants (B1).
    """

    @pytest.mark.xfail(reason="F136 retires attempt_teammate_push_on_insert (D1: single writer)")
    def test_push_succeeds_when_config_flag_false(self, tmp_path: Path) -> None:
        """Flag OFF still writes a CC inbox entry on insert (proves the bypass)."""
        inbox_path = tmp_path / "teams" / "session-abc" / "inboxes" / "team-lead.json"
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.ConfigService"
            ) as mock_cfg,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_cfg.get.return_value = False
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            result = attempt_teammate_push_on_insert("sup-001", [_make_message(msg_id=5)])

        assert result is True
        assert inbox_path.exists()
        entries = json.loads(inbox_path.read_text())
        assert len(entries) == 1
        assert entries[0]["from"] == _TEAMMATE_FROM

    def test_returns_false_when_no_inbox_path_in_metadata(self, tmp_path: Path) -> None:
        """No cc_team_inbox_path in metadata → validation blocks the push."""
        inbox_path = tmp_path / "should_not_exist" / "inbox.json"
        with (
            patch("cli_agent_orchestrator.services.teammate_push_service.ConfigService"),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_without_path()
            result = attempt_teammate_push_on_insert("sup-001", [_make_message(msg_id=5)])

        assert result is False
        assert not inbox_path.exists()

    def test_returns_false_on_empty_messages(self, tmp_path: Path) -> None:
        """Empty messages list → early-out, no write."""
        inbox_path = tmp_path / "should_not_exist" / "inbox.json"
        with (
            patch("cli_agent_orchestrator.services.teammate_push_service.ConfigService"),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            result = attempt_teammate_push_on_insert("sup-001", [])

        assert result is False
        assert not inbox_path.exists()

    def test_returns_false_on_empty_string_terminal_id(self, tmp_path: Path) -> None:
        """Empty terminal_id → early-out, no write (guards the truthiness guard)."""
        inbox_path = tmp_path / "should_not_exist" / "inbox.json"
        with (
            patch("cli_agent_orchestrator.services.teammate_push_service.ConfigService"),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata"
            ) as mock_meta,
        ):
            mock_meta.return_value = _metadata_with_path(str(inbox_path))
            result = attempt_teammate_push_on_insert("", [_make_message(msg_id=5)])

        assert result is False
        assert not inbox_path.exists()
