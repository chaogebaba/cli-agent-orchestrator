"""The herdr socket transport leaf — unit tests (WP-HERDR H1, #702).

These drive :class:`HerdrClient` against a FAKE in-process herdr server: an
``asyncio`` unix-socket server that speaks the same newline-delimited JSON-RPC
the real herdr socket does, scripted per test.  No real herdr binary, no
subprocess — the transport is exercised against herdr's real 0.9.0 wire shapes
replayed from ``test/fixtures/herdr/`` (see that dir's ``PROVENANCE.md``).

The fake server matters as much as the client: the request/reply match, the
single-subscribe rule, the protocol-pin refusal and the gap-on-close behaviour
are all properties of how the client talks to a socket, and a double that did
not actually round-trip bytes over one would test none of them.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

import pytest

from cli_agent_orchestrator.adapters.herdr import client as herdr_client
from cli_agent_orchestrator.adapters.herdr.client import (
    HERDR_PROTOCOL,
    HERDR_SCHEMA_VERSION,
    HerdrClient,
    HerdrProtocolMismatch,
    HerdrRequestError,
    HerdrTransportError,
    default_socket_path,
)

FIXTURES = Path(__file__).resolve().parents[3] / "test" / "fixtures" / "herdr"

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fake herdr server
# --------------------------------------------------------------------------

Handler = Callable[["FakeHerdrServer", dict[str, Any]], Awaitable[None]]


class FakeHerdrServer:
    """A scriptable herdr socket server for one connection.

    ``on_request`` is called for every JSON-RPC line the client sends; the
    handler writes whatever replies/pushes the test wants via :meth:`reply`,
    :meth:`error` and :meth:`push`.  The default handler answers ``api.schema``
    with the pinned protocol and acks ``events.subscribe`` — enough for the happy
    path — and a test overrides it for the edge cases.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self.requests: list[dict[str, Any]] = []
        self.on_request: Handler = FakeHerdrServer._default_handler

    async def __aenter__(self) -> "FakeHerdrServer":
        self._server = await asyncio.start_unix_server(self._serve, path=self._socket_path)
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Force the connection down rather than relying on ``wait_closed()``:
        # on Python 3.13+ ``Server.wait_closed()`` blocks until every active
        # connection handler returns, and ``_serve`` is parked on ``readline()``
        # — so a plain ``close()``/``wait_closed()`` hangs the test.  Cancel the
        # handler and close the writer, THEN close the server.
        if self._serve_task is not None:
            self._serve_task.cancel()
            try:
                await self._serve_task
            except (asyncio.CancelledError, Exception):
                pass
            self._serve_task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._serve_task = asyncio.current_task()
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            request = json.loads(text)
            self.requests.append(request)
            await self.on_request(self, request)

    async def reply(self, request_id: str, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def error(self, request_id: str, code: str, message: str) -> None:
        await self._write({"id": request_id, "error": {"code": code, "message": message}})

    async def push(self, event: dict[str, Any]) -> None:
        await self._write(event)

    async def close_connection(self) -> None:
        if self._writer is not None:
            self._writer.close()

    async def _write(self, obj: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write(json.dumps(obj).encode() + b"\n")
        await self._writer.drain()

    async def _default_handler(self, request: dict[str, Any]) -> None:
        method = request.get("method")
        request_id = request["id"]
        if method == "api.schema":
            await self.reply(
                request_id,
                {"protocol": HERDR_PROTOCOL, "schema_version": HERDR_SCHEMA_VERSION},
            )
        elif method == "events.subscribe":
            await self.reply(request_id, {"type": "subscription_started"})
        elif method == "api.snapshot":
            await self.reply(request_id, {"snapshot": {"panes": []}})
        else:
            await self.reply(request_id, {})


@pytest.fixture
def socket_path(tmp_path: Path) -> Iterator[str]:
    # AF_UNIX paths are capped at ~108 bytes and a pytest tmp path (especially
    # under a box's long ``--basetemp``) can exceed it.  Bind under a SHORT,
    # unique, absolute dir instead — ``/dev/shm`` when present (Linux tmpfs),
    # else a short ``/tmp`` dir — so the path is well under the limit and does
    # not depend on ``chdir`` (which is unsafe under xdist's shared cwd).  The
    # dir is removed after the test.
    short_root = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
    d = Path(tempfile.mkdtemp(prefix="hc", dir=str(short_root)))
    try:
        yield str(d / "s")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# connect / close
# --------------------------------------------------------------------------


async def test_connect_and_close_round_trip(socket_path: str) -> None:
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        assert client.connected is True
        await client.close()
        assert client.connected is False


async def test_connect_is_idempotent(socket_path: str) -> None:
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        await client.connect()  # second call is a no-op, not a churn
        assert client.connected is True
        await client.close()


async def test_connect_to_absent_socket_raises_transport_error(socket_path: str) -> None:
    client = HerdrClient(socket_path, connect_timeout_s=0.5)
    with pytest.raises(HerdrTransportError):
        await client.connect()
    assert client.connected is False


async def test_close_is_safe_when_never_connected(socket_path: str) -> None:
    client = HerdrClient(socket_path)
    await client.close()  # no raise
    assert client.connected is False


# --------------------------------------------------------------------------
# request / reply matching and error bodies
# --------------------------------------------------------------------------


async def test_request_returns_result_body(socket_path: str) -> None:
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        result = await client.request("api.snapshot")
        assert result == {"snapshot": {"panes": []}}
        await client.close()


async def test_request_error_body_becomes_request_error(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.error(request["id"], "pane_not_found", "no such pane")

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrRequestError) as excinfo:
            await client.request("pane.get", {"pane_id": "wX:pY"})
        assert excinfo.value.code == "pane_not_found"
        assert "no such pane" in excinfo.value.message
        await client.close()


async def test_request_buffers_a_pushed_event_and_matches_by_id(socket_path: str) -> None:
    """A pushed event (no id) that arrives before the reply must not be mistaken
    for the reply; the request reads the line carrying its own id, and the event
    is BUFFERED for the stream (§6 reconcile) rather than dropped."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        # An unsolicited event first, then the real reply.
        await server.push({"event": "pane_updated", "data": {"pane": {"n": 1}}})
        await server.reply(request["id"], {"ok": True})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        result = await client.request("pane.get")
        assert result == {"ok": True}
        # The racing event was held, not dropped: it is the first thing the
        # event stream yields.
        assert list(client._event_buffer) == [{"event": "pane_updated", "data": {"pane": {"n": 1}}}]
        await client.close()


async def test_event_between_subscribe_and_snapshot_is_delivered_once_in_order(
    socket_path: str,
) -> None:
    """§6 race: a pane.updated that interleaves between the subscribe ack and the
    snapshot reply is BUFFERED and replayed into the stream after the snapshot,
    exactly once and in order, ahead of later live events."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        method = request.get("method")
        request_id = request["id"]
        if method == "events.subscribe":
            await server.reply(request_id, {"type": "subscription_started"})
            # An event races in AFTER the ack but BEFORE the snapshot request.
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
        elif method == "api.snapshot":
            await server.reply(request_id, {"snapshot": {"panes": []}})
            # A live event that arrives AFTER the snapshot.
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 2}}})
            await server.close_connection()
        else:
            await server.reply(request_id, {})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        await client.snapshot()
        seen: list[int] = []
        with pytest.raises(HerdrTransportError):
            async for event in client.events():
                seen.append(int(event["data"]["pane"]["seq"]))
        # The buffered (seq=1) event replays first, then the live (seq=2) one —
        # in order, each exactly once.
        assert seen == [1, 2]
        await client.close()


