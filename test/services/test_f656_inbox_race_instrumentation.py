"""F656 (#511): team-inbox mutation instrumentation — EVIDENCE ONLY.

The post-ack resurrection race (a settled id re-injected ~41s after ack,
frequency ~1/299) is a file-level race on ``team-lead.json`` between this
fork's writers and the drain hook's scrub. Neither writer logged, so the
record could not distinguish the interleaving.

F656 adds a single structured DEBUG line on the dedicated ``cao.inbox_race``
logger at every atomic write/scrub of the inbox file. These tests pin:

1. The APPEND path (``write_supervisor_callback_notification``) emits one
   instrumentation line carrying pid, monotonic ns, entry counts, the msg_id
   added, size/mtime before+after, and a content hash.
2. The legacy pull-gate append (``_write_inbox_entry``) emits.
3. The SCRUB / read-mark path (``mark_cc_inbox_entries_read``) emits.
4. Behavior is BYTE-IDENTICAL with instrumentation active: the file the writer
   produces is exactly the file the pre-F656 writer produced (same JSON bytes),
   and the existing return contracts are unchanged.
5. Instrumentation is DEBUG-gated: nothing is emitted when the dedicated logger
   is not enabled for DEBUG, and a broken logger never breaks a write.
6. RACE-HONESTY: no writer performs an extra content read inside its held-lock
   window (the constraint Mutation A violated at gate r1).

Instrumentation must NOT change behavior. The assertions below therefore also
re-derive the expected on-disk bytes independently and compare.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from cli_agent_orchestrator.models.inbox import (
    InboxMessage,
    MessageStatus,
    OrchestrationType,
)
from cli_agent_orchestrator.services.teammate_push_service import (
    _TEAMMATE_FROM,
    _build_entry,
    _write_inbox_entry,
    callback_notification_id,
    mark_cc_inbox_entries_read,
    write_supervisor_callback_notification,
)

_RACE_LOGGER = "cao.inbox_race"
_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
MAILBOX_ID = "mb_f656test"


def _msg(msg_id: int = 1, sender: str = "worker-01", text: str = "done") -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=sender,
        receiver_id="sup-001",
        message=text,
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING,
        created_at=_NOW,
    )


def _make_cao_entry(mailbox_id: str, row_id: int, *, read: bool = False) -> Dict[str, Any]:
    return {
        "type": "message",
        "from": _TEAMMATE_FROM,
        "text": f"[CAO:worker-{row_id}] completed\n\n---\n1 message(s) ready.",
        "timestamp": "2026-08-31T08:00:00+00:00",
        "summary": f"worker-{row_id}: completed",
        "read": read,
        "msgV": 1,
        "msg_id": callback_notification_id(mailbox_id, row_id),
    }


def _race_records(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _RACE_LOGGER]


def _race_lines(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in _race_records(caplog)]


# ---------------------------------------------------------------------------
# 1. Append path emits with the full field set
# ---------------------------------------------------------------------------


def test_append_emits_instrumentation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    inbox = tmp_path / "team-lead.json"
    msg = _msg(7, sender="kiro_dev", text="hello")
    expected_msg_id = callback_notification_id(MAILBOX_ID, 7)

    with caplog.at_level(logging.DEBUG, logger=_RACE_LOGGER):
        result = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, message=msg
        )

    assert result.kind == "written"
    lines = _race_lines(caplog)
    assert len(lines) == 1, f"expected exactly one race line, got {lines}"
    line = lines[0]

    # Required fields present.
    assert "f656_inbox_mutation" in line
    assert "op=append" in line
    assert "outcome=written" in line
    assert re.search(r"\bpid=\d+", line)
    assert re.search(r"\bmono_ns=\d+", line)
    assert "entries_before=0" in line
    assert "entries_after=1" in line
    assert expected_msg_id in line  # msg_id added
    assert "removed=[]" in line
    # size before None (new file), after a real byte count.
    assert "size_before=None" in line
    assert re.search(r"size_after=\d+", line)
    # mtime: before None (new file), after numeric.
    assert "mtime_before=None" in line
    assert re.search(r"mtime_after=\d+(\.\d+)?", line)
    # 64-hex sha256 content hash.
    assert re.search(r"hash=[0-9a-f]{64}", line)


# ---------------------------------------------------------------------------
# 2. Legacy pull-gate append emits
# ---------------------------------------------------------------------------


def test_legacy_append_emits_instrumentation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox = tmp_path / "team-lead.json"
    entry = _build_entry("worker-x", "hello world", 1, mailbox_id=MAILBOX_ID, first_row_id=42)

    with caplog.at_level(logging.DEBUG, logger=_RACE_LOGGER):
        ok = _write_inbox_entry(inbox, entry)

    assert ok is True
    lines = _race_lines(caplog)
    assert len(lines) == 1
    assert "op=append_legacy" in lines[0]
    assert entry["msg_id"] in lines[0]
    assert "entries_before=0" in lines[0]
    assert "entries_after=1" in lines[0]


# ---------------------------------------------------------------------------
# 3. Scrub / read-mark path emits
# ---------------------------------------------------------------------------


def test_scrub_emits_instrumentation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    inbox = tmp_path / "team-lead.json"
    entry = _make_cao_entry(MAILBOX_ID, 100)
    inbox.write_text(json.dumps([entry], indent=2), encoding="utf-8")
    expected_msg_id = callback_notification_id(MAILBOX_ID, 100)

    with caplog.at_level(logging.DEBUG, logger=_RACE_LOGGER):
        marked = mark_cc_inbox_entries_read(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, acked_row_ids=[100]
        )

    assert marked == 1
    lines = _race_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "op=scrub_read_mark" in line
    assert "outcome=marked=1" in line
    # read-mark neither adds nor drops rows.
    assert "entries_before=1" in line
    assert "entries_after=1" in line
    assert "added=[]" in line
    # the scrubbed msg_id is reported in removed.
    assert expected_msg_id in line
    # before file already exists → real size + numeric mtime.
    assert re.search(r"size_before=\d+", line)
    assert re.search(r"size_after=\d+", line)
    assert re.search(r"mtime_before=\d+(\.\d+)?", line)
    assert re.search(r"mtime_after=\d+(\.\d+)?", line)
    assert re.search(r"hash=[0-9a-f]{64}", line)


def test_scrub_no_match_does_not_emit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No write happens when nothing matches, so no instrumentation line."""
    inbox = tmp_path / "team-lead.json"
    entry = _make_cao_entry(MAILBOX_ID, 100)
    inbox.write_text(json.dumps([entry], indent=2), encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger=_RACE_LOGGER):
        marked = mark_cc_inbox_entries_read(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, acked_row_ids=[999]
        )

    assert marked == 0
    assert _race_lines(caplog) == []


