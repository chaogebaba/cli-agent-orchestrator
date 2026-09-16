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
    HERDR_AGENT_BLOCKED,
    HERDR_AGENT_PROMPT_STALLED,
    HERDR_PROMPT_ACK_STATES,
    HERDR_PROTOCOL,
    HERDR_SCHEMA_VERSION,
    HERDR_TIMEOUT,
    HerdrClient,
    HerdrProtocolMismatch,
    HerdrRequestError,
    HerdrTransportError,
    default_socket_path,
)
from cli_agent_orchestrator.core.delivery import AttemptOutcome

FIXTURES = Path(__file__).resolve().parents[3] / "test" / "fixtures" / "herdr"

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fake herdr server
# --------------------------------------------------------------------------

Handler = Callable[["FakeHerdrServer", dict[str, Any]], Awaitable[None]]


class FakeHerdrServer:
    """A scriptable herdr socket server, correct across MULTIPLE connections.

    It used to serve one connection and keep a single ``_writer``, which every
    reply went to.  That is wrong now that the client opens a second, short-lived
    connection for each request/reply (``HerdrClient.request_once``, forced by
    herdr 0.9.0 accepting a subscription only as a connection's first message):
    the second connection clobbered ``_writer``, so the STREAMING connection's
    next reply was written to a socket its client was not reading, and the client
    waited forever.  Under ``-n 2`` that surfaced as a worker parked in
    ``asyncio.run`` and an xdist run stuck at 99% — the tests themselves passed
    when run alone, because the clobber is a race.

    So: replies go to the connection the request ARRIVED on, and every
    connection's handler task and writer is tracked so teardown closes them all
    (a handler left parked on ``readline`` is what kept the interpreter alive).
    ``push_stream`` writes to the FIRST connection — the one a test subscribed on
    — for the cases that need an unsolicited event to reach the event stream.

    ``on_request`` is called for every JSON-RPC line the client sends; the
    handler writes whatever replies/pushes the test wants via :meth:`reply`,
    :meth:`error` and :meth:`push`.  The default handler acks
    ``events.subscribe`` and answers ``session.snapshot`` with the pinned
    protocol — enough for the happy path, since 0.9.0 carries the protocol in the
    snapshot and has no schema method — and a test overrides it for the edge
    cases.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        #: The connection the request being handled arrived on.  ``reply`` /
        #: ``error`` / ``push`` target it, which is what every handler means.
        self._writer: asyncio.StreamWriter | None = None
        #: The FIRST connection — the streaming one in every test that subscribes.
        self._stream_writer: asyncio.StreamWriter | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._serve_tasks: list[asyncio.Task[None]] = []
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
        for task in self._serve_tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._serve_tasks.clear()
        for writer in self._writers:
            try:
                writer.close()
            except Exception:
                pass
        self._writers.clear()
        self._writer = None
        self._stream_writer = None
        if self._server is not None:
            self._server.close()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        if self._stream_writer is None:
            self._stream_writer = writer
        task = asyncio.current_task()
        if task is not None:
            self._serve_tasks.append(task)
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            request = json.loads(text)
            self.requests.append(request)
            # Per REQUEST, not per connection: a handler's ``reply`` must reach
            # the client that asked, even while another connection is open.
            self._writer = writer
            await self.on_request(self, request)

    async def reply(self, request_id: str, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def error(self, request_id: str, code: str, message: str) -> None:
        await self._write({"id": request_id, "error": {"code": code, "message": message}})

    async def push(self, event: dict[str, Any]) -> None:
        """Push an unsolicited event on the connection being handled."""
        await self._write(event)

    async def push_stream(self, event: dict[str, Any]) -> None:
        """Push on the FIRST connection — the one the client subscribed on.

        A handler running for a ``request_once`` connection has to name the
        stream explicitly; ``push`` would send the event to a socket the client
        is about to close and never read.
        """
        writer = self._stream_writer
        if writer is None:
            return
        writer.write(json.dumps(event).encode() + b"\n")
        await writer.drain()

    async def close_connection(self) -> None:
        """Drop the connection being handled."""
        if self._writer is not None:
            self._writer.close()

    async def close_stream(self) -> None:
        """Drop the FIRST connection — the one the client is streaming on."""
        if self._stream_writer is not None:
            self._stream_writer.close()

    async def _write(self, obj: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write(json.dumps(obj).encode() + b"\n")
        await self._writer.drain()

    async def _default_handler(self, request: dict[str, Any]) -> None:
        method = request.get("method")
        request_id = request["id"]
        if method == "events.subscribe":
            await self.reply(request_id, {"type": "subscription_started"})
        elif method == "session.snapshot":
            # herdr 0.9.0 carries the protocol number IN the snapshot; there is
            # no separate schema method, so ``check_protocol`` reads this reply.
            await self.reply(
                request_id,
                {
                    "snapshot": {
                        "panes": [],
                        "protocol": HERDR_PROTOCOL,
                        "schema_version": HERDR_SCHEMA_VERSION,
                    }
                },
            )
        else:
            await self.reply(request_id, {})


def _snapshot_body(panes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A ``session.snapshot`` body carrying the pinned protocol numbers.

    herdr 0.9.0 reports the protocol IN the snapshot and has no separate schema
    method, so every fake that answers ``session.snapshot`` must carry the pin or
    ``check_protocol`` — which now reads it from here — sees a mismatch.
    """
    return {
        "panes": list(panes or []),
        "protocol": HERDR_PROTOCOL,
        "schema_version": HERDR_SCHEMA_VERSION,
    }


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
        result = await client.request("session.snapshot")
        assert result == {"snapshot": _snapshot_body()}
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
        elif method == "session.snapshot":
            await server.reply(request_id, {"snapshot": _snapshot_body()})
            # A live event that arrives AFTER the snapshot, ON THE STREAM. The
            # snapshot now travels on its own short-lived connection
            # (``request_once``), so an event meant for the subscriber has to
            # name the stream rather than reply-channel.
            await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": 2}}})
            await server.close_stream()
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
            await client.request("session.snapshot")
        await client.close()


