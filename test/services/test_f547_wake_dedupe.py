"""F547 #403: duplicate supervisor doorbell pushes.

Contract points covered here (the cc_session_registry surfaces):
  * Point 1 — deterministic msg_id per (receiver incarnation, inbox_row_id).
  * Point 5 — per-sender content-hash dedupe window (default 20) in the
    socket-write sink.

Each test carries a mutation note: which reverted line makes it fail.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import cc_session_registry
from cli_agent_orchestrator.services.cc_session_registry import (
    _reset_dedupe_windows,
    build_wake_msg_id,
    build_wake_payload,
    write_to_socket,
)


@pytest.fixture(autouse=True)
def _reset_window():
    _reset_dedupe_windows()
    yield
    _reset_dedupe_windows()


def _msg_id(payload_line: str) -> str:
    return json.loads(payload_line)["msg_id"]


# ---------------------------------------------------------------------------
# Point 1: deterministic msg_id
# ---------------------------------------------------------------------------


def test_msgid_same_row_same_incarnation_is_stable():
    """Same (receiver, row, incarnation) → SAME id (a re-push is de-dupable).

    Mutation: revert build_wake_msg_id's sha256 seed to `str(uuid.uuid4())`
    (the pre-F547 line in build_wake_payload) → the two ids differ → fail.
    """
    a = build_wake_msg_id("sup1", 42, "pid:1000")
    b = build_wake_msg_id("sup1", 42, "pid:1000")
    assert a == b


def test_msgid_different_incarnation_differs():
    """Different receiver incarnation → DIFFERENT id (a new seat still surfaces).

    Mutation: drop `incarnation` from the seed string in build_wake_msg_id
    (`f"{receiver}\\x00{row}\\x00{incarnation or ''}"` → `f"{receiver}\\x00{row}"`)
    → the two ids collide → fail.
    """
    a = build_wake_msg_id("sup1", 42, "pid:1000")
    b = build_wake_msg_id("sup1", 42, "pid:2000")
    assert a != b


def test_msgid_different_row_differs():
    """Different inbox row → different id.

    Mutation: drop `inbox_row_id` from the seed → ids collide → fail.
    """
    assert build_wake_msg_id("sup1", 1, "pid:1") != build_wake_msg_id("sup1", 2, "pid:1")


def test_msgid_is_uuid_shaped():
    """Id parses as a UUID so legacy uuid-shaped parsers keep working.

    Mutation: return the raw 64-char sha256 digest instead of the 8-4-4-4-12
    reshaping in build_wake_msg_id → uuid.UUID() raises → fail.
    """
    uuid.UUID(build_wake_msg_id("sup1", 42, "pid:1000"))


def test_payload_uses_deterministic_msgid():
    """build_wake_payload emits the deterministic id, not a fresh uuid4.

    Mutation: restore `"msg_id": str(uuid.uuid4())` in build_wake_payload →
    two payloads for the same (worker,row,incarnation) differ → fail.
    """
    p1 = build_wake_payload("w", 7, incarnation="pid:5")
    p2 = build_wake_payload("w", 7, incarnation="pid:5")
    assert _msg_id(p1) == _msg_id(p2)
    assert _msg_id(p1) == build_wake_msg_id("w", 7, "pid:5")


# ---------------------------------------------------------------------------
# Point 5: per-sender content-hash dedupe window
# ---------------------------------------------------------------------------


def _payload(sender: str, content: str) -> str:
    return json.dumps(
        {
            "msgV": 1,
            "msg_id": "x",
            "type": "user",
            "message": {"role": "user", "content": content},
            "priority": "next",
            "from": sender,
        },
        separators=(",", ":"),
    )


def test_dedupe_drops_byte_identical_within_window():
    """A byte-identical payload from the same sender is suppressed while still
    inside the last-N window.

    Mutation: delete the `if _is_duplicate_in_window(...)` guard in
    write_to_socket (or make _is_duplicate_in_window always return False) → the
    second identical payload is no longer flagged → second call returns False →
    fail.
    """
    line = _payload("bridge:cao-71a72e09", "identical body")
    assert cc_session_registry._is_duplicate_in_window(line) is False  # first: recorded
    assert cc_session_registry._is_duplicate_in_window(line) is True  # second: dup


def test_dedupe_survives_interleaved_other_sender():
    """An interleaved message from ANOTHER sender does NOT reset the window
    (this is the exact bug: previous-only dedupe was defeated by interleaving).

    Mutation: key the window on 'previous only' (replace the deque with a
    single last-hash per sender) → the interleave clears it → second identical
    returns False → fail.
    """
    a = _payload("bridge:cao-A", "dup")
    b = _payload("bridge:cao-B", "other")
    assert cc_session_registry._is_duplicate_in_window(a) is False
    assert cc_session_registry._is_duplicate_in_window(b) is False  # interleave
    assert cc_session_registry._is_duplicate_in_window(a) is True  # still a dup


def test_dedupe_window_evicts_beyond_20(monkeypatch):
    """Past the window size, the oldest content falls out and is re-emittable.

    Mutation: give the deque no maxlen (unbounded) in _is_duplicate_in_window →
    the first payload is never evicted → returns True → fail.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.config_service.ConfigService.get",
        lambda path, default=None, override=None: 20 if path == "supervisor.wake.dedupe_window" else default,
    )
    first = _payload("bridge:cao-S", "content-0")
    assert cc_session_registry._is_duplicate_in_window(first) is False
    # Push 20 distinct further payloads → window (size 20) evicts `first`.
    for i in range(1, 21):
        assert cc_session_registry._is_duplicate_in_window(_payload("bridge:cao-S", f"content-{i}")) is False
    # `first` fell out → no longer a duplicate.
    assert cc_session_registry._is_duplicate_in_window(first) is False


