"""F178: Regression tests — ack_messages marks correlated CC inbox entries as read.

Tests:
1. ack marks the entry read
2. Foreign entries (different msg_id / different from) untouched
3. Absent file: no-op (no crash)
4. Malformed file: fail-safe (no crash, no data loss)
5. Locked file: times out gracefully
6. Multi-row batch acks all correlated entries
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

from cli_agent_orchestrator.services.teammate_push_service import (
    F136_CALLBACK_NAMESPACE,
    _TEAMMATE_FROM,
    _acquire_lockfile_deadline,
    _release_lockfile,
    _write_inbox_entry,
    callback_notification_id,
    mark_cc_inbox_entries_read,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAILBOX_ID = "mb_test1234"


def _make_cao_entry(mailbox_id: str, row_id: int, *, read: bool = False) -> Dict[str, Any]:
    """Build a CC inbox entry matching what teammate_push writes."""
    msg_id = callback_notification_id(mailbox_id, row_id)
    return {
        "type": "message",
        "from": _TEAMMATE_FROM,
        "text": f"[CAO:worker-{row_id}] completed\n\n---\n1 message(s) ready.",
        "timestamp": "2026-08-13T08:00:00+00:00",
        "summary": f"worker-{row_id}: completed",
        "read": read,
        "msgV": 1,
        "msg_id": msg_id,
    }


def _make_foreign_entry(index: int = 0) -> Dict[str, Any]:
    """Build a CC inbox entry NOT authored by CAO (e.g. Claude Code's own teammate messaging)."""
    return {
        "type": "message",
        "from": "human-user",
        "text": f"Foreign message {index}",
        "timestamp": "2026-08-13T07:00:00+00:00",
        "summary": f"Human message {index}",
        "read": False,
        "msgV": 1,
        "msg_id": str(uuid.uuid4()),
    }


def _write_entries(inbox_path: Path, entries: List[Dict[str, Any]]) -> None:
    """Write entries to the inbox file."""
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _read_entries(inbox_path: Path) -> List[Dict[str, Any]]:
    """Read entries from the inbox file."""
    raw = inbox_path.read_text(encoding="utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Test 1: ack marks the entry read
# ---------------------------------------------------------------------------


def test_ack_marks_entry_read(tmp_path: Path) -> None:
    """A single acked row's CC inbox entry should become read=True."""
    inbox_path = tmp_path / "team-lead.json"
    entry = _make_cao_entry(MAILBOX_ID, 100)
    assert entry["read"] is False
    _write_entries(inbox_path, [entry])

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100],
    )

    assert marked == 1
    entries = _read_entries(inbox_path)
    assert len(entries) == 1
    assert entries[0]["read"] is True
    # Ensure other fields preserved
    assert entries[0]["msg_id"] == callback_notification_id(MAILBOX_ID, 100)
    assert entries[0]["from"] == _TEAMMATE_FROM


# ---------------------------------------------------------------------------
# Test 2: Foreign entries untouched
# ---------------------------------------------------------------------------


def test_foreign_entries_untouched(tmp_path: Path) -> None:
    """Entries from other producers must NOT be modified."""
    inbox_path = tmp_path / "team-lead.json"
    foreign1 = _make_foreign_entry(1)
    foreign2 = _make_foreign_entry(2)
    cao_entry = _make_cao_entry(MAILBOX_ID, 200)
    _write_entries(inbox_path, [foreign1, cao_entry, foreign2])

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[200],
    )

    assert marked == 1
    entries = _read_entries(inbox_path)
    assert len(entries) == 3
    # Foreign entries unchanged
    assert entries[0]["read"] is False
    assert entries[0]["from"] == "human-user"
    assert entries[0]["text"] == foreign1["text"]
    assert entries[2]["read"] is False
    assert entries[2]["from"] == "human-user"
    assert entries[2]["text"] == foreign2["text"]
    # CAO entry marked read
    assert entries[1]["read"] is True


# ---------------------------------------------------------------------------
# Test 3: Absent file — no-op
# ---------------------------------------------------------------------------


@pytest.mark.slow  # F254 D19: exceeds unit budget
def test_absent_file_noop(tmp_path: Path) -> None:
    """When the CC inbox file doesn't exist, return 0 without error."""
    inbox_path = tmp_path / "nonexistent" / "team-lead.json"

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100, 200, 300],
    )

    assert marked == 0
    assert not inbox_path.exists()