async def test_reply_with_neither_result_nor_error_is_a_transport_error(socket_path: str) -> None:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.push({"id": request["id"]})  # malformed: no result, no error

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrTransportError):
            await client.request("session.snapshot")
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
        # A future herdr on a different protocol, reporting it where 0.9.0 does:
        # inside the ``session.snapshot`` body.
        await server.reply(
            request["id"],
            {"snapshot": {"panes": [], "protocol": 23, "schema_version": 1}},
        )

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrProtocolMismatch) as excinfo:
            await client.check_protocol()
        assert excinfo.value.got_protocol == 23
        await client.close()


async def test_check_protocol_matches_the_fixture_schema_head() -> None:
    """The pin equals what the real 0.9.0 schema head reported (fixture).

    The fixture is kept as the record of where the numbers came from; the LIVE
    read is now ``session.snapshot``, because 0.9.0's API socket has no
    ``api.schema`` method and closes the connection on one.
    """
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
        if method == "events.subscribe":
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
            await server.reply(rid, {"type": "subscription_started"})
        elif method == "session.snapshot":
            await server.reply(rid, {"snapshot": _snapshot_body()})
            await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": 2}}})
            await server.close_stream()
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
        if method == "events.subscribe":
            await server.reply(rid, {"type": "subscription_started"})
        elif method == "session.snapshot":
            await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
            await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": 2}}})
            await server.reply(rid, {"snapshot": _snapshot_body()})
            await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": 3}}})
            await server.close_stream()
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
        if method == "events.subscribe":
            await server.reply(rid, {"type": "subscription_started"})
            for i in range(1, 21):  # 20 between ack and snapshot request
                await server.push({"event": "pane_updated", "data": {"pane": {"seq": i}}})
        elif method == "session.snapshot":
            for i in range(21, 41):  # 20 more inside the snapshot round trip
                await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": i}}})
            await server.reply(rid, {"snapshot": _snapshot_body()})
            for i in range(41, 51):  # 10 live, after the snapshot
                await server.push_stream({"event": "pane_updated", "data": {"pane": {"seq": i}}})
            await server.close_stream()
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
    From the reviewer probe test_adj_r2_race.py.

    The racing push moved AHEAD of the subscribe ack, and the snapshot call is
    gone.  Both follow from the snapshot travelling on its own short-lived
    connection now (``request_once``): only a request on the STREAM connection
    can race a pushed event into ``_event_buffer``, and ``subscribe`` is the one
    such request.  An event pushed after the ack simply waits in the socket until
    ``events()`` reads it — correct, and not what this test is about.  The
    property under test is unchanged: whatever the buffer holds, ``close()``
    drops it.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        method = request.get("method")
        rid = request["id"]
        if method == "events.subscribe":
            # AHEAD of the ack, so ``subscribe`` buffers it on the way past.
            await server.push({"event": "pane_updated", "data": {"pane": {"seq": 1}}})
            await server.reply(rid, {"type": "subscription_started"})
        else:
            await server.reply(rid, {})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        assert len(client._event_buffer) == 1
        await client.close()
        assert len(client._event_buffer) == 0


