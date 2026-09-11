"""F970 (#819) — the per-turn relay store: seq, replay, declared gaps, hygiene.

The relay's whole value is that a reader can leave and come back without
guessing what it missed, so the store owes three things the fleet bus cannot
give: a per-turn monotonic seq, a replay cursor, and an EXPLICIT gap when the
bounded ring has outrun the reader. The fourth property is containment: the
relay must never become a place where a publisher bug leaks a token.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from cli_agent_orchestrator.services import chatgpt_turn_stream as cts

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_store():
    cts.reset_turn_streams()
    yield
    cts.reset_turn_streams()


def test_seq_is_per_turn_and_monotonic_from_one():
    store = cts.TurnStreamStore()
    a1 = store.append("turn-a", "token", {"text": "x"})
    a2 = store.append("turn-a", "token", {"text": "y"})
    b1 = store.append("turn-b", "token", {"text": "z"})
    assert (a1.seq, a2.seq, b1.seq) == (1, 2, 1)
    assert a2.ts >= a1.ts


def test_read_after_is_the_replay_cursor():
    store = cts.TurnStreamStore()
    for i in range(5):
        store.append("t", "token", {"text": str(i)})
    events, gap = store.read_after("t", 3)
    assert [e.payload["text"] for e in events] == ["3", "4"]
    assert gap is None


def test_a_reader_behind_the_ring_is_told_so_explicitly():
    """Silent truncation is the failure mode this exists to prevent: without a
    declared gap the reader concatenates a discontiguous stream and believes it
    has the whole turn."""
    store = cts.TurnStreamStore()
    for i in range(cts.TURN_RING_CAPACITY + 25):
        store.append("t", "token", {"text": str(i)})
    events, gap = store.read_after("t", 1)
    assert gap is not None
    assert gap.after_seq == 1
    assert gap.before_seq == events[0].seq
    assert gap.missing_count == events[0].seq - 2
    assert gap.reason == "ring_evicted"
    frame = cts.gap_frame(gap)
    assert frame.startswith("event: gap\n")
    assert "id:" not in frame  # a gap owns no seq of its own


def test_a_caught_up_reader_is_never_told_about_a_gap():
    store = cts.TurnStreamStore()
    for i in range(cts.TURN_RING_CAPACITY + 5):
        store.append("t", "token", {"text": str(i)})
    last = store.read_after("t", None)[0][-1].seq
    events, gap = store.read_after("t", last)
    assert events == []
    assert gap is None


def test_terminal_kinds_end_the_turn():
    store = cts.TurnStreamStore()
    store.append("t", "token", {"text": "x"})
    assert store.is_ended("t") is False
    store.append("t", "turn_finished", {"ok": True})
    assert store.is_ended("t") is True


def test_sse_frame_carries_the_seq_as_the_event_id():
    store = cts.TurnStreamStore()
    event = store.append("t", "quota", {"quota": [{"feature_name": "file_upload"}]})
    frame = cts.sse_frame(event)
    assert frame.startswith("event: quota\n")
    assert "\nid: 1\n\n" in frame
    assert '"turn_id": "t"' in frame


def test_credential_shaped_payload_keys_are_dropped_at_the_relay():
    """Defence in depth: the runner's allow-list already excludes these, so a
    key arriving here means a publisher bug — the relay must not store it."""
    store = cts.TurnStreamStore()
    event = store.append(
        "t",
        "token",
        {
            "text": "hello",
            "authorization": "Bearer abc",
            "nested": {"resume_conversation_token": "eyJ0", "ok": 1},
            "list": [{"proof_token": "p"}, {"keep": 2}],
        },
    )
    assert event.payload == {"text": "hello", "nested": {"ok": 1}, "list": [{}, {"keep": 2}]}


def test_unknown_turn_reads_as_empty_and_not_ended():
    store = cts.TurnStreamStore()
    assert store.read_after("nope", None) == ([], None)
    assert store.is_ended("nope") is False
    snapshot = store.snapshot("nope")
    assert snapshot["known"] is False and snapshot["events"] == []


def test_turns_are_capped_and_swept_by_ttl(monkeypatch):
    store = cts.TurnStreamStore()
    for i in range(cts.MAX_TURNS + 3):
        store.append(f"t{i}", "token", {"text": "x"})
    assert len(store.turn_ids()) == cts.MAX_TURNS
    assert "t0" not in store.turn_ids()  # oldest evicted first

    # Age every retained turn past the TTL and confirm the next touch sweeps.
    for turn in store._turns.values():  # noqa: SLF001 - white-box on purpose
        turn.updated_at = turn.updated_at - cts.TURN_TTL - timedelta(seconds=1)
    store.append("fresh", "token", {"text": "x"})
    assert store.turn_ids() == ["fresh"]


def test_the_singleton_is_shared_and_resettable():
    assert cts.get_turn_streams() is cts.get_turn_streams()
    cts.get_turn_streams().append("t", "token", {"text": "x"})
    cts.reset_turn_streams()
    assert cts.get_turn_streams().turn_ids() == []


def test_turn_ids_are_validated_by_a_closed_pattern():
    assert cts.TURN_ID_RE.match("a1b2c3d4e5f6")
    assert cts.TURN_ID_RE.match("run-1.2_3")
    assert not cts.TURN_ID_RE.match("../etc/passwd")
    assert not cts.TURN_ID_RE.match("")
    assert not cts.TURN_ID_RE.match("x" * 200)