# ---------------------------------------------------------------------------
# Test 4: Malformed file — fail-safe
# ---------------------------------------------------------------------------


def test_malformed_file_failsafe(tmp_path: Path) -> None:
    """A corrupted JSON file should not crash and should not lose data."""
    inbox_path = tmp_path / "team-lead.json"
    inbox_path.write_text("not valid json {{{", encoding="utf-8")

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100],
    )

    assert marked == 0
    # File content preserved (not truncated)
    assert inbox_path.read_text() == "not valid json {{{"


def test_non_array_file_failsafe(tmp_path: Path) -> None:
    """A JSON file that is not an array should not crash."""
    inbox_path = tmp_path / "team-lead.json"
    inbox_path.write_text('{"not": "an array"}', encoding="utf-8")

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100],
    )

    assert marked == 0


# ---------------------------------------------------------------------------
# Test 5: Locked file — graceful timeout
# ---------------------------------------------------------------------------


@pytest.mark.slow  # F254 D19: exceeds unit budget
def test_locked_file_graceful(tmp_path: Path) -> None:
    """When lockfile is held by another process, timeout gracefully."""
    inbox_path = tmp_path / "team-lead.json"
    entry = _make_cao_entry(MAILBOX_ID, 100)
    _write_entries(inbox_path, [entry])

    # Hold the lock
    lock_path = Path(str(inbox_path) + ".lock")
    lock_fd = _acquire_lockfile_deadline(lock_path, time.monotonic() + 1.0)
    assert lock_fd is not None

    try:
        # Attempt should timeout gracefully (2s deadline in mark_cc_inbox_entries_read)
        # We shorten via the function's internal deadline
        marked = mark_cc_inbox_entries_read(
            inbox_path=inbox_path,
            mailbox_id=MAILBOX_ID,
            acked_row_ids=[100],
        )
        # Should return 0 (lock timeout), not raise
        assert marked == 0
        # Original file untouched
        entries = _read_entries(inbox_path)
        assert entries[0]["read"] is False
    finally:
        _release_lockfile(lock_fd, lock_path)


# ---------------------------------------------------------------------------
# Test 6: Multi-row batch acks all correlated entries
# ---------------------------------------------------------------------------


def test_multi_row_batch_acks_all(tmp_path: Path) -> None:
    """Acking multiple rows marks all their correlated CC entries as read."""
    inbox_path = tmp_path / "team-lead.json"
    entries = [
        _make_cao_entry(MAILBOX_ID, 100),
        _make_cao_entry(MAILBOX_ID, 101),
        _make_cao_entry(MAILBOX_ID, 102),
        _make_foreign_entry(1),
        _make_cao_entry(MAILBOX_ID, 103),
    ]
    _write_entries(inbox_path, entries)

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100, 101, 102, 103],
    )

    assert marked == 4
    result = _read_entries(inbox_path)
    assert len(result) == 5
    assert result[0]["read"] is True   # row 100
    assert result[1]["read"] is True   # row 101
    assert result[2]["read"] is True   # row 102
    assert result[3]["read"] is False  # foreign — untouched
    assert result[4]["read"] is True   # row 103


# ---------------------------------------------------------------------------
# Test 7: Already-read entries are not double-counted
# ---------------------------------------------------------------------------


def test_already_read_not_recounted(tmp_path: Path) -> None:
    """Entries already marked read should not be counted again."""
    inbox_path = tmp_path / "team-lead.json"
    entries = [
        _make_cao_entry(MAILBOX_ID, 100, read=True),  # already read
        _make_cao_entry(MAILBOX_ID, 101, read=False),
    ]
    _write_entries(inbox_path, entries)

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100, 101],
    )

    assert marked == 1  # only 101 newly marked
    result = _read_entries(inbox_path)
    assert result[0]["read"] is True
    assert result[1]["read"] is True


# ---------------------------------------------------------------------------
# Test 8: Empty acked_row_ids — no-op
# ---------------------------------------------------------------------------