async def test_subscribe_must_be_the_first_message_on_the_connection(socket_path: str) -> None:
    """N5: the rule ``request_once`` exists for, pinned.

    herdr 0.9.0 accepts ``events.subscribe`` only as a connection's FIRST
    message; after a plain request it answers by resetting the connection. So a
    client that means to stream must keep its connection clean, and the protocol
    read and the snapshot have to happen somewhere else. This asserts the shape
    rather than the server's reaction: on the STREAMING connection the client
    sends exactly one message before ``events.subscribe``, namely nothing.
    """
    seen: list[tuple[bool, str]] = []

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        # The fake points ``_writer`` at the connection the request ARRIVED on,
        # and ``_stream_writer`` at the first connection ever opened — the one
        # ``client.connect()`` made and the one the client streams on. Counting
        # connections instead would be wrong: ``check_protocol`` opens its
        # one-shot BEFORE the subscribe, so the count is already 2 by then.
        on_stream = server._writer is server._stream_writer
        seen.append((on_stream, str(request.get("method"))))
        await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.check_protocol()
        await client.subscribe([{"type": "pane.updated"}])
        await client.snapshot()
        await client.close()

    # The protocol read and the snapshot each opened their OWN connection, so by
    # the time the stream's subscribe arrives more than one connection exists —
    # and the FIRST connection carried no request before it.
    stream_methods = [method for on_stream, method in seen if on_stream]
    assert stream_methods == [
        "events.subscribe"
    ], "the streaming connection carries the subscribe and nothing else"
    off_stream = [method for on_stream, method in seen if not on_stream]
    assert off_stream == [
        "session.snapshot",
        "session.snapshot",
    ], "check_protocol and snapshot each take a one-shot connection"


async def test_an_absent_schema_version_does_not_auto_match_the_pin(socket_path: str) -> None:
    """N6: 0.9.0 sends no ``schema_version``, and r1 defaulted the missing value
    TO the pin — so the D7 check asserted something it had never read. An absent
    schema is not a mismatch, but the pin it satisfies is ``protocol`` alone."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.reply(request["id"], {"snapshot": {"panes": [], "protocol": HERDR_PROTOCOL}})

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        snapshot = await client.check_protocol()
        assert snapshot["protocol"] == HERDR_PROTOCOL
        assert "schema_version" not in snapshot
        await client.close()


async def test_a_present_but_wrong_schema_version_still_refuses(socket_path: str) -> None:
    """The other half of N6: a future herdr that reintroduces the field is still
    checked, rather than silently accepted."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.reply(
            request["id"],
            {"snapshot": {"panes": [], "protocol": HERDR_PROTOCOL, "schema_version": 99}},
        )

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        with pytest.raises(HerdrProtocolMismatch):
            await client.check_protocol()
        await client.close()


