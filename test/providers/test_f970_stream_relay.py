"""F970 (#819) — the runner-side relay publisher.

The relay is a convenience surface bolted onto a lane whose correctness lives
elsewhere (the conversation GET and the published report). So the properties
that matter are all about it staying out of the way: bounded memory, no
blocking, no raising, and a terminal event that tells the follower whether what
it saw was the whole stream.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.sse_stream import SseProgressTracker
from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import (
    MAX_BATCH,
    MAX_QUEUED_EVENTS,
    TurnRelay,
    relay_from_env,
)

pytestmark = pytest.mark.unit


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list = []
        self.fail = fail

    def __call__(self, url, token, body, timeout):  # noqa: ANN001
        self.calls.append((url, token, body, timeout))
        if self.fail:
            raise RuntimeError("server down")

    @property
    def events(self):
        return [e for _, _, body, _ in self.calls for e in body["events"]]


def _relay(post, **kwargs):
    return TurnRelay(
        endpoint="http://127.0.0.1:8990",
        terminal_id="worker-1",
        token="tok-worker-1",
        turn_id="abc123",
        post=post,
        **kwargs,
    )


def test_it_posts_to_the_owning_workers_route_with_its_own_token():
    post = _Recorder()
    relay = _relay(post)
    relay.enqueue("token", {"text": "x"})
    assert relay.flush_once() == 1
    url, token, body, _ = post.calls[0]
    assert url == "http://127.0.0.1:8990/terminals/worker-1/chatgpt/turns/abc123/events"
    assert token == "tok-worker-1"
    assert body == {"events": [{"kind": "token", "payload": {"text": "x"}}]}


def test_stream_events_are_relayed_under_their_own_kinds():
    """A follower should see ``event: token`` / ``event: quota`` frames, not one
    opaque kind it has to re-parse."""
    post = _Recorder()
    relay = _relay(post)
    tracker = SseProgressTracker()
    for event in tracker.feed(
        'event: delta\ndata: {"p":"/message/content/parts/0","o":"append","v":"hi"}\n\n'
        'data: {"type":"conversation_detail_metadata","limits_progress":'
        '[{"feature_name":"file_upload","remaining":80}]}\n\n'
    ):
        relay.publish_stream_event(event)
    relay.flush_once()
    kinds = [e["kind"] for e in post.events]
    assert kinds == ["token", "quota"]
    assert post.events[0]["payload"]["text"] == "hi"
    assert post.events[1]["payload"]["quota"][0]["remaining"] == 80
    assert "kind" not in post.events[0]["payload"]


def test_a_full_queue_drops_and_counts_instead_of_blocking_the_turn():
    post = _Recorder()
    relay = _relay(post)
    for i in range(MAX_QUEUED_EVENTS + 40):
        relay.enqueue("token", {"text": str(i)})
    assert relay.dropped == 40
    assert len(relay._queue) == MAX_QUEUED_EVENTS  # noqa: SLF001 - white-box on purpose


def test_a_batch_never_exceeds_the_ingest_routes_cap():
    post = _Recorder()
    relay = _relay(post)
    for i in range(MAX_BATCH + 30):
        relay.enqueue("token", {"text": str(i)})
    assert relay.flush_once() == MAX_BATCH
    assert relay.flush_once() == 30


def test_a_dead_server_is_swallowed_and_counted_never_raised():
    post = _Recorder(fail=True)
    relay = _relay(post)
    relay.enqueue("token", {"text": "x"})
    assert relay.flush_once() == 0  # no raise
    assert relay.post_failures == 1
    assert relay.posted == 0


def test_the_terminal_event_reports_the_relays_own_losses():
    """ "The model said nothing" and "the relay dropped it" must be
    distinguishable by the follower, or a lossy watch looks like a bad turn."""
    post = _Recorder()
    relay = _relay(post)
    for i in range(MAX_QUEUED_EVENTS + 5):
        relay.enqueue("token", {"text": str(i)})
    relay._queue.clear()  # noqa: SLF001 - simulate a flushed queue
    relay.finished({"ok": True})
    relay.flush_once()
    terminal = post.events[-1]
    assert terminal["kind"] == "turn_finished"
    assert terminal["payload"]["ok"] is True
    assert terminal["payload"]["relay"]["dropped"] == 5


def test_failed_is_terminal_too():
    post = _Recorder()
    relay = _relay(post)
    relay.failed({"error_code": "quota"})
    relay.flush_once()
    assert post.events[-1]["kind"] == "turn_failed"


def test_a_malformed_event_object_is_ignored_not_raised():
    relay = _relay(_Recorder())
    relay.publish_stream_event(object())
    relay.enqueue("", {"text": "x"})
    assert len(relay._queue) == 0  # noqa: SLF001


def test_the_background_flusher_drains_and_stops(monkeypatch):
    post = _Recorder()
    relay = _relay(post, flush_interval_s=0.01).start()
    for i in range(5):
        relay.enqueue("token", {"text": str(i)})
    relay.finished({"ok": True})
    relay.close(timeout_s=5.0)
    assert [e["kind"] for e in post.events][-1] == "turn_finished"
    assert relay.posted >= 6


def test_relay_from_env_is_off_outside_a_worker_and_when_disabled(monkeypatch):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.delenv("CAO_CHATGPT_TURN_RELAY", raising=False)
    assert relay_from_env("abc") is None

    monkeypatch.setenv("CAO_TERMINAL_ID", "worker-1")
    monkeypatch.setenv("CAO_TERMINAL_TOKEN", "tok")
    monkeypatch.setenv("CAO_CHATGPT_TURN_RELAY", "0")
    assert relay_from_env("abc") is None

    monkeypatch.setenv("CAO_CHATGPT_TURN_RELAY", "1")
    relay = relay_from_env("abc", post=_Recorder())
    assert relay is not None
    relay.close(timeout_s=2.0)
