"""F970 (#819) — the turn relay's two routes: publish (worker-bound) and follow.

The relay only earns its keep if (a) nothing but the worker that owns the turn
can publish under its name, and (b) a follower can reconnect and resume exactly
where it left off. Both are asserted here against the real FastAPI app.
"""

from __future__ import annotations

import json

import pytest

from cli_agent_orchestrator.services import chatgpt_turn_stream as cts

pytestmark = pytest.mark.integration

_TERMINAL = "worker-1"
_TURN = "0123456789abcdef"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cts.reset_turn_streams()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_token_service.verify_sender_token",
        lambda _db, terminal_id, token: (token == f"tok-{terminal_id}", None),
    )
    yield
    cts.reset_turn_streams()


def _publish(client, events, *, terminal=_TERMINAL, token=None, turn=_TURN):
    headers = {"X-CAO-Terminal-Token": token if token is not None else f"tok-{terminal}"}
    return client.post(
        f"/terminals/{terminal}/chatgpt/turns/{turn}/events",
        json={"events": events},
        headers=headers,
    )


def _frames(body: str):
    """Parse an SSE body into (event, data, id) triples."""
    out = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name = data = ident = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line.startswith("id: "):
                ident = int(line[4:])
        out.append((name, data, ident))
    return out


def test_publishing_requires_the_owning_worker_token_not_merely_a_scope(client):
    """Scope is never identity (the F707/F829 rule): without the worker's own
    token, anything on the box could fabricate a turn stream under its name."""
    missing = client.post(
        f"/terminals/{_TERMINAL}/chatgpt/turns/{_TURN}/events",
        json={"events": [{"kind": "token", "payload": {"text": "x"}}]},
    )
    assert missing.status_code == 401

    wrong = _publish(
        client, [{"kind": "token", "payload": {"text": "x"}}], token="tok-someone-else"
    )
    assert wrong.status_code == 403
    assert cts.get_turn_streams().turn_ids() == []


def test_publish_then_follow_replays_with_seq_ids_and_closes_on_terminal(client):
    resp = _publish(
        client,
        [
            {"kind": "turn_started", "payload": {"artifact_path": "/x.md"}},
            {"kind": "token", "payload": {"text": "Hel"}},
            {"kind": "token", "payload": {"text": "lo"}},
            {"kind": "quota", "payload": {"quota": [{"feature_name": "file_upload"}]}},
            {"kind": "turn_finished", "payload": {"ok": True}},
        ],
    )
    assert resp.status_code == 200
    assert resp.json() == {"turn_id": _TURN, "accepted": 5, "last_seq": 5}

    # A terminal event is already in the ring, so the follow stream replays and
    # closes rather than hanging — that is what makes this testable at all.
    stream = client.get(f"/chatgpt/turns/{_TURN}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    frames = _frames(stream.text)
    assert [f[0] for f in frames] == ["turn_started", "token", "token", "quota", "turn_finished"]
    assert [f[2] for f in frames] == [1, 2, 3, 4, 5]
    assert frames[1][1]["text"] == "Hel"


def test_after_seq_and_last_event_id_both_resume_and_the_explicit_one_wins(client):
    _publish(
        client,
        [{"kind": "token", "payload": {"text": str(i)}} for i in range(4)]
        + [{"kind": "turn_finished", "payload": {}}],
    )
    by_query = _frames(client.get(f"/chatgpt/turns/{_TURN}/events?after_seq=3").text)
    assert [f[2] for f in by_query] == [4, 5]

    by_header = _frames(
        client.get(f"/chatgpt/turns/{_TURN}/events", headers={"Last-Event-ID": "4"}).text
    )
    assert [f[2] for f in by_header] == [5]

    both = _frames(
        client.get(
            f"/chatgpt/turns/{_TURN}/events?after_seq=1", headers={"Last-Event-ID": "4"}
        ).text
    )
    assert [f[2] for f in both] == [2, 3, 4, 5]


def test_a_follower_behind_the_ring_gets_a_gap_frame_before_the_events(client):
    store = cts.get_turn_streams()
    for i in range(cts.TURN_RING_CAPACITY + 10):
        store.append(_TURN, "token", {"text": str(i)}, terminal_id=_TERMINAL)
    store.append(_TURN, "turn_finished", {}, terminal_id=_TERMINAL)

    frames = _frames(client.get(f"/chatgpt/turns/{_TURN}/events?after_seq=2").text)
    assert frames[0][0] == "gap"
    assert frames[0][1]["after_seq"] == 2
    assert frames[0][1]["missing_count"] > 0
    assert frames[0][2] is None
    assert frames[1][0] == "token"


def test_the_non_streaming_read_returns_the_same_events_as_json(client):
    _publish(
        client,
        [{"kind": "token", "payload": {"text": "a"}}, {"kind": "turn_finished", "payload": {}}],
    )
    body = client.get(f"/chatgpt/turns/{_TURN}/events?stream=false").json()
    assert body["known"] is True and body["ended"] is True
    assert [e["kind"] for e in body["events"]] == ["token", "turn_finished"]
    after = client.get(f"/chatgpt/turns/{_TURN}/events?stream=false&after_seq=1").json()
    assert [e["seq"] for e in after["events"]] == [2]


def test_an_unknown_turn_reads_as_unknown_rather_than_404(client):
    body = client.get("/chatgpt/turns/does-not-exist/events?stream=false").json()
    assert body == {
        "turn_id": "does-not-exist",
        "known": False,
        "ended": False,
        "terminal_id": "",
        "events": [],
        "gap": None,
    }


def test_a_path_shaped_turn_id_is_rejected_on_both_routes(client):
    assert client.get("/chatgpt/turns/..%2F..%2Fetc/events?stream=false").status_code in (400, 404)
    bad = _publish(client, [{"kind": "token"}], turn="..")
    assert bad.status_code in (400, 404)


def test_an_oversized_batch_is_refused_whole(client):
    resp = _publish(client, [{"kind": "token", "payload": {"text": "x"}}] * 201)
    assert resp.status_code == 413
    assert cts.get_turn_streams().turn_ids() == []


def test_lifecycle_events_are_mirrored_to_the_fleet_bus_but_tokens_are_not(client, monkeypatch):
    published = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.sse_bus.get_bus",
        lambda: type("B", (), {"publish": staticmethod(published.append)})(),
    )
    _publish(
        client,
        [
            {"kind": "turn_started", "payload": {}},
            {"kind": "token", "payload": {"text": "noise"}},
            {"kind": "quota", "payload": {"quota": []}},
            {"kind": "turn_finished", "payload": {"ok": True}},
        ],
    )
    kinds = [p["detail"]["kind"] for p in published]
    assert kinds == ["turn_started", "quota", "turn_finished"]
    assert all(p["kind"] == "chatgpt_turn" for p in published)
    assert "noise" not in json.dumps(published)