# --------------------------------------------------------------------------
# H2-S2 — the agent-prompt verb and its mapping into the delivery vocabulary.
#
# Every test here asserts TWO things where it can: the outcome, and that exactly
# ONE ``agent.prompt`` went down the wire.  The second is the property that
# matters more — a transport that retried internally would double-submit into a
# human-visible composer, and no outcome assertion would notice.
# --------------------------------------------------------------------------


def _prompts(server: FakeHerdrServer) -> list[dict[str, Any]]:
    return [r for r in server.requests if r.get("method") == "agent.prompt"]


def _pre_state(status: str = "idle", seq: int = 1) -> Handler:
    """A handler arm answering the pre-submission ``agent.get``.

    Every prompt now reads the agent's state first, because the live arm showed
    a success reply is not evidence when the agent was already in an ack state.
    So the fake has to answer it, and the tests say WHAT it answers — the
    pre-state is an input to the mapping, not scaffolding.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        await server.reply(
            request["id"], {"agent": {"agent_status": status, "state_change_seq": seq}}
        )

    return handler


async def test_a_successful_prompt_is_delivered(socket_path: str) -> None:
    """herdr observed an ack state, so the submission is acknowledged (D4)."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.reply(
                request["id"],
                {
                    "type": "agent_prompted",
                    "agent": {"agent_status": "working", "state_change_seq": 41},
                },
            )
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.DELIVERED
        assert result.agent_status == "working"
        assert result.state_change_seq == 41
        assert len(_prompts(server)) == 1


async def test_the_prompt_asks_for_the_ack_states_not_herdr_s_default_until(
    socket_path: str,
) -> None:
    """The wait shape IS the design, so it is pinned.

    herdr's default ``--until`` is ``idle``/``done``/``blocked``, which waits for
    a TURN to end; Seam B waits for the earliest states that can only be reached
    by the submission having landed.  A change to either the states or the
    presence of ``wait`` silently changes what ``DELIVERED`` means.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.reply(
                request["id"], {"type": "agent_prompted", "agent": {"agent_status": "working"}}
            )
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.prompt_agent(target="%3", text="hello", wait_timeout_ms=1234)
        params = _prompts(server)[0]["params"]
        assert params["target"] == "%3"
        assert params["text"] == "hello"
        assert params["wait"]["until"] == list(HERDR_PROMPT_ACK_STATES)
        assert params["wait"]["until"] == ["working", "blocked"]
        assert params["wait"]["timeout_ms"] == 1234


async def test_agent_prompt_stalled_is_an_uncertain_submission(socket_path: str) -> None:
    """herdr wrote the text and saw no state move: the question is OPEN.

    This is the arm the whole ninth outcome exists for.  Projecting it onto
    ``DELIVERED`` would claim an acceptance herdr explicitly declined to
    confirm; projecting it onto a retryable outcome would re-offer a row whose
    prompt may already sit in the composer.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.error(request["id"], HERDR_AGENT_PROMPT_STALLED, "no state observed")
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert result.detail == "herdr:" + HERDR_AGENT_PROMPT_STALLED
        assert len(_prompts(server)) == 1


async def test_a_caller_timeout_is_the_same_fact_as_a_stall(socket_path: str) -> None:
    """Bytes were written either way; only the observer gave up sooner."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.error(request["id"], HERDR_TIMEOUT, "caller timeout")
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert len(_prompts(server)) == 1


async def test_agent_blocked_is_a_dialog_veto_and_sends_no_second_prompt(
    socket_path: str,
) -> None:
    """herdr refuses ``before any input is sent``, so nothing was submitted.

    A worker parked on a permission card is WAITING, which D12 bounds by the
    veto ceiling — not a poison message, which is what the attempt budget is
    for.  And the transport does not retry: one call, one ``agent.prompt``.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.error(request["id"], HERDR_AGENT_BLOCKED, "agent is blocked")
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.VETO_DIALOG
        assert result.detail == "herdr:" + HERDR_AGENT_BLOCKED
        assert len(_prompts(server)) == 1
        assert result.outcome not in {
            AttemptOutcome.DELIVERED,
            AttemptOutcome.SUBMISSION_UNCERTAIN,
        }