def test_empty_row_ids_noop(tmp_path: Path) -> None:
    """Empty acked_row_ids returns 0 immediately."""
    inbox_path = tmp_path / "team-lead.json"
    entry = _make_cao_entry(MAILBOX_ID, 100)
    _write_entries(inbox_path, [entry])

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[],
    )

    assert marked == 0
    # Entry still unread
    entries = _read_entries(inbox_path)
    assert entries[0]["read"] is False


# ---------------------------------------------------------------------------
# Test 9: Partial match — only matching rows are marked
# ---------------------------------------------------------------------------


def test_partial_match(tmp_path: Path) -> None:
    """When only some acked rows have CC entries, only those are marked."""
    inbox_path = tmp_path / "team-lead.json"
    entries = [
        _make_cao_entry(MAILBOX_ID, 100),
        _make_cao_entry(MAILBOX_ID, 102),
    ]
    _write_entries(inbox_path, entries)

    # Ack rows 100-105, but only 100 and 102 have CC entries
    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[100, 101, 102, 103, 104, 105],
    )

    assert marked == 2
    result = _read_entries(inbox_path)
    assert result[0]["read"] is True
    assert result[1]["read"] is True


# ---------------------------------------------------------------------------
# Test 10: Deterministic msg_id correlation works correctly
# ---------------------------------------------------------------------------


def test_msg_id_correlation_correct(tmp_path: Path) -> None:
    """Verify that msg_id derivation matches between writer and reader."""
    inbox_path = tmp_path / "team-lead.json"

    # Simulate what teammate_push_service._build_entry would write
    from cli_agent_orchestrator.services.teammate_push_service import _build_entry

    entry = _build_entry("worker-x", "hello world", 1, mailbox_id=MAILBOX_ID, first_row_id=42)
    _write_entries(inbox_path, [entry])

    # Now ack row 42 — should match
    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[42],
    )

    assert marked == 1
    result = _read_entries(inbox_path)
    assert result[0]["read"] is True



# ---------------------------------------------------------------------------
# Test 11: Symlinked inbox path — resolve preserves symlink + targets real file
# ---------------------------------------------------------------------------


def test_symlinked_inbox_path_resolves(tmp_path: Path) -> None:
    """When inbox_path is a symlink, mark_cc_inbox_entries_read should:
    1. Resolve the symlink before operating
    2. After marking, the path is STILL a symlink
    3. The TARGET file carries the read flags
    """
    # Create the real file in a subdirectory
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "team-lead.json"
    entry = _make_cao_entry(MAILBOX_ID, 500)
    _write_entries(real_file, [entry])

    # Create a symlink pointing to the real file
    link_path = tmp_path / "link" / "team-lead.json"
    link_path.parent.mkdir()
    link_path.symlink_to(real_file)

    assert link_path.is_symlink()

    # Call mark_cc_inbox_entries_read with the SYMLINK path
    marked = mark_cc_inbox_entries_read(
        inbox_path=link_path,
        mailbox_id=MAILBOX_ID,
        acked_row_ids=[500],
    )

    assert marked == 1
    # The symlink is STILL a symlink (not replaced by a regular file)
    assert link_path.is_symlink()
    # The TARGET file carries the read flag
    target_entries = _read_entries(real_file)
    assert target_entries[0]["read"] is True
    # Reading through the symlink also shows read=True
    link_entries = _read_entries(link_path)
    assert link_entries[0]["read"] is True


def test_symlinked_inbox_write_entry_resolves(tmp_path: Path) -> None:
    """_write_inbox_entry with a symlinked path should preserve the symlink."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "team-lead.json"
    _write_entries(real_file, [])

    link_path = tmp_path / "link" / "team-lead.json"
    link_path.parent.mkdir()
    link_path.symlink_to(real_file)

    assert link_path.is_symlink()

    from cli_agent_orchestrator.services.teammate_push_service import _build_entry

    entry = _build_entry("worker-sym", "symlink test", 1, mailbox_id=MAILBOX_ID, first_row_id=600)
    success = _write_inbox_entry(link_path, entry)

    assert success is True
    # Symlink preserved
    assert link_path.is_symlink()
    # Real file has the entry
    target_entries = _read_entries(real_file)
    assert len(target_entries) == 1
    assert target_entries[0]["from"] == _TEAMMATE_FROM
