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
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from cli_agent_orchestrator.core.delivery import AttemptOutcome
from cli_agent_orchestrator.core.timing import HERDR_PROMPT_WAIT_MS

logger = logging.getLogger(__name__)

__all__ = [
    "HERDR_AGENT_BLOCKED",
    "HERDR_DETAIL_PREFIX",
    "HERDR_AGENT_NOT_FOUND",
    "HERDR_AGENT_PROMPT_STALLED",
    "HERDR_PROMPT_ACK_STATES",
    "HERDR_PROTOCOL",
    "HERDR_SCHEMA_VERSION",
    "HERDR_TIMEOUT",
    "HerdrClient",
    "HerdrError",
    "HerdrProtocolMismatch",
    "HerdrRequestError",
    "HerdrTransportError",
    "PromptSubmission",
    "default_socket_path",
]

#: The pinned herdr wire protocol (D7).  H0 round 2 observed 0.9.0 at protocol 22
#: / schema_version 1.  A pin is re-certified, never drifted: moving it is a
#: milestone decision, not a silent bump, so :meth:`HerdrClient.check_protocol`
#: compares against these and refuses anything else.
HERDR_PROTOCOL = 22
HERDR_SCHEMA_VERSION = 1

#: The three ``agent.prompt`` failure codes herdr 0.9.0 documents, verbatim from
#: its own ``herdr agent prompt --help``:
#:
#:   "If the agent is already blocked, submission is rejected with agent_blocked
#:   before any input is sent.  When an accepted submission starts from another
#:   non-working state, --wait requires an observed working or blocked state
#:   within 5000ms; otherwise it returns agent_prompt_stalled.  A caller timeout
#:   that expires first returns timeout."
#:
#: Quoted rather than paraphrased because the WHOLE mapping below turns on the
#: phrase "before any input is sent": that is what makes ``agent_blocked`` a
#: refusal we may retry, and the other two submissions we may not.
HERDR_AGENT_BLOCKED = "agent_blocked"
HERDR_AGENT_NOT_FOUND = "agent_not_found"
HERDR_AGENT_PROMPT_STALLED = "agent_prompt_stalled"
HERDR_TIMEOUT = "timeout"

#: The pane states that ACKNOWLEDGE a submission.
#:
#: Seam B asks for ``wait`` and asks for exactly these two, which is the whole
#: reason it is not a bare ``agent.prompt``.  A bare prompt returns
#: ``agent_prompted`` the instant herdr has WRITTEN the text, which is evidence
#: that bytes left CAO and no evidence at all that an agent took them — i.e.
#: every delivery would be ``SUBMISSION_UNCERTAIN`` and nothing would ever be
#: ``DELIVERED``.  With ``wait``, herdr's own observation of ``working`` or
#: ``blocked`` is D4's "acknowledged acceptance for the bound occupant", and its
#: absence inside herdr's five-second gate is ``agent_prompt_stalled``.
#:
#: They are NOT herdr's default ``--until`` set (``idle``, ``done``, ``blocked``).
#: That set waits for a TURN TO END, which for a delivery seam means blocking the
#: tick on the agent's work; these two are the earliest states that can only be
#: reached by the submission having landed.
HERDR_PROMPT_ACK_STATES = ("working", "blocked")


class HerdrError(Exception):
    """Base for every herdr transport failure this leaf raises.

    A dedicated hierarchy rather than the fork's ``TerminalBackendError`` because
    an adapter may not import ``backends`` (``new-code-never-imports-legacy``),
    and because a transport that raised the backend's type would blur "the socket
    is gone" with "the terminal is gone" — two facts Seam A must keep apart.
    """


class HerdrTransportError(HerdrError):
    """The socket could not be reached, or the connection dropped mid-call.

    ``submitted`` is the ONE fact the delivery seam cannot reconstruct
    afterwards: whether this request's bytes had already been flushed to herdr
    when the transport failed.  A failure BEFORE the flush is a delivery that
    never started and may be retried; a failure AFTER it is a submission whose
    outcome is unknown, which is a different thing and is what
    ``AttemptOutcome.SUBMISSION_UNCERTAIN`` exists to carry.  Collapsing the two
    into one error is how a retry double-submits into an agent's composer.

    It defaults ``False`` because every raise site outside :meth:`request_once`
    is a connect-time failure, where nothing was written by construction.
    """

    def __init__(self, message: str, *, submitted: bool = False) -> None:
        super().__init__(message)
        self.submitted = submitted


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