async def test_a_transport_failure_before_the_flush_is_retryable(socket_path: str) -> None:
    """No socket at all: nothing was written, so this is a failing delivery."""
    client = HerdrClient(socket_path + "-absent")
    result = await client.prompt_agent(target="%3", text="hello")
    assert result.outcome is AttemptOutcome.VETO_UNVERIFIED
    assert result.detail == "herdr:transport_before_submit"


async def test_a_transport_failure_after_the_flush_is_uncertain(socket_path: str) -> None:
    """The socket died holding our bytes, which is the stall by another route."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.close_connection()
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert result.detail == "herdr:transport_after_submit"
        assert len(_prompts(server)) == 1


async def test_an_unmapped_error_code_dies_on_the_attempt_budget(socket_path: str) -> None:
    """A herdr this build is not certified against is a configuration fault.

    It must not sit open until ``dead_by`` pretending to be an uncertain
    submission — the pin exists so an unknown code is loud.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.error(request["id"], "no_such_agent", "unknown target")
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="nope", text="hello")
        assert result.outcome is AttemptOutcome.VETO_UNVERIFIED
        assert result.detail == "herdr:error:no_such_agent"


async def test_an_unexpected_success_shape_is_not_read_as_delivered(socket_path: str) -> None:
    """Guessing that an unknown result body meant success is the one failure
    this seam must not have."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.reply(request["id"], {"type": "something_else"})
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.VETO_UNVERIFIED
        assert result.detail.startswith("herdr:unexpected_result:")


async def test_the_prompt_never_shares_the_streaming_connection(socket_path: str) -> None:
    """Seam B on its own short-lived socket, so it cannot poison Seam A.

    herdr 0.9.0 accepts ``events.subscribe`` only as a connection's FIRST
    message.  A prompt sent down the streaming connection would make every later
    subscribe fail, which is the live-round defect ``request_once`` was written
    for.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.reply(
                request["id"], {"type": "agent_prompted", "agent": {"agent_status": "working"}}
            )
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        await client.connect()
        await client.subscribe([{"type": "pane.updated"}])
        await client.prompt_agent(target="%3", text="hello")
        await server.push_stream({"event": "pane_updated", "data": {}})
        stream = client.events()
        assert (await anext(stream))["event"] == "pane_updated"
        await stream.aclose()
        await client.close()