async def test_request_times_out_when_server_never_answers(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        return  # swallow the request, never reply

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path, request_timeout_s=0.3)
        await client.connect()
        with pytest.raises(HerdrTransportError):
            await client.request("api.snapshot")
        await client.close()


async def test_reply_with_neither_result_nor_error_is_a_transport_error(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.push({"id": request["id"]})  # malformed: no result, no error

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrTransportError):
            await client.request("api.snapshot")
        await client.close()


# --------------------------------------------------------------------------
# protocol pin (D7)
# --------------------------------------------------------------------------


async def test_check_protocol_accepts_the_pinned_version(socket_path: str) -> None:
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        result = await client.check_protocol()
        assert result["protocol"] == HERDR_PROTOCOL
        assert result["schema_version"] == HERDR_SCHEMA_VERSION
        await client.close()


async def test_check_protocol_refuses_a_drifted_protocol(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        # A future herdr on a different protocol.
        await server.reply(request["id"], {"protocol": 23, "schema_version": 1})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrProtocolMismatch) as excinfo:
            await client.check_protocol()
        assert excinfo.value.got_protocol == 23
        await client.close()


async def test_check_protocol_matches_the_fixture_schema_head() -> None:
    """The pin equals what the real 0.9.0 ``api schema`` reported (fixture)."""
    head = json.loads((FIXTURES / "api-schema-head.json").read_text())
    assert head["protocol"] == HERDR_PROTOCOL
    assert head["schema_version"] == HERDR_SCHEMA_VERSION