#: The one prefix every Seam B ``detail`` carries, applied HERE and nowhere else.
#:
#: r1 applied it in two places — this module and ``HerdrPromptInjector._submit``
#: — so every live journal row read ``herdr:herdr:working`` (review r1 §5).  One
#: prefix, applied once, also makes ``detail LIKE 'herdr:%'`` the exact predicate
#: for "this attempt went through Seam B", which is the observability the shared
#: ``carrier=pane`` value costs (review r1 §5 accepts the shared carrier name on
#: condition the detail stays legible).
HERDR_DETAIL_PREFIX = "herdr:"


def _detail(word: str) -> str:
    """``word`` under the single Seam B prefix, idempotently."""
    return word if word.startswith(HERDR_DETAIL_PREFIX) else HERDR_DETAIL_PREFIX + word


@dataclass(frozen=True)
class PromptSubmission:
    """What ONE ``agent.prompt`` did, in the delivery vocabulary (H2-S2).

    The mapping from herdr's reply to :class:`~core.delivery.AttemptOutcome`
    lives HERE, in the transport, and not in the injector that calls it.  That is
    deliberate: the codes are wire facts of herdr 0.9.0, they change when herdr
    changes, and the pin that says which herdr this build talks to is three
    constants up.  An injector that re-derived them would be a second place the
    protocol is known, and the blueprint allows exactly one.

    Importing ``core.delivery`` is the only import this leaf has beyond stdlib.
    It is contract-legal (``adapters`` may import ``core``; the reverse and every
    legacy package are what the contracts forbid) and it is what keeps the
    mapping typed instead of stringly.

    ``agent_status`` and ``state_change_seq`` are herdr's own reading of the pane
    AT THE MOMENT the submission resolved, passed through untouched.  The
    injector uses the sequence number to decide later whether an earlier
    UNCERTAIN submission has since resolved; nothing here interprets them.
    """

    outcome: AttemptOutcome
    detail: str
    agent_status: str | None = None
    state_change_seq: int | None = None


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
        raise HerdrRequestError(str(error.get("code", "unknown")), str(error.get("message", "")))
    result = reply.get("result")
    if not isinstance(result, dict):
        raise HerdrTransportError(f"herdr reply carries neither result nor error: {reply!r}")
    return result


