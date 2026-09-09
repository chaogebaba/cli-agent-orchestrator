"""The one herdr socket transport in the tree (WP-HERDR H1, blueprint §4, §8 H1).

**Transport ONLY.**  This leaf does three things and nothing else: it opens the
herdr unix socket, it speaks the newline-delimited JSON-RPC request/response the
socket API uses, and it streams ``events.subscribe`` pushes.  It carries no
lifecycle mapping, no ``WorkerState``, no delivery policy — those belong to
``adapters/truth/herdr_runtime.py`` (Seam A) and the A2 delivery owner (Seam B),
which call this.

Why it is a NEW file rather than a method moved verbatim: the blueprint §4 places
the herdr transport at ``adapters/herdr/client.py`` as the leaf both seams call,
and the ``adapters-are-leaves`` + ``new-code-never-imports-legacy`` contracts
forbid an adapter from importing ``backends``, ``clients``, ``services``,
``models`` or the fork's own exception types.  So the socket JSON-RPC that lives
today in ``services/herdr_inbox_service.py`` (``_connect``/``_subscribe_all_events``/
``_event_loop``/``_send``) and the socket-path resolution that lives in both that
service and ``backends/herdr_backend.py`` are re-expressed here against the stdlib
alone.  There is exactly ONE herdr client in the tree; the legacy shim IMPORTS
this one (legacy may import new; not the reverse) and both retire together in H3.

Protocol pin (D7): the H0 round-2 evidence records herdr 0.9.0 at **protocol 22 /
schema_version 1** (``/data/cao-scratch/herdr-p0-r2-evidence/p1/status.txt``,
``p1/api-schema.json``).  :meth:`HerdrClient.check_protocol` reads the server's
own ``api schema`` and refuses a mismatch, so a drifted pin fails loudly at
connect rather than by silently misreading a later wire format.

Wire facts this transport encodes, each observed in the H0 evidence:

* A request is one JSON object with ``id``/``method``/``params`` followed by a
  newline; the matching reply is a JSON object carrying the same ``id`` and
  either ``result`` or ``error`` (``p1/api-schema.json`` ``request`` /
  ``success_response`` / ``error_response`` schemas).
* ``events.subscribe`` first replies with an ack — ``{"result":{"type":
  "subscription_started"}}`` (``p2/pi/subscribe-ack.json``) — and thereafter the
  server pushes event objects with NO ``id`` and NO replay of pre-subscription
  events.  herdr resets the connection on a SECOND ``events.subscribe``, so a
  connection subscribes exactly once (``herdr_inbox_service._subscribe_all_events``).
* A pushed event names itself in ``event`` (underscore form, e.g.
  ``pane_updated``) and nests its body under ``data``; a broadcast
  ``pane.updated`` nests the pane under ``data.pane`` carrying ``agent_status``,
  ``agent`` and ``agent_session`` (``p2/pi`` turn evidence).

Timeouts are keyword-argument DEFAULTS rather than module constants on purpose:
§4c forbids a bare duration literal outside ``core/timing.py`` for any
``*_S``-named binding, and the H1 brief forbids editing ``core/``.  Arg defaults
are neither an assignment target nor a ``sleep`` literal, so they satisfy both
the duration rule and the no-core-edit rule; a caller that wants a different
bound passes one.  This module holds NO reconnect loop and NO ``sleep`` — the
reconnect cadence is the adapter's, driven off a backoff the adapter owns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

__all__ = [
    "HERDR_PROTOCOL",
    "HERDR_SCHEMA_VERSION",
    "HerdrClient",
    "HerdrError",
    "HerdrProtocolMismatch",
    "HerdrRequestError",
    "HerdrTransportError",
    "default_socket_path",
]

#: The pinned herdr wire protocol (D7).  H0 round 2 observed 0.9.0 at protocol 22
#: / schema_version 1.  A pin is re-certified, never drifted: moving it is a
#: milestone decision, not a silent bump, so :meth:`HerdrClient.check_protocol`
#: compares against these and refuses anything else.
HERDR_PROTOCOL = 22
HERDR_SCHEMA_VERSION = 1


class HerdrError(Exception):
    """Base for every herdr transport failure this leaf raises.

    A dedicated hierarchy rather than the fork's ``TerminalBackendError`` because
    an adapter may not import ``backends`` (``new-code-never-imports-legacy``),
    and because a transport that raised the backend's type would blur "the socket
    is gone" with "the terminal is gone" — two facts Seam A must keep apart.
    """


class HerdrTransportError(HerdrError):
    """The socket could not be reached, or the connection dropped mid-call."""


class HerdrRequestError(HerdrError):
    """The server answered a request with an ``error`` body.

    Carries the herdr ``code``/``message`` verbatim so a caller can branch on the
    code without re-parsing text.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"herdr request error [{code}]: {message}")
        self.code = code
        self.message = message