async def test_agent_state_reads_the_status_and_sequence(socket_path: str) -> None:
    """The evidence the injector's no-second-submission rule runs on."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await server.reply(
                request["id"], {"agent": {"agent_status": "idle", "state_change_seq": 7}}
            )
        elif request.get("method") == "agent.get":
            await _pre_state()(server, request)
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        state = await client.agent_state(target="%3")
        assert state is not None
        assert state.agent_status == "idle"
        assert state.state_change_seq == 7


async def test_agent_state_is_none_when_it_cannot_be_read(socket_path: str) -> None:
    """No evidence either way is not an answer, and is never an error."""
    client = HerdrClient(socket_path + "-absent")
    assert await client.agent_state(target="%3") is None


# --------------------------------------------------------------------------
# The qualification rule, and why it exists.
#
# Live on grok-box-005 (2026-09-16): a prompt issued while the pi pane was
# ALREADY ``working`` returned success in 302 ms, with the status between the
# two prompts read as ``["working", 30]``.  ``--wait`` matches the first state
# observed AFTER submission and ``working`` was already true, so the reply said
# nothing about our text.  These tests pin the projection that finding forced.
# --------------------------------------------------------------------------


def _prompt_ok(status: str = "working", seq: int = 42) -> Handler:
    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.prompt":
            await server.reply(
                request["id"],
                {
                    "type": "agent_prompted",
                    "agent": {"agent_status": status, "state_change_seq": seq},
                },
            )
        else:
            await FakeHerdrServer._default_handler(server, request)

    return handler


async def test_a_success_from_an_already_working_agent_is_uncertain(socket_path: str) -> None:
    """The live finding, as a test.

    herdr's own five-second submission gate is scoped to a submission that
    "starts from another non-working state", so on a busy agent it does not run
    at all and there is no evidence in the reply.  Calling that DELIVERED is the
    false receipt this seam exists to stop.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await _pre_state("working", 30)(server, request)
        else:
            await _prompt_ok("working", 31)(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert result.detail == "herdr:submitted_while_working"
        # It still SUBMITTED — the text is with the runtime, which is exactly
        # why the row may not simply be re-offered.
        assert len(_prompts(server)) == 1


async def test_a_success_from_a_blocked_agent_is_uncertain_too(socket_path: str) -> None:
    """``blocked`` is an ack state, so it satisfies the wait without evidence."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await _pre_state("blocked", 12)(server, request)
        else:
            await _prompt_ok("blocked", 12)(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert result.detail == "herdr:submitted_while_blocked"


async def test_a_success_whose_state_sequence_did_not_move_is_uncertain(
    socket_path: str,
) -> None:
    """The second measure, which catches a pre-read that raced.

    Nothing about the pane moved between the submission and the reply, so the
    wait matched something that was already true.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await _pre_state("idle", 9)(server, request)
        else:
            await _prompt_ok("working", 9)(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert result.detail == "herdr:no_state_advance"


async def test_a_success_from_idle_with_an_advanced_sequence_is_delivered(
    socket_path: str,
) -> None:
    """The one shape that IS evidence: herdr's gate ran and was satisfied."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await _pre_state("idle", 9)(server, request)
        else:
            await _prompt_ok("working", 10)(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.DELIVERED
        assert result.state_change_seq == 10


async def test_an_unreadable_pre_state_is_not_a_delivery(socket_path: str) -> None:
    """No pre-evidence means the qualification cannot be made, so it is not made
    in our favour."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await server.reply(request["id"], {})
        else:
            await _prompt_ok("working", 10)(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.SUBMISSION_UNCERTAIN
        assert result.detail == "herdr:no_pre_state"


async def test_a_refusal_is_never_qualified(socket_path: str) -> None:
    """Only a SUCCESS needs qualifying; a refusal already says what happened."""

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await _pre_state("working", 5)(server, request)
        elif request.get("method") == "agent.prompt":
            await server.error(request["id"], HERDR_AGENT_BLOCKED, "blocked")
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="%3", text="hello")
        assert result.outcome is AttemptOutcome.VETO_DIALOG


async def test_agent_not_found_is_a_pane_absent(socket_path: str) -> None:
    """herdr's real code for an unknown target, read off the live box.

    Same bound as the unmapped default, but the pane injector already has a word
    for "no pane to write to" and two carriers must not name one condition
    differently in the same journal.
    """

    async def handler(server: FakeHerdrServer, request: dict[str, Any]) -> None:
        if request.get("method") == "agent.get":
            await _pre_state()(server, request)
        elif request.get("method") == "agent.prompt":
            await server.error(request["id"], "agent_not_found", "no such agent")
        else:
            await FakeHerdrServer._default_handler(server, request)

    async with FakeHerdrServer(socket_path) as server:
        server.on_request = handler
        client = HerdrClient(socket_path)
        result = await client.prompt_agent(target="nope", text="hello")
        assert result.outcome is AttemptOutcome.PANE_ABSENT
        assert result.detail == "herdr:agent_not_found"
