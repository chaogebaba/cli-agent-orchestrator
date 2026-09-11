"""F970 (#819) — the send SSE is CONSUMED, not just observed by URL.

F862 read only ``resp.url`` on ``POST /backend-api/f/conversation`` and threw
the body away (D6: "SSE is passive observation only"). Blueprint Amendment D
reopens that so the turn can be watched live and its quota read. These tests
pin the three things that make consuming it safe:

1. the parser reconstructs the answer text, status/model metadata and the
   ``limits_progress`` quota from a RECORDED transcript in the shape findings
   §2 measured — including delta-encoding v1's continuation frames, which carry
   only ``v``;
2. credential-shaped frames (``resume_conversation_token``, a JWT) never reach
   an event, the tracker's state, or the snapshot — the rule is an allow-list,
   so an unknown future field is dropped by default;
3. the in-page tee reads the browser's OWN clone and issues no request, so the
   D7/D16 one-send invariant is untouched, and a page that cannot install it
   degrades to pre-F970 behaviour instead of failing the turn.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import Transport
from cli_agent_orchestrator.chatgpt_web_runner.sse_stream import (
    KIND_DONE,
    KIND_QUOTA,
    KIND_TOKEN,
    SSE_BINDING_NAME,
    SseProgressTracker,
    build_sse_tee_script,
    iter_sse_frames,
)

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "f970_send_stream.sse"
_FAKE_JWT_MARKER = "THIS-IS-A-FAKE-JWT-FIXTURE-VALUE"


def _recorded() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _feed_all(tracker: SseProgressTracker, text: str, *, chunk_size: int = 0) -> list:
    """Feed the transcript, optionally split at an arbitrary byte boundary."""
    if chunk_size <= 0:
        return tracker.feed(text)
    events = []
    for i in range(0, len(text), chunk_size):
        events.extend(tracker.feed(text[i : i + chunk_size]))
    return events


# ── 1. the recorded transcript is reconstructed ──────────────────────────────


def test_recorded_transcript_reconstructs_answer_quota_and_metadata():
    tracker = SseProgressTracker()
    events = _feed_all(tracker, _recorded())

    assert tracker.encoding == "v1"
    # Continuation frames (``{"v": ...}`` with no pointer) append to the last
    # pointer — without that the answer is the first chunk only.
    assert tracker.text == "The sentinel line goes last. Findings follow."
    assert tracker.status == "finished_successfully"
    assert tracker.end_turn is True
    assert tracker.model_slug == "gpt-5-6-thinking"
    assert tracker.thinking_effort == "extended"
    assert tracker.conversation_id == "6aa15468-e7a0-83e9-8db7-18a35b16e573"
    assert tracker.stream_complete is True
    assert tracker.done is True

    quota = {q["feature_name"]: q for q in tracker.quota}
    assert quota["deep_research"]["remaining"] == 25
    assert quota["file_upload"]["reset_after"] == "2026-09-12T04:00:00Z"
    # Only the three allow-listed keys survive a quota entry.
    assert "internal_bucket" not in quota["file_upload"]

    kinds = [e.kind for e in events]
    assert kinds[0] == "encoding"
    assert kinds[-1] == KIND_DONE
    assert KIND_QUOTA in kinds
    assert sum(1 for k in kinds if k == KIND_TOKEN) == 3


def test_chunk_boundaries_anywhere_do_not_change_the_result():
    """The tee hands over network-sized chunks that split frames and lines."""
    whole = SseProgressTracker()
    _feed_all(whole, _recorded())
    for size in (1, 7, 64, 997):
        split = SseProgressTracker()
        _feed_all(split, _recorded(), chunk_size=size)
        assert split.text == whole.text, f"chunk_size={size}"
        assert split.snapshot()["quota"] == whole.snapshot()["quota"], f"chunk_size={size}"
        assert split.end_turn is True


def test_a_continuation_of_an_unknown_pointer_never_lands_in_the_answer():
    """The fixture appends ``32`` as a continuation of
    ``/message/metadata/finished_duration_sec`` — a pointer this module does not
    interpret. Forgetting the last pointer for ignored frames would append that
    number to the answer text, corrupting it."""
    tracker = SseProgressTracker()
    _feed_all(tracker, _recorded())
    assert "32" not in tracker.text
    assert tracker.frames_dropped >= 2


def test_patch_arrays_apply_every_sub_patch():
    tracker = SseProgressTracker()
    tracker.feed(
        'event: delta\ndata: {"o":"patch","v":['
        '{"p":"/message/status","o":"replace","v":"finished_successfully"},'
        '{"p":"/message/end_turn","o":"replace","v":true}]}\n\n'
    )
    assert tracker.status == "finished_successfully"
    assert tracker.end_turn is True


def test_malformed_frames_degrade_to_no_progress_never_to_an_exception():
    tracker = SseProgressTracker()
    assert tracker.feed("event: delta\ndata: {not json\n\n") == []
    assert tracker.feed("data: \n\n") == []
    assert tracker.feed("") == []
    assert tracker.text == ""


def test_iter_sse_frames_returns_the_incomplete_tail_as_remainder():
    frames, remainder = iter_sse_frames('data: {"a":1}\n\ndata: {"b"')
    assert len(frames) == 1
    assert remainder == 'data: {"b"'


# ── 2. hygiene: the allow-list, not a scrub ──────────────────────────────────


def test_the_jwt_frame_reaches_neither_an_event_nor_the_snapshot():
    tracker = SseProgressTracker()
    events = _feed_all(tracker, _recorded())
    blob = json.dumps([e.to_payload() for e in events]) + json.dumps(tracker.snapshot())
    assert _FAKE_JWT_MARKER not in blob
    assert "resume_conversation_token" not in blob
    assert _FAKE_JWT_MARKER not in tracker.text


def test_an_unknown_credential_shaped_field_is_dropped_by_default():
    """The rule must hold for a field nobody has seen yet — that is the whole
    point of interpreting an allow-list instead of scrubbing known names."""
    tracker = SseProgressTracker()
    events = tracker.feed(
        'data: {"type":"brand_new_event","conduit_refresh_token":"sk-abc123",'
        '"conversation_id":"c1"}\n\n'
    )
    assert events == []
    assert "sk-abc123" not in json.dumps(tracker.snapshot())
    assert tracker.frames_dropped == 1


def test_the_word_sentinel_in_the_ANSWER_is_not_mistaken_for_a_credential():
    """A raw-substring scrub would eat this lane's own answers: every
    ``design_findings`` turn ends with an END_REVIEW sentinel and the prose
    above it says so. Hygiene keys on JSON KEYS, so content survives."""
    tracker = SseProgressTracker()
    tracker.feed(
        'event: delta\ndata: {"p":"/message/content/parts/0","o":"append",'
        '"v":"End with the terminal sentinel token line."}\n\n'
    )
    assert tracker.text == "End with the terminal sentinel token line."


# ── 3. the in-page tee: reads a clone, sends nothing ─────────────────────────


def test_the_tee_script_calls_through_exactly_once_and_constructs_no_request():
    script = build_sse_tee_script()
    # Exactly one call to the real fetch: the app's own. A second would be a
    # second send, which D7/D16 forbids outright.
    assert script.count("origFetch.apply") == 1
    assert "new Request(" not in script
    assert "XMLHttpRequest" not in script
    assert "sendBeacon" not in script
    # It reads the browser's own copy and returns the ORIGINAL response object.
    assert "res.clone()" in script
    assert "new Response(" not in script
    # The /prepare pre-warm is not the send.
    assert "'/prepare'" in script
    # The JWT frame is filtered in-page, before it can cross the boundary.
    assert "resume_conversation_token" in script
    assert SSE_BINDING_NAME in script


class _FakePage:
    """Minimal page double: records init scripts and exposed bindings."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.init_scripts: list[str] = []
        self.bindings: dict[str, object] = {}

    async def expose_function(self, name, fn):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("expose_function unsupported")
        self.bindings[name] = fn

    async def add_init_script(self, script):  # noqa: ANN001
        self.init_scripts.append(script)