# ---------------------------------------------------------------------------
# 4. Behavior byte-identical with instrumentation active
# ---------------------------------------------------------------------------


def test_append_file_bytes_are_exactly_the_uninstrumented_form(tmp_path: Path) -> None:
    """The append writer writes EXACTLY the JSON bytes it always produced.

    Gate r1 noted the previous test only checked JSON semantics. This asserts
    byte-for-byte equality against an independently hand-built expected file
    (same entry schema, same ``indent=2`` serialization), so any stray field or
    serialization drift introduced by the instrumentation would fail here.
    """
    inbox = tmp_path / "team-lead.json"
    msg = _msg(7, sender="kiro_dev", text="hello")

    result = write_supervisor_callback_notification(
        inbox_path=inbox, mailbox_id=MAILBOX_ID, message=msg
    )
    assert result.kind == "written"

    # Independently re-derive the exact bytes the writer must have produced.
    expected_entry = {
        "type": "message",
        "from": _TEAMMATE_FROM,
        "text": "[CAO] Message 7 ready from kiro_dev. Drain: list_messages -> ack_messages",
        "timestamp": _NOW.isoformat(),
        "summary": "Message 7 ready",
        "read": False,
        "msgV": 1,
        "msg_id": callback_notification_id(MAILBOX_ID, 7),
    }
    expected_bytes = json.dumps([expected_entry], indent=2).encode("utf-8")

    # Byte-for-byte equality — not JSON-semantic equality.
    assert inbox.read_bytes() == expected_bytes