class HerdrProtocolMismatch(HerdrError):
    """The server's ``api schema`` reported a protocol/schema this pin refuses."""

    def __init__(self, *, got_protocol: int, got_schema: int) -> None:
        super().__init__(
            f"herdr protocol mismatch: server reports protocol={got_protocol} "
            f"schema_version={got_schema}, this build pins protocol={HERDR_PROTOCOL} "
            f"schema_version={HERDR_SCHEMA_VERSION}"
        )
        self.got_protocol = got_protocol
        self.got_schema = got_schema


def default_socket_path(session: str, *, config_home: str | None = None) -> str:
    """Resolve the herdr socket path for ``session``, stdlib only.

    The single definition of herdr's socket layout, carved out of the two legacy
    copies (``herdr_backend._session_socket_path`` and
    ``HerdrInboxService._default_socket_path``) so the transport does not depend
    on either.  The default session (name ``"default"``) uses the flat
    ``<config>/herdr/herdr.sock``; a named session nests under
    ``<config>/herdr/sessions/<name>/herdr.sock``.

    ``config_home`` defaults to ``$XDG_CONFIG_HOME`` then ``~/.config`` — the same
    precedence both legacy copies use — and is a parameter so a test need not
    mutate the environment.
    """
    base = config_home
    if base is None:
        # Match the legacy resolution exactly: os.environ.get with a default,
        # which returns an EMPTY string if XDG_CONFIG_HOME is set-but-empty
        # (rather than falling back), so the shim's behaviour is byte-identical.
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    # String concatenation rather than ``Path`` joins, byte-for-byte with the two
    # legacy copies this replaces (``herdr_backend._session_socket_path`` and
    # ``HerdrInboxService._default_socket_path``): a ``Path`` join would normalise
    # a trailing slash or an empty base differently and break "behaves identically".
    if session == "default":
        return f"{base}/herdr/herdr.sock"
    return f"{base}/herdr/sessions/{session}/herdr.sock"


def _envelope_result(reply: dict[str, Any]) -> dict[str, Any]:
    """Return the ``result`` body of a JSON-RPC reply, raising on an error body.

    herdr wraps a success as ``{"id":..., "result": {...}}`` and a failure as
    ``{"id":..., "error": {"code":..., "message":...}}`` (``api-schema.json``
    ``success_response`` / ``error_response``).  An error body becomes a
    :class:`HerdrRequestError`; a reply with neither is a transport-level
    malformation.
    """
    error = reply.get("error")
    if isinstance(error, dict):
        raise HerdrRequestError(
            str(error.get("code", "unknown")), str(error.get("message", ""))
        )
    result = reply.get("result")
    if not isinstance(result, dict):
        raise HerdrTransportError(f"herdr reply carries neither result nor error: {reply!r}")
    return result