def _as_status(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_seq(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _submission_from_result(result: dict[str, Any]) -> PromptSubmission:
    """Map a successful ``agent.prompt`` reply.

    A reply reached us only because the wait was satisfied — herdr answers an
    unsatisfied wait with ``agent_prompt_stalled`` or ``timeout``, not with a
    success — so the outcome is ``DELIVERED`` and the pane's state rides along as
    detail.  The ``type`` is checked anyway: a herdr that answered
    ``agent.prompt`` with some other result body is one this build does not know,
    and guessing that it meant success is exactly the failure this seam must not
    have.
    """
    if result.get("type") != "agent_prompted":
        return PromptSubmission(
            outcome=AttemptOutcome.VETO_UNVERIFIED,
            detail=_detail(f"unexpected_result:{result.get('type')!r}"),
        )
    agent = result.get("agent")
    agent_dict: dict[str, Any] = agent if isinstance(agent, dict) else {}
    status = _as_status(agent_dict.get("agent_status"))
    return PromptSubmission(
        outcome=AttemptOutcome.DELIVERED,
        detail=_detail(status or "unknown"),
        agent_status=status,
        state_change_seq=_as_seq(agent_dict.get("state_change_seq")),
    )


async def _read_state(
    send: Callable[..., Awaitable[dict[str, Any]]], target: str, timeout_s: float
) -> PromptSubmission | None:
    """The pre-submission state read, on the caller's already-open connection.

    Its own, SHORTER bound, and its own swallowed failure — review r1 §10.5.
    Two distinct things were conflated in r1: a read that cannot be answered and
    a submission whose fate is unknown.  They differ in what was written to the
    agent (nothing, versus a whole prompt), so they must not share a deadline and
    must not share an outcome.  A read that fails here returns ``None``, the
    submission goes ahead regardless, and ``_qualify_success`` reports
    ``no_pre_state`` — which is honest, because by then the text HAS gone.

    Never raises: a failed read is a missing input, not a failed delivery.
    """
    try:
        result = await send("agent.get", {"target": target}, timeout_s=timeout_s)
    except HerdrError:
        logger.debug("herdr pre-submission state read failed for %s", target, exc_info=True)
        return None
    agent = result.get("agent")
    if not isinstance(agent, dict):
        return None
    return PromptSubmission(
        outcome=AttemptOutcome.DELIVERED,
        detail=_detail("agent_state"),
        agent_status=_as_status(agent.get("agent_status")),
        state_change_seq=_as_seq(agent.get("state_change_seq")),
    )


def _qualify_success(
    submission: PromptSubmission, before: PromptSubmission | None
) -> PromptSubmission:
    """Downgrade a success that the wait could not actually have witnessed.

    ``agent.prompt --wait`` answers on "the first matching state observed after
    submission".  Two ways that is satisfied WITHOUT the agent having taken our
    text, both observed live rather than reasoned about:

    * the agent was ALREADY in an ack state, so the match is the state it was
      already in.  herdr's five-second submission gate does not even run here —
      its own help scopes the gate to a submission that "starts from another
      non-working state" — so there is no evidence in the reply at all.
    * the agent's ``state_change_seq`` did not advance.  Nothing about the pane
      moved between the submission and the reply, which is the same emptiness
      by a different measure, and it catches the case where the pre-read raced.

    Both become ``SUBMISSION_UNCERTAIN``: the text HAS been handed to the
    runtime (so the row must never be offered again — the store's per-id
    quarantine is what enforces that) and nothing acknowledged it (so it is not
    a delivery).  A non-success submission is returned untouched — a refusal
    needs no qualifying.

    **What this does NOT catch**, stated here and not only in
    :meth:`HerdrClient.prompt_agent`: an agent that flips into an ack state in
    the window between the read and the submission passes both arms and is
    reported ``DELIVERED``.  herdr "does not track turns" and exposes no
    submission identity, so this function has nothing left to compare.  Review
    r1 §3 executed that case; it is open.
    """
    if submission.outcome is not AttemptOutcome.DELIVERED:
        return submission
    if before is None:
        return PromptSubmission(
            outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
            detail=_detail("no_pre_state"),
            agent_status=submission.agent_status,
            state_change_seq=submission.state_change_seq,
        )
    if before.agent_status in HERDR_PROMPT_ACK_STATES:
        return PromptSubmission(
            outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
            detail=_detail(f"submitted_while_{before.agent_status}"),
            agent_status=submission.agent_status,
            state_change_seq=submission.state_change_seq,
        )
    if (
        before.state_change_seq is not None
        and submission.state_change_seq is not None
        and submission.state_change_seq <= before.state_change_seq
    ):
        return PromptSubmission(
            outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
            detail=_detail("no_state_advance"),
            agent_status=submission.agent_status,
            state_change_seq=submission.state_change_seq,
        )
    return submission


def _submission_from_error_code(code: str, message: str) -> PromptSubmission:
    """Map one herdr ``agent.prompt`` error code.  The table is in the docstring
    of :meth:`HerdrClient.prompt_agent`; this is only its transcription."""
    if code == HERDR_AGENT_PROMPT_STALLED:
        return PromptSubmission(
            outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
            detail=_detail(HERDR_AGENT_PROMPT_STALLED),
        )
    if code == HERDR_TIMEOUT:
        return PromptSubmission(
            outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
            detail=_detail(HERDR_TIMEOUT),
        )
    if code == HERDR_AGENT_BLOCKED:
        return PromptSubmission(
            outcome=AttemptOutcome.VETO_DIALOG, detail=_detail(HERDR_AGENT_BLOCKED)
        )
    if code == HERDR_AGENT_NOT_FOUND:
        # The live arm on grok-box-005 (2026-09-16) is where this code came
        # from: a prompt to a name herdr does not hold answers
        # ``agent_not_found``.  Same BOUND as the unmapped default — both spend
        # the attempt budget — but the pane injector already has a word for "no
        # pane to write to" and using a different one for the same fact would
        # make two carriers' journals disagree about one condition.
        return PromptSubmission(
            outcome=AttemptOutcome.PANE_ABSENT, detail=_detail(HERDR_AGENT_NOT_FOUND)
        )
    logger.debug("unmapped herdr agent.prompt error code %s: %s", code, message)
    return PromptSubmission(outcome=AttemptOutcome.VETO_UNVERIFIED, detail=_detail(f"error:{code}"))


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
    a pushed event: pushed events carry no ``id`` and are BUFFERED for the event
    stream (they are not dropped), replies carry the awaited ``id`` and are handed
    back to :meth:`request`.  The buffer is the subscribe → snapshot → reconcile
    recipe (§6) made mechanical: an event that races in between the
    ``events.subscribe`` ack and the ``api.snapshot`` reply is held, then replayed
    into :meth:`events` after the snapshot, in arrival order, exactly once — so
    the reconnect path has no drop window.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        connect_timeout_s: float = 5.0,
        request_timeout_s: float = 10.0,
        state_read_timeout_s: float = 3.0,
    ) -> None:
        self._socket_path = socket_path
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        #: SEPARATE from the request bound, and shorter, because a state read is
        #: an OPTIONAL input to the mapping while a submission is the delivery
        #: itself.  A slow read must not spend the submission's budget, and it
        #: must never be reported as a submission that may have landed.
        self._state_read_timeout_s = state_read_timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._id_counter = 0
        self._io_lock = asyncio.Lock()
        self._subscribed = False
        #: Pushed events read off the wire while awaiting a request/subscribe
        #: reply, held in ARRIVAL order for :meth:`events` to replay after the
        #: snapshot.  This is the §6 reconcile buffer — the line an event stream
        #: would otherwise lose when it interleaves with the subscribe→snapshot
        #: handshake.  Bounded implicitly by how many events herdr can push
        #: during the two round-trips of that handshake; it drains the instant
        #: :meth:`events` runs.
        self._event_buffer: deque[dict[str, Any]] = deque()

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
        self._event_buffer.clear()
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
                line = await asyncio.wait_for(reader.readline(), timeout=self._request_timeout_s)
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

    @staticmethod
    def _is_pushed_event(line: dict[str, Any]) -> bool:
        """Whether a wire line is a pushed event rather than a request reply.

        A reply carries the awaited ``id`` and either ``result`` or ``error``; a
        pushed event carries NO ``id`` and names itself in ``event``.  This is the
        one place that distinction is spelled, so :meth:`request`, :meth:`subscribe`
        and :meth:`events` all buffer/skip on the same rule.
        """
        return "id" not in line and "event" in line

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue one JSON-RPC request and return its ``result`` body.

        Matches the reply by ``id``.  A pushed event (no ``id``) that arrives on
        the wire before the reply is BUFFERED into :attr:`_event_buffer` — NOT
        dropped — so it is replayed into :meth:`events` after the snapshot, in
        arrival order, exactly once.  This is the §6 subscribe → snapshot →
        reconcile recipe: an event that races into the handshake window is held,
        not lost, closing B4's drop race.  A line that is neither this request's
        reply nor a pushed event (a stray reply for another id — which should not
        occur while the io lock serialises requests) is skipped.
        """
        async with self._io_lock:
            request_id = self._next_id()
            await self._write_message({"id": request_id, "method": method, "params": params or {}})
            while True:
                reply = await self._read_line()
                if reply.get("id") == request_id:
                    return _envelope_result(reply)
                if self._is_pushed_event(reply):
                    self._event_buffer.append(reply)
                # else: a stray line for another id — skip; see docstring.

    @asynccontextmanager
    async def _one_shot(self) -> AsyncIterator[Callable[..., Awaitable[dict[str, Any]]]]:
        """One short-lived connection, over which SEVERAL requests may be issued.

        r1 had only :meth:`request_once`, one connection per request.  That is
        correct for an isolated call and wrong for a pair of calls whose ORDER
        and CLOSENESS is the point: Seam B reads the agent's state and then
        submits, and everything that happens between the two is a race it cannot
        see (review r1 §3).  A second connect, DNS-free though it is, is still a
        socket setup, a scheduler hop and a teardown between them.

        This does not CLOSE that race — nothing available in herdr 0.9.0 does,
        and the docstring of :meth:`prompt_agent` says so plainly — it narrows
        the window from two connection lifetimes to one message gap on a socket
        that is already open.

        The connection still carries no subscription, so the single-subscribe
        rule :meth:`request_once` documents is untouched.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._socket_path),
                timeout=self._connect_timeout_s,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise HerdrTransportError(
                f"could not connect to herdr socket {self._socket_path}: {exc}",
                submitted=False,
            ) from exc

        async def send(
            method: str,
            params: dict[str, Any] | None = None,
            *,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            # H2: whether THIS request's bytes reached herdr is the difference
            # between "retry this" and "we do not know what happened", so it is
            # tracked per request rather than inferred from the exception type.
            flushed = False
            try:
                request_id = self._next_id()
                payload = (
                    json.dumps(
                        {"id": request_id, "method": method, "params": params or {}}
                    ).encode()
                    + b"\n"
                )
                writer.write(payload)
                await writer.drain()
                flushed = True
                deadline = self._request_timeout_s if timeout_s is None else timeout_s
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=deadline)
                    if not line:
                        raise HerdrTransportError("herdr socket closed")
                    body = line.strip()
                    if not body:
                        continue
                    obj = json.loads(body)
                    if not isinstance(obj, dict):
                        raise HerdrTransportError(f"herdr sent a non-object line: {obj!r}")
                    if obj.get("id") == request_id:
                        return _envelope_result(obj)
                    # A one-shot connection carries no subscription, so anything
                    # else on it is noise; keep reading for our reply.
            except HerdrTransportError as exc:
                # Raised inside the loop (a closed socket, a non-object line, a
                # reply carrying neither result nor error) — all of them AFTER
                # the flush.  Re-raised carrying that fact rather than swallowed
                # into the generic arm, which would lose it.
                raise HerdrTransportError(str(exc), submitted=flushed) from exc
            except (OSError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as exc:
                raise HerdrTransportError(
                    f"herdr one-shot request {method} failed: {exc}", submitted=flushed
                ) from exc

        try:
            yield send
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, RuntimeError):
                logger.debug("herdr one-shot close raced a broken transport", exc_info=True)

    async def request_once(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue one request on its OWN short-lived connection.

        herdr 0.9.0's API socket accepts ``events.subscribe`` only as the FIRST
        message on a connection: once a plain request has gone down the wire, a
        later subscribe is answered by resetting the connection.  So a client
        that means to STREAM must keep its connection clean, and every
        request/reply it also needs — the protocol read and the snapshot — has to
        happen somewhere else.

        This is that somewhere else.  It is also what the legacy inbox service
        did without naming it: it subscribed first on its socket and read the
        snapshot by shelling out to ``herdr api snapshot``, which is a separate
        connection by construction.

        The H1 live round on grok-box-002 is what surfaced the rule: with the
        protocol read on the streaming connection, ``subscribe`` failed with
        "Connection lost" on every attempt and the source never received an
        event.

        A one-request case of :meth:`_one_shot`.
        """
        async with self._one_shot() as send:
            return await send(method, params)

    async def prompt_agent(
        self,
        *,
        target: str,
        text: str,
        wait_timeout_ms: int = HERDR_PROMPT_WAIT_MS,
    ) -> PromptSubmission:
        """Submit ONE prompt to a herdr agent and say what happened (Seam B).

        ``target`` is a herdr-namespace key (a pane id, or a name herdr resolves
        to one) — never a CAO terminal uuid.  Resolving the CAO id to it is the
        caller's job, because this leaf holds no registry and must not acquire
        one.

        On its OWN short-lived connection, via :meth:`request_once`, for the
        reason that method documents: a streaming connection may carry no plain
        request before its ``events.subscribe``, and Seam A owns the streaming
        connection.  So Seam B never shares a socket with Seam A and cannot
        poison it.

        **A success is QUALIFIED against the state the agent was in before —
        and that does NOT close the race.**  Say the open part first, because r1
        claimed closure and review r1 §3 disproved it.

        herdr 0.9.0's own ``agent prompt --help`` ends:

            "It does not track turns: if the agent is already working, that
            active turn's completion may match."

        A reply therefore carries no identity of its own.  ``--wait`` matches the
        first state observed after submission, and herdr's five-second
        submission gate runs only "when an accepted submission starts from
        another non-working state".  Two consequences, and only the first is
        fixed here:

        * **Closed:** the agent was ALREADY in an ack state when we read it.  The
          reply is then evidence of nothing, and the pre-state read catches it —
          on grok-box-005 (2026-09-16) a prompt to an already-``working`` pi pane
          came back successful in 302 ms, and now projects to
          ``SUBMISSION_UNCERTAIN``.
        * **OPEN:** the agent flips ``idle -> working`` in the window BETWEEN the
          read and the submission — it self-started, or another writer prompted
          it.  Its own transition advances ``state_change_seq``, both qualifier
          arms pass, and the outcome is ``DELIVERED`` while our text sits
          unconsumed in a mid-turn composer.  herdr exposes no submission id and
          no per-submission event, so no comparison available to this client
          separates that from a true delivery.

        What is done about the open case is narrowing, not closing: the read and
        the submission share ONE connection (:meth:`_one_shot`), so the window is
        a message gap rather than two connection lifetimes.  The residual belongs
        in the cohort's certification row as reduced assurance (blueprint
        amendment (7)) and is never to be described as closed.

        The mirror race — the pane finishing its turn in the window, so a genuine
        delivery is downgraded — is left conservative on purpose.  Since the
        quarantine became per-id and lives in the store, a spurious
        ``SUBMISSION_UNCERTAIN`` costs one row held to ``dead_by`` and can no
        longer seed a duplicate, which is what made it worth arguing about in r1.

        **Exactly one submission per call, in every arm.**  There is no internal
        retry and no second ``agent.prompt`` on any path, including the arms
        below that return a retryable outcome — retrying is the delivery tick's
        decision, made with the lease and the attempt budget in view, neither of
        which a transport can see.  ``agent_blocked`` in particular is herdr
        refusing *before any input is sent*, so the re-offer that follows it is
        the first submission and not a second.

        The mapping, and what each arm costs if it is wrong:

        =============================== ========================= ==============
        herdr says                      outcome                   bound
        =============================== ========================= ==============
        ``agent_prompted`` + ack state   ``DELIVERED``             none
        ``agent_blocked``                ``VETO_DIALOG``           veto ceiling
        ``agent_prompt_stalled``         ``SUBMISSION_UNCERTAIN``  ``dead_by``
        ``timeout``                      ``SUBMISSION_UNCERTAIN``  ``dead_by``
        transport failed AFTER flush     ``SUBMISSION_UNCERTAIN``  ``dead_by``
        transport failed BEFORE flush    ``VETO_UNVERIFIED``       attempt budget
        ``agent_not_found``              ``PANE_ABSENT``           attempt budget
        any other error code             ``VETO_UNVERIFIED``       attempt budget
        success, agent ALREADY busy      ``SUBMISSION_UNCERTAIN``  ``dead_by``
        success, state seq did not move  ``SUBMISSION_UNCERTAIN``  ``dead_by``
        =============================== ========================= ==============

        ``agent_blocked`` is ``VETO_DIALOG`` and not an attempt-budget outcome
        because it is the same condition the pane injector's dialog gate already
        names: a worker parked on a permission card is WAITING, not failing, and
        D12 bounds a wait by the veto ceiling.  Spending the attempt budget on it
        would kill a message at 325 s because a human had not answered a prompt.

        The unmapped default is ``VETO_UNVERIFIED`` for the reason
        ``PaneWorkerInjector._send`` gives for the same default: a submission
        whose result cannot be verified is a FAILING delivery, not a deferral.
        It is deliberately not ``SUBMISSION_UNCERTAIN`` — an unrecognised code is
        a herdr this build has not been certified against, which is a
        configuration fault that should die on the budget rather than sit open
        until ``dead_by``.
        """
        params: dict[str, Any] = {
            "target": target,
            "text": text,
            "wait": {
                "until": list(HERDR_PROMPT_ACK_STATES),
                "timeout_ms": wait_timeout_ms,
            },
        }
        try:
            async with self._one_shot() as send:
                before = await _read_state(send, target, self._state_read_timeout_s)
                result = await send("agent.prompt", params)
        except HerdrRequestError as exc:
            return _submission_from_error_code(exc.code, exc.message)
        except HerdrTransportError as exc:
            if exc.submitted:
                return PromptSubmission(
                    outcome=AttemptOutcome.SUBMISSION_UNCERTAIN,
                    detail=_detail("transport_after_submit"),
                )
            return PromptSubmission(
                outcome=AttemptOutcome.VETO_UNVERIFIED,
                detail=_detail("transport_before_submit"),
            )
        return _qualify_success(_submission_from_result(result), before)

    async def agent_state(self, *, target: str) -> PromptSubmission | None:
        """Read one agent's current status and state sequence, or ``None``.

        Not a submission — it reuses :class:`PromptSubmission` only as the
        carrier for the two fields the injector needs, with the outcome fixed at
        ``DELIVERED`` and meaningless.  It exists because the injector's
        no-second-submission rule needs to know whether an earlier UNCERTAIN
        submission has since resolved, and herdr's own ``state_change_seq`` is
        the only monotone evidence for that.

        Returns ``None`` when the agent cannot be read at all, which the caller
        reads as "no evidence either way" rather than as an answer.
        """
        try:
            result = await self.request_once("agent.get", {"target": target})
        except HerdrError:
            logger.debug("herdr agent.get failed for %s", target, exc_info=True)
            return None
        agent = result.get("agent")
        if not isinstance(agent, dict):
            return None
        return PromptSubmission(
            outcome=AttemptOutcome.DELIVERED,
            detail="agent_state",
            agent_status=_as_status(agent.get("agent_status")),
            state_change_seq=_as_seq(agent.get("state_change_seq")),
        )

    async def check_protocol(self) -> dict[str, Any]:
        """Read the live protocol number and refuse one this build does not pin.

        Returns the snapshot body on a match so a caller may inspect it; raises
        :class:`HerdrProtocolMismatch` otherwise.  This is the D7 pin made
        mechanical: the transport will not stream a wire format it was not
        certified against.

        The number is read from ``session.snapshot``, which carries ``protocol``
        and ``version`` alongside the pane records.  There is no separate schema
        method: herdr 0.9.0's API socket rejects ``api.schema`` as an unknown
        variant AND CLOSES THE CONNECTION, so a client that opened with it could
        never reach ``events.subscribe`` at all — the live round on grok-box-002
        found exactly that, as an unbroken run of subscription-gap events from a
        source that had never once connected.
        """
        snapshot = await self.snapshot()
        got_protocol = snapshot.get("protocol")
        # 0.9.0 reports no separate schema number, so an ABSENT one is not a
        # mismatch — but it is not a match either, and defaulting it to the pin
        # (r1) made the D7 check assert something it had not read. The pin is
        # carried by ``protocol``; ``schema_version`` is compared only when the
        # server actually sends it, so a future herdr that reintroduces it is
        # still checked rather than silently accepted.
        got_schema = snapshot.get("schema_version")
        schema_mismatch = got_schema is not None and got_schema != HERDR_SCHEMA_VERSION
        if got_protocol != HERDR_PROTOCOL or schema_mismatch:
            raise HerdrProtocolMismatch(
                got_protocol=int(got_protocol) if isinstance(got_protocol, int) else -1,
                got_schema=int(got_schema) if isinstance(got_schema, int) else -1,
            )
        return snapshot

    async def snapshot(self) -> dict[str, Any]:
        """Return the ``snapshot`` body of herdr's ``session.snapshot``.

        The recipe's second step: after subscribing, a caller snapshots to get the
        current pane/agent records, then applies buffered events in order.  Kept
        here because it is a plain request/reply on the same socket; the mapping
        of its records onto ``WorkerState`` is Seam A's, not this leaf's.

        The method name is ``session.snapshot``.  ``api.snapshot`` — which reads
        like the CLI's ``herdr api snapshot`` and is what this client shipped
        with — is not a method herdr 0.9.0 accepts; it is the CLI SUBCOMMAND that
        invokes this one, and the legacy inbox service reached the snapshot by
        shelling out to that subcommand rather than over the socket, which is why
        the difference went unnoticed until the H1 live round.
        """
        # On its OWN connection: a snapshot request on the streaming connection
        # would poison a later ``events.subscribe`` (see :meth:`request_once`).
        result = await self.request_once("session.snapshot")
        snapshot = result.get("snapshot")
        if not isinstance(snapshot, dict):
            raise HerdrTransportError(f"herdr session.snapshot carried no snapshot: {result!r}")
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
                if reply.get("id") == request_id:
                    ack = _envelope_result(reply)
                    self._subscribed = True
                    return ack
                if self._is_pushed_event(reply):
                    # An event that raced ahead of the ack: BUFFER it (§6). herdr
                    # does not push before the ack in practice, but if it does the
                    # event is held for events() rather than lost.
                    self._event_buffer.append(reply)

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
        # Replay any events buffered during the subscribe→snapshot handshake
        # FIRST, in arrival order, before reading new ones off the wire (§6
        # reconcile).  ``popleft`` drains oldest-first and each event leaves the
        # buffer exactly once, so an event that raced into the handshake window is
        # delivered exactly once, in order — never dropped, never duplicated.
        while self._event_buffer:
            yield self._event_buffer.popleft()
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