def test_scrub_file_bytes_match_uninstrumented_form(tmp_path: Path) -> None:
    """The scrub writer output equals the hand-computed read=True JSON bytes."""
    inbox = tmp_path / "team-lead.json"
    entries = [_make_cao_entry(MAILBOX_ID, 100), _make_cao_entry(MAILBOX_ID, 101)]
    inbox.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    marked = mark_cc_inbox_entries_read(
        inbox_path=inbox, mailbox_id=MAILBOX_ID, acked_row_ids=[100, 101]
    )
    assert marked == 2

    # Independent expected form: same entries with read flipped True.
    expected = [dict(e, read=True) for e in entries]
    expected_bytes = json.dumps(expected, indent=2)
    assert inbox.read_text() == expected_bytes


# ---------------------------------------------------------------------------
# 5. DEBUG-gated: silent unless the dedicated logger is at DEBUG
# ---------------------------------------------------------------------------


def test_no_emit_when_not_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    inbox = tmp_path / "team-lead.json"
    msg = _msg(7)

    # Capture at WARNING for the race logger → DEBUG line must be suppressed
    # both by the isEnabledFor guard and by the capture level.
    with caplog.at_level(logging.WARNING, logger=_RACE_LOGGER):
        result = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, message=msg
        )

    assert result.kind == "written"
    assert _race_lines(caplog) == []
    # Write still happened (behavior unchanged).
    assert len(json.loads(inbox.read_text())) == 1


def test_broken_logger_does_not_break_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the instrumentation logger raises, the write must still succeed."""
    import cli_agent_orchestrator.services.teammate_push_service as tps

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("logging exploded")

    # Force the DEBUG-enabled branch, then blow up inside the emit.
    monkeypatch.setattr(tps._inbox_race_logger, "isEnabledFor", lambda *_a: True)
    monkeypatch.setattr(tps._inbox_race_logger, "debug", _boom)

    inbox = tmp_path / "team-lead.json"
    result = write_supervisor_callback_notification(
        inbox_path=inbox, mailbox_id=MAILBOX_ID, message=_msg(9)
    )
    assert result.kind == "written"
    assert len(json.loads(inbox.read_text())) == 1


# ---------------------------------------------------------------------------
# 6. Race-honesty guard: no extra content read inside the held-lock window
# ---------------------------------------------------------------------------
#
# Gate r1 Mutation A added a forbidden extra ``read_bytes()`` inside the scrub
# writer's locked region and ALL eight tests stayed green — the suite did not
# guard the central race-honesty constraint. This section closes that gap by
# counting content reads of the inbox path that occur WHILE the writer's
# lockfile is held, and asserting the count never exceeds the uninstrumented
# baseline of a single read-modify-write read.
#
# Gate r2 finding 2: the r1 Mutation-A shape is guarded by
# ``if _inbox_race_logger.isEnabledFor(logging.DEBUG): inbox_path.read_bytes()``
# — i.e. the forbidden read only executes when the dedicated logger is at DEBUG.
# A guard test that runs the writer at the default level therefore never
# exercises the mutated path and stays green. So EACH guard test below FORCES
# ``cao.inbox_race`` to DEBUG (via caplog.at_level) around the writer call, so
# the forbidden read runs in an ordinary pytest invocation with no external env
# (no --log-cli-level, no CAO env var). ``_force_race_debug`` centralizes this.


class _InLockReadCounter:
    """Counts Path.read_text/read_bytes calls on the inbox while its lock is held.

    A writer holds the lock exactly while ``<inbox>.lock`` exists on disk. Every
    legitimate read-modify-write reads the inbox contents exactly once inside
    that window. Any additional in-lock content read (Mutation A) pushes the
    count above the baseline and fails the guarding test.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, inbox_path: Path) -> None:
        self.inbox = inbox_path.resolve()
        self.lock = Path(str(self.inbox) + ".lock")
        self.in_lock_reads = 0
        self._orig_read_text = Path.read_text
        self._orig_read_bytes = Path.read_bytes

        counter = self

        def _counting_read_text(self_path: Path, *a: Any, **k: Any) -> str:
            counter._record(self_path)
            return counter._orig_read_text(self_path, *a, **k)

        def _counting_read_bytes(self_path: Path, *a: Any, **k: Any) -> bytes:
            counter._record(self_path)
            return counter._orig_read_bytes(self_path, *a, **k)

        monkeypatch.setattr(Path, "read_text", _counting_read_text)
        monkeypatch.setattr(Path, "read_bytes", _counting_read_bytes)

    def _record(self, self_path: Path) -> None:
        try:
            same = self_path.resolve() == self.inbox
        except OSError:
            same = False
        if same and self.lock.exists():
            self.in_lock_reads += 1