# --------------------------------------------------------------------------
# subscribe (single-subscribe rule) + event stream
# --------------------------------------------------------------------------


async def test_subscribe_returns_the_ack(socket_path: str) -> None:
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        ack = await client.subscribe([{"type": "pane.updated"}])
        assert ack == {"type": "subscription_started"}
        await client.close()


async def test_subscribe_ack_matches_the_real_fixture(socket_path: str) -> None:
    """The ack shape is herdr's real one (``p2/pi/subscribe-ack.json``)."""
    fixture = json.loads((FIXTURES / "subscribe-ack.json").read_text())
    expected = fixture["ack"]["result"]
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        ack = await client.subscribe([{"type": "pane.updated"}])
        assert ack == expected
        await client.close()


async def test_a_second_subscribe_is_refused(socket_path: str) -> None:
    """herdr resets the connection on a second events.subscribe; the client
    refuses it before it can trigger that reset."""
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        with pytest.raises(herdr_client.HerdrError):
            await client.subscribe([{"type": "pane.closed"}])
        await client.close()


async def test_events_requires_a_prior_subscribe(socket_path: str) -> None:
    async with FakeHerdrServer(socket_path):
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(herdr_client.HerdrError):
            async for _ in client.events():  # pragma: no cover - should raise before first yield
                break
        await client.close()


async def test_events_streams_pushed_events_then_ends_on_close(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "events.subscribe":
            await server.reply(request["id"], {"type": "subscription_started"})
            await server.push({"event": "pane_updated", "data": {"pane": {"pane_id": "w2:p1"}}})
            await server.push({"event": "pane_updated", "data": {"pane": {"pane_id": "w2:p2"}}})
            await server.close_connection()

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        seen: list[dict[str, Any]] = []
        with pytest.raises(HerdrTransportError):
            async for event in client.events():
                seen.append(event)
        assert [e["data"]["pane"]["pane_id"] for e in seen] == ["w2:p1", "w2:p2"]
        await client.close()


async def test_events_replays_the_real_pi_event_stream(socket_path: str) -> None:
    """Replay ``test/fixtures/herdr/pi-events.jsonl`` — herdr's real broadcast
    stream — through the client and confirm every pane.updated is yielded."""
    lines = [
        json.loads(line)
        for line in (FIXTURES / "pi-events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # The first line is the subscribe ack; the rest are pushed events.
    ack, *pushed = lines

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "events.subscribe":
            await server.reply(request["id"], ack["result"])
            for event in pushed:
                await server.push(event)
            await server.close_connection()

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        seen: list[dict[str, Any]] = []
        with pytest.raises(HerdrTransportError):
            async for event in client.events():
                seen.append(event)
        pane_updated = [e for e in seen if e.get("event") == "pane_updated"]
        assert pane_updated, "the real stream carries pane_updated events"
        assert len(seen) == len(pushed)


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


async def test_snapshot_returns_the_snapshot_body(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.reply(request["id"], {"snapshot": {"panes": [{"pane_id": "w1:p1"}]}})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        snapshot = await client.snapshot()
        assert snapshot["panes"] == [{"pane_id": "w1:p1"}]
        await client.close()


async def test_snapshot_without_a_snapshot_body_raises(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.reply(request["id"], {"type": "session_snapshot"})  # no 'snapshot'

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrTransportError):
            await client.snapshot()
        await client.close()


# --------------------------------------------------------------------------
# default_socket_path — byte-identical to the two legacy copies
# --------------------------------------------------------------------------


async def test_default_socket_path_named_session() -> None:
    assert default_socket_path("cao", config_home="/c") == "/c/herdr/sessions/cao/herdr.sock"


async def test_default_socket_path_default_session_is_flat() -> None:
    assert default_socket_path("default", config_home="/c") == "/c/herdr/herdr.sock"


async def test_default_socket_path_matches_legacy_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client's resolution equals the former inline backend/inbox logic."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    for session in ("cao", "default", "p0-r2"):
        if session == "default":
            legacy = "/xdg/herdr/herdr.sock"
        else:
            legacy = f"/xdg/herdr/sessions/{session}/herdr.sock"
        assert default_socket_path(session) == legacy


async def test_adj_r4_event_pushed_BEFORE_the_subscribe_ack_is_buffered(
    socket_path: str,
) -> None:
    """S-2 (Opus r2, kills mutant M8): subscribe()'s own buffer branch — an event
    that arrives AHEAD of the subscription ack must be held for events(), not
    dropped.  The shipped race test and both r2 races push the racing event AFTER
    the ack, so only request()'s buffer was covered; this covers subscribe()'s.
    Verbatim from the reviewer probe
    /data/cao-scratch/herdr-adj-r2/probes/test_adj_r2_race.py."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        method = request.get("method")
        rid = request["id"]
        if method == "api.schema":
            await server.reply(
                rid, {"protocol": HERDR_PROTOCOL, "schema_version": HERDR_SCHEMA_VERSION}
            )
        elif method == "events.subscribe":
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
            await server.reply(rid, {"type": "subscription_started"})
        elif method == "api.snapshot":
            await server.reply(rid, {"snapshot": {"panes": []}})
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 2}}})
            await server.close_connection()
        else:
            await server.reply(rid, {})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        await client.snapshot()
        seen: list[int] = []
        with pytest.raises(HerdrTransportError):
            async for event in client.events():
                seen.append(int(event["data"]["pane"]["seq"]))
        assert seen == [1, 2]
        await client.close()


async def test_adj_r1_event_arrives_during_the_snapshot_response_itself(
    socket_path: str,
) -> None:
    """Ordering race (Opus r2, contributes to killing mutant M6): the racing
    events are pushed BEFORE the snapshot reply (inside the snapshot round trip),
    the tighter half of the §6 window; they must replay in ARRIVAL order ahead of
    the live event.  Verbatim from the reviewer probe test_adj_r2_race.py."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        method = request.get("method")
        rid = request["id"]
        if method == "api.schema":
            await server.reply(
                rid, {"protocol": HERDR_PROTOCOL, "schema_version": HERDR_SCHEMA_VERSION}
            )
        elif method == "events.subscribe":
            await server.reply(rid, {"type": "subscription_started"})
        elif method == "api.snapshot":
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 2}}})
            await server.reply(rid, {"snapshot": {"panes": []}})
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 3}}})
            await server.close_connection()
        else:
            await server.reply(rid, {})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        await client.snapshot()
        seen: list[int] = []
        with pytest.raises(HerdrTransportError):
            async for event in client.events():
                seen.append(int(event["data"]["pane"]["seq"]))
        assert seen == [1, 2, 3]
        await client.close()