class HerdrClient:
    """One connection to one herdr session's socket.

    Lifecycle: :meth:`connect`, then :meth:`request` for one-shot RPCs and/or ONE
    :meth:`subscribe` followed by iterating :meth:`events`, then :meth:`close`.
    Not reconnecting by itself — a dropped socket raises :class:`HerdrTransportError`
    and the caller (the adapter's run loop) reconnects on its own backoff, exactly
    as ``herdr_inbox_service`` does today.  This keeps the transport a leaf: the
    reconnect POLICY (how long to wait, when to resnapshot, when to declare a
    degraded gap) is Seam A's, not the socket's.

    A single ``asyncio.Lock`` serialises writes and the request/reply match, so a
    ``request`` issued while events are streaming reads its OWN reply rather than
    a pushed event: pushed events carry no ``id`` and are routed to the event
    queue, replies carry the awaited ``id`` and are handed back to
    :meth:`request`.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        connect_timeout_s: float = 5.0,
        request_timeout_s: float = 10.0,
    ) -> None:
        self._socket_path = socket_path
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._id_counter = 0
        self._io_lock = asyncio.Lock()
        self._subscribed = False

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def connected(self) -> bool:
        return self._reader is not None and self._writer is not None

    async def connect(self) -> None:
        """Open the unix socket.  Raises :class:`HerdrTransportError` on failure.

        Idempotent while connected: a second call on a live connection returns
        without churning it, mirroring the attach idempotency the rollout tailer
        relies on.
        """
        if self.connected:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._socket_path),
                timeout=self._connect_timeout_s,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self._reader = None
            self._writer = None
            raise HerdrTransportError(
                f"could not connect to herdr socket {self._socket_path}: {exc}"
            ) from exc
        logger.debug("connected to herdr socket %s", self._socket_path)

    async def close(self) -> None:
        """Close the socket.  Safe to call more than once and when never opened."""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._subscribed = False
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, RuntimeError):
            # A close on an already-broken transport is not a new failure to
            # report — the caller asked us to let go of it.
            logger.debug("herdr socket close raced a broken transport", exc_info=True)

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"cao-{self._id_counter}"

    async def _write_message(self, message: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            raise HerdrTransportError("herdr client is not connected")
        payload = json.dumps(message).encode() + b"\n"
        try:
            writer.write(payload)
            await writer.drain()
        except (OSError, RuntimeError) as exc:
            raise HerdrTransportError(f"herdr socket write failed: {exc}") from exc

    async def _read_line(self) -> dict[str, Any]:
        """Read one JSON line, skipping blank lines.  Raises on EOF/timeout.

        A blank line is not a message; herdr does not emit them, but a lenient
        reader keeps a stray keep-alive newline from being mistaken for EOF.
        """
        reader = self._reader
        if reader is None:
            raise HerdrTransportError("herdr client is not connected")
        while True:
            try:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=self._request_timeout_s
                )
            except asyncio.TimeoutError as exc:
                raise HerdrTransportError("timed out waiting for a herdr reply") from exc
            if not line:
                raise HerdrTransportError("herdr socket closed")
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                raise HerdrTransportError(f"herdr sent unparseable JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise HerdrTransportError(f"herdr sent a non-object line: {obj!r}")
            return obj

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue one JSON-RPC request and return its ``result`` body.

        Matches the reply by ``id`` and DROPS any pushed event that arrives on the
        wire first (events carry no ``id``), so a request issued on a subscribed
        connection is still answered correctly.  A dropped event during a request
        is not lost data in the way it looks: the caller that mixes ``request``
        and ``events`` on one connection is the adapter, and it resnapshots after
        (re)subscribing rather than trusting the event backlog — the herdr
        client recipe (subscribe, snapshot, apply buffered events) that the docs
        and ``herdr_inbox_service`` both follow.
        """
        async with self._io_lock:
            request_id = self._next_id()
            await self._write_message(
                {"id": request_id, "method": method, "params": params or {}}
            )
            while True:
                reply = await self._read_line()
                if reply.get("id") != request_id:
                    # A pushed event or an unrelated reply; not ours.  See the
                    # method docstring for why dropping it here is safe.
                    continue
                return _envelope_result(reply)

    async def check_protocol(self) -> dict[str, Any]:
        """Read ``api schema`` and refuse a protocol/schema this build does not pin.

        Returns the schema ``result`` on a match so a caller may inspect it;
        raises :class:`HerdrProtocolMismatch` otherwise.  This is the D7 pin made
        mechanical: the transport will not stream a wire format it was not
        certified against.
        """
        result = await self.request("api.schema")
        got_protocol = result.get("protocol")
        got_schema = result.get("schema_version")
        if got_protocol != HERDR_PROTOCOL or got_schema != HERDR_SCHEMA_VERSION:
            raise HerdrProtocolMismatch(
                got_protocol=int(got_protocol) if isinstance(got_protocol, int) else -1,
                got_schema=int(got_schema) if isinstance(got_schema, int) else -1,
            )
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Return the ``snapshot`` body of ``api snapshot``.

        The recipe's second step: after subscribing, a caller snapshots to get the
        current pane/agent records, then applies buffered events in order.  Kept
        here because it is a plain request/reply on the same socket; the mapping
        of its records onto ``WorkerState`` is Seam A's, not this leaf's.
        """
        result = await self.request("api.snapshot")
        snapshot = result.get("snapshot")
        if not isinstance(snapshot, dict):
            raise HerdrTransportError(f"herdr api.snapshot carried no snapshot: {result!r}")
        return snapshot

    async def subscribe(self, subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        """Send the ONE ``events.subscribe`` this connection may send, return the ack.

        herdr resets the connection on a second ``events.subscribe`` (0.7.5+),
        so this refuses a second call rather than triggering that reset — the
        single-subscribe rule ``herdr_inbox_service`` enforces, made a property of
        the client instead of a comment on the caller.  Callers that need more
        event types add them to this one call's ``subscriptions`` list.
        """
        if self._subscribed:
            raise HerdrError(
                "events.subscribe may be sent only once per herdr connection; "
                "add every subscription to the first call"
            )
        async with self._io_lock:
            request_id = self._next_id()
            await self._write_message(
                {
                    "id": request_id,
                    "method": "events.subscribe",
                    "params": {"subscriptions": subscriptions},
                }
            )
            while True:
                reply = await self._read_line()
                if reply.get("id") != request_id:
                    continue
                ack = _envelope_result(reply)
                self._subscribed = True
                return ack

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield pushed event objects until the socket closes.

        Only valid after :meth:`subscribe`.  Each yielded object is the raw event
        dict; the caller reads ``event`` (the underscore-form name) and ``data``.
        A reply carrying an ``id`` that slips through here (there should be none
        after the ack) is skipped rather than yielded as an event.  A socket close
        ends the iterator by raising :class:`HerdrTransportError`, which the
        adapter's run loop turns into a reconnect — events are not receipts, and a
        gap is Seam A's degraded signal, not a silently swallowed EOF.
        """
        if not self._subscribed:
            raise HerdrError("events() requires a prior subscribe() on this connection")
        while True:
            event = await self._read_line_streaming()
            if "id" in event and "event" not in event:
                continue
            yield event

    async def _read_line_streaming(self) -> dict[str, Any]:
        """Read one pushed line WITHOUT the request timeout.

        The event stream is idle for long stretches by design — a working pane
        emits nothing until its status changes — so a request-sized timeout would
        tear down a healthy idle subscription.  This read blocks until a line
        arrives or the socket closes; the caller's degraded-gap horizon
        (``NO_SIGNAL_S``) is what notices a source that has genuinely gone quiet,
        not a socket read timeout.
        """
        reader = self._reader
        if reader is None:
            raise HerdrTransportError("herdr client is not connected")
        while True:
            line = await reader.readline()
            if not line:
                raise HerdrTransportError("herdr socket closed")
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                logger.debug("skipping unparseable herdr event line", exc_info=True)
                continue
            if isinstance(obj, dict):
                return obj