def _force_race_debug(caplog: pytest.LogCaptureFixture) -> Any:
    """Context manager that forces the ``cao.inbox_race`` logger to DEBUG.

    Any instrumentation path gated on ``_inbox_race_logger.isEnabledFor(DEBUG)``
    — including a DEBUG-gated forbidden in-lock read (the r1 Mutation-A shape) —
    then executes inside an ordinary pytest run, so the counter below actually
    observes it. No external env (``--log-cli-level``, CAO vars) is required.
    """
    return caplog.at_level(logging.DEBUG, logger=_RACE_LOGGER)


def test_append_no_extra_in_lock_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Native append performs no in-lock content read on a fresh file.

    A brand-new file skips the read entirely (baseline 0). Mutation A would add
    an in-lock read and trip this guard. DEBUG is forced so a DEBUG-gated
    mutation executes here.
    """
    inbox = tmp_path / "team-lead.json"
    counter = _InLockReadCounter(monkeypatch, inbox)

    with _force_race_debug(caplog):
        result = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, message=_msg(7)
        )
    assert result.kind == "written"
    assert counter.in_lock_reads == 0


def test_append_existing_file_single_in_lock_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Native append over an existing file reads its contents exactly once in-lock."""
    inbox = tmp_path / "team-lead.json"
    inbox.write_text(json.dumps([_make_cao_entry(MAILBOX_ID, 1)], indent=2), encoding="utf-8")
    counter = _InLockReadCounter(monkeypatch, inbox)

    with _force_race_debug(caplog):
        result = write_supervisor_callback_notification(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, message=_msg(7)
        )
    assert result.kind == "written"
    assert counter.in_lock_reads == 1


def test_legacy_append_single_in_lock_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    inbox = tmp_path / "team-lead.json"
    inbox.write_text(json.dumps([_make_cao_entry(MAILBOX_ID, 1)], indent=2), encoding="utf-8")
    counter = _InLockReadCounter(monkeypatch, inbox)

    entry = _build_entry("worker-x", "hi", 1, mailbox_id=MAILBOX_ID, first_row_id=42)
    with _force_race_debug(caplog):
        ok = _write_inbox_entry(inbox, entry)
    assert ok is True
    assert counter.in_lock_reads == 1


def test_scrub_single_in_lock_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The scrub path (Mutation A's target) reads the inbox exactly once in-lock."""
    inbox = tmp_path / "team-lead.json"
    inbox.write_text(json.dumps([_make_cao_entry(MAILBOX_ID, 100)], indent=2), encoding="utf-8")
    counter = _InLockReadCounter(monkeypatch, inbox)

    with _force_race_debug(caplog):
        marked = mark_cc_inbox_entries_read(
            inbox_path=inbox, mailbox_id=MAILBOX_ID, acked_row_ids=[100]
        )
    assert marked == 1
    # Baseline is exactly one read-modify-write read; Mutation A made it two.
    assert counter.in_lock_reads == 1


def test_in_lock_counter_detects_an_extra_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the counter actually fires on an in-lock read.

    Simulates the Mutation-A shape (an extra ``read_bytes`` while the lock is
    held) to prove the guard above is not vacuously green.
    """
    inbox = tmp_path / "team-lead.json"
    inbox.write_text(json.dumps([_make_cao_entry(MAILBOX_ID, 100)], indent=2), encoding="utf-8")
    counter = _InLockReadCounter(monkeypatch, inbox)

    lock = Path(str(inbox.resolve()) + ".lock")
    lock.touch()  # simulate the writer holding the lock
    try:
        inbox.read_bytes()  # the forbidden in-lock content read
    finally:
        lock.unlink()

    assert counter.in_lock_reads == 1