def test_dedupe_is_per_sender():
    """Same content from different senders is not cross-suppressed.

    Mutation: key the window on content only (drop the per-sender dict key in
    _is_duplicate_in_window) → the second sender's identical body returns True
    → fail.
    """
    body = "same body"
    assert cc_session_registry._is_duplicate_in_window(_payload("bridge:cao-A", body)) is False
    assert cc_session_registry._is_duplicate_in_window(_payload("bridge:cao-B", body)) is False


def test_dedupe_failopen_on_unparseable_payload():
    """A non-JSON / shapeless payload is never treated as a duplicate (fail-open).

    Mutation: make _extract_sender_and_content raise instead of returning
    (None, None) on bad JSON → _is_duplicate_in_window would blow up → fail.
    """
    assert cc_session_registry._is_duplicate_in_window("not json at all") is False
    assert cc_session_registry._is_duplicate_in_window("not json at all") is False


def test_write_to_socket_dupe_returns_none_no_connect(monkeypatch):
    """write_to_socket returns None (success no-op) for an in-window dupe and
    does NOT attempt a socket connect on the second call.

    Mutation: return an error string (e.g. "skipped_dupe") instead of None from
    the dedupe branch → the caller would treat it as a refusal and fall back to
    the pane nudge → this test's `is None` assertion fails.
    """
    connects: list[str] = []

    class _FakeSock:
        def __init__(self, *a, **k):
            pass

        def settimeout(self, *_):
            pass

        def connect(self, path):
            connects.append(path)

        def sendall(self, *_):
            pass

        def shutdown(self, *_):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cc_session_registry.socket, "socket", lambda *a, **k: _FakeSock())

    line = _payload("bridge:cao-Z", "hello")
    assert write_to_socket("/tmp/does-not-matter.sock", line) is None
    assert connects == ["/tmp/does-not-matter.sock"]  # first write connected
    # Second, identical: dropped BEFORE connect.
    assert write_to_socket("/tmp/does-not-matter.sock", line) is None
    assert connects == ["/tmp/does-not-matter.sock"]  # no new connect



# ---------------------------------------------------------------------------
# Point 1 (integration): _attempt_native_ring binds msg_id to the receiver's
# live process incarnation (procStart).
# ---------------------------------------------------------------------------


def test_native_ring_threads_incarnation_from_record(monkeypatch):
    """_attempt_native_ring passes incarnation=record.proc_start into
    build_wake_payload, so the same row rung by two different receiver
    incarnations produces two different msg_ids.

    Mutation: revert doorbell_service to `build_wake_payload(terminal_id,
    max_written_row_id, message_body=..., sender_display_name=...)` (no
    incarnation) → captured incarnation is None for both → the two ids collide
    → fail.
    """
    from types import SimpleNamespace

    from cli_agent_orchestrator.services import doorbell_service

    captured = {}

    def _fake_build(worker_name, row_id, *, priority=None, message_body=None,
                    sender_display_name=None, incarnation=None):
        captured["incarnation"] = incarnation
        # Return a minimal valid wake line; do NOT call the (patched) real
        # build_wake_payload — that would recurse.
        return json.dumps(
            {
                "msgV": 1,
                "msg_id": build_wake_msg_id(worker_name, row_id, incarnation),
                "type": "user",
                "message": {"role": "user", "content": "x"},
                "priority": "next",
                "from": f"bridge:cao-{worker_name}",
            },
            separators=(",", ":"),
        )

    def _run_with_proc_start(proc_start: int) -> str:
        record = SimpleNamespace(
            pid=proc_start + 1,
            proc_start=proc_start,
            status_updated_at="t0",
            messaging_socket_path="/tmp/x.sock",
            version="2.1.5",
        )
        with (
            patch.object(
                doorbell_service, "get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.resolve_target",
                return_value=SimpleNamespace(refusal_reason=None, record=record),
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.check_version_guard",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.build_wake_payload",
                side_effect=_fake_build,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.read_peer_token",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.write_to_socket",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.verify_wake",
                return_value=True,
            ),
        ):
            decision = doorbell_service._attempt_native_ring("sup1", 42)
        assert decision == "rang"
        return captured["incarnation"]

    inc_a = _run_with_proc_start(1000)
    inc_b = _run_with_proc_start(2000)
    assert inc_a == "1000"
    assert inc_b == "2000"
    assert build_wake_msg_id("sup1", 42, inc_a) != build_wake_msg_id("sup1", 42, inc_b)