def test_arming_the_tee_installs_the_binding_and_tracks_fed_chunks():
    page = _FakePage()
    transport = Transport(page)
    assert asyncio.run(transport.arm_sse_tee()) is True
    assert SSE_BINDING_NAME in page.bindings
    assert page.init_scripts and "res.clone()" in page.init_scripts[0]

    seen: list = []
    page = _FakePage()
    transport = Transport(page)
    asyncio.run(transport.arm_sse_tee(on_event=seen.append))
    sink = page.bindings[SSE_BINDING_NAME]
    for i in range(0, len(_recorded()), 128):
        sink(_recorded()[i : i + 128])

    snapshot = transport.stream_snapshot()
    assert snapshot is not None
    assert snapshot["token_events"] == 3
    assert snapshot["end_turn"] is True
    assert snapshot["quota"][0]["feature_name"] == "deep_research"
    assert [e.kind for e in seen][-1] == KIND_DONE
    assert _FAKE_JWT_MARKER not in json.dumps(snapshot)


def test_a_page_that_cannot_install_the_tee_still_runs_the_turn():
    """Best-effort by construction: pre-F970 behaviour on failure, never a
    raise — the conversation GET is what decides the turn (D6)."""
    page = _FakePage(fail=True)
    transport = Transport(page)
    assert asyncio.run(transport.arm_sse_tee()) is False
    assert transport.sse_tracker is None
    assert transport.stream_snapshot() is None


def test_a_raising_sink_never_propagates_into_the_turn():
    page = _FakePage()
    transport = Transport(page)

    def _boom(_event):  # noqa: ANN001
        raise ValueError("consumer blew up")

    asyncio.run(transport.arm_sse_tee(on_event=_boom))
    page.bindings[SSE_BINDING_NAME](_recorded())  # must not raise
    assert transport.stream_snapshot()["token_events"] == 3