async def test_adj_r2_burst_of_fifty_interleaved_events_no_drop_dup_or_reorder(
    socket_path: str,
) -> None:
    """50 events straddling the handshake window: exactly once, in order — the
    shipped test that KILLS mutant M6 (LIFO replay), because it buffers many
    events whose order a reorder would break.  Verbatim from the reviewer probe
    test_adj_r2_race.py."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        method = request.get("method")
        rid = request["id"]
        if method == "api.schema":
            await server.reply(
                rid, {"protocol": HERDR_PROTOCOL, "schema_version": HERDR_SCHEMA_VERSION}
            )
        elif method == "events.subscribe":
            await server.reply(rid, {"type": "subscription_started"})
            for i in range(1, 21):  # 20 between ack and snapshot request
                await server.push({"event": "pane_updated", "data": {"pane": {"seq": i}}})
        elif method == "api.snapshot":
            for i in range(21, 41):  # 20 more inside the snapshot round trip
                await server.push({"event": "pane_updated", "data": {"pane": {"seq": i}}})
            await server.reply(rid, {"snapshot": {"panes": []}})
            for i in range(41, 51):  # 10 live, after the snapshot
                await server.push({"event": "pane_updated", "data": {"pane": {"seq": i}}})
            await server.close_connection()
        else:
            await server.reply(rid, {})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        await client.snapshot()
        seen: list[int] = []
        with pytest.raises(HerdrTransportError):
            async for event in client.events():
                seen.append(int(event["data"]["pane"]["seq"]))
        assert len(seen) == 50, f"drop/dup: {len(seen)}"
        assert len(set(seen)) == 50, "duplicates"
        assert seen == sorted(seen), f"reorder: {seen}"
        assert seen == list(range(1, 51))
        await client.close()


async def test_adj_r3_buffer_is_cleared_on_close_no_cross_connection_replay(
    socket_path: str,
) -> None:
    """The buffer must not survive a close() (no cross-connection replay).
    Verbatim from the reviewer probe test_adj_r2_race.py."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        method = request.get("method")
        rid = request["id"]
        if method == "api.schema":
            await server.reply(
                rid, {"protocol": HERDR_PROTOCOL, "schema_version": HERDR_SCHEMA_VERSION}
            )
        elif method == "events.subscribe":
            await server.reply(rid, {"type": "subscription_started"})
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
        elif method == "api.snapshot":
            await server.reply(rid, {"snapshot": {"panes": []}})
        else:
            await server.reply(rid, {})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        await client.snapshot()
        assert len(client._event_buffer) == 1
        await client.close()
        assert len(client._event_buffer) == 0