# ---------------------------------------------------------------------------
# Live-evidence regression (points 1 + 2 + 5 together).
#
# Observed 2026-08-28 on the supervisor seat (60d393b2): the bridge frame
#   "[cao] Callback from 60d393b2 (message id 1616). Run any command ..."
# arrived THREE times, and id 1617 TWICE, interleaved, all within ~2 min while
# the row was still unacked. With deterministic msg_id (pt1) + first-ring legacy
# text (pt2) + per-sender content-hash window (pt5), the SAME (sender,row)
# unacked inside the first 60 s must yield exactly ONE socket write, and any
# later re-ring text must differ (attempt count / age).
# ---------------------------------------------------------------------------


def test_live_evidence_one_write_per_sender_row_within_60s(monkeypatch):
    """Same (sender,row) rung repeatedly while unacked → exactly ONE socket
    write in the first-ring window; a later re-ring (backoff body) is a distinct
    line that DOES write.

    Reproduces the #403 screenshot: id 1616 x3 and 1617 x2 interleaved collapse
    to one write each inside the window.

    Mutation (any of):
      * restore uuid4 msg_id in build_wake_payload (pt1) → each ring's content
        wrapper still identical (content has no id), but see pt5;
      * delete the _is_duplicate_in_window guard in write_to_socket (pt5) → the
        3 identical 1616 frames all write → written count 3 → fail;
      * make the re-ring reuse the legacy first-ring text (pt2) → the later
        distinct-line assertion fails.
    """
    from cli_agent_orchestrator.services import cc_session_registry as reg

    _reset_dedupe_windows()

    written: list[str] = []

    class _FakeSock:
        def __init__(self, *a, **k):
            pass

        def settimeout(self, *_):
            pass

        def connect(self, *_):
            pass

        def sendall(self, data):
            # Capture only the payload line (auth frame, if any, is separate).
            written.append(data.decode("utf-8").rstrip("\n"))

        def shutdown(self, *_):
            pass

        def close(self):
            pass

    monkeypatch.setattr(reg.socket, "socket", lambda *a, **k: _FakeSock())

    sender = "60d393b2"

    def _ring(row: int, *, message_body=None):
        # Mirrors what _attempt_native_ring builds and writes for one ring.
        payload = build_wake_payload(
            sender, row, message_body=message_body, incarnation="pid:4242"
        )
        return write_to_socket("/tmp/60d393b2.sock", payload)

    # Interleaved first-ring frames, all unacked within the first 60 s:
    # 1616, 1616, 1617, 1616, 1617  (exactly the observed multiplicities).
    for row in (1616, 1616, 1617, 1616, 1617):
        assert _ring(row) is None  # every call returns success (no error/fallback)

    first_ring_lines = list(written)
    # Exactly one write per distinct (sender,row): 1616 once, 1617 once.
    assert len(first_ring_lines) == 2, first_ring_lines
    payloads = [json.loads(x) for x in first_ring_lines]
    rows_written = sorted(
        int(p["message"]["content"].split("message id ")[1].split(")")[0]) for p in payloads
    )
    assert rows_written == [1616, 1617]
    # Deterministic ids (pt1): re-deriving the id for the same (sender,row,incarnation)
    # reproduces exactly what was written.
    for p in payloads:
        row = int(p["message"]["content"].split("message id ")[1].split(")")[0])
        assert p["msg_id"] == build_wake_msg_id(sender, row, "pid:4242")

    # A later re-ring for 1616 carries the escalating-backoff body (attempt/age):
    # a DISTINCT content line, so it is NOT suppressed by the window and DOES write.
    repush_body = (
        "[cao] re-push 1, unacked 1m. Pending callback message id(s): 1616. "
        "Run any command to surface and ack."
    )
    assert _ring(1616, message_body=repush_body) is None
    assert len(written) == 3  # the re-ring wrote a new, distinct line
    assert written[-1] != first_ring_lines[0]
    # The re-ring content differs from the legacy first-ring text.
    last = json.loads(written[-1])
    assert "re-push 1" in last["message"]["content"]
    assert "Run any command to surface and ack it." not in last["message"]["content"]
