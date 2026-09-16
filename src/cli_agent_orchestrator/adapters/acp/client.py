"""D3 — a newline-delimited JSON-RPC 2.0 client for ACP, and nothing more.

Four properties are worth reading this file for:

* **The client is the only reader of its own stream**, so it is authoritative for
  ``idle | active(handle)`` and :meth:`AcpClient.session_state` needs no probe on
  the wire.  D6b(3) makes that the basis of ``prepare_interrupt``: the driver
  answers from memory, touches no wire, and the answer is exact.

* **CAO tracks mid-turn itself; there is no wire busy class to branch on.**  S0's
  AC-S0.3c/S0.4 disproved the ``-32003`` model the blueprint was originally built
  on, so :meth:`prompt` refuses to write a second prompt while a turn is open,
  from this client's own state.  AC-S1.19 asserts that from the FRAME LOG, not
  from the absence of errors, and raises ``DIAG-ACP-BUSY-LEAK`` if a busy-shaped
  error is ever received anyway.

* **Permissions select by KIND, never by ``optionId``.**  AC-S0.9 measured the
  fleet: all five adapters that ask supply exactly ``allow_once`` /
  ``allow_always`` / ``reject_once``, and **no adapter offers
  ``reject_always``** — "deny and remember" is not expressible over ACP today.
  ``optionId`` is not portable either (codex answers
  ``accept_execpolicy_amendment``), so an id-based selector is a per-vendor
  branch waiting to happen.

* **Every frame is logged verbatim, both directions.**  AC-S1.2 requires it,
  AC-S1.19 and AC-S1.21 are asserted from it, and a transport whose evidence is
  reconstructed after the fact is not evidence.

Cancellation is core v1 and needs no capability gate for its WIRE SHAPE, but the
fleet diverged on BEHAVIOUR in every arm S0 measured — so whether a row may be
interrupted at all is a certification question, answered elsewhere, never here.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "ACP_PROTOCOL_VERSION",
    "AcpClient",
    "AcpFrameLog",
    "DENIAL_KINDS",
    "PERMISSION_KINDS",
    "PromptRefused",
    "SessionSnapshot",
    "select_permission_option",
]

#: D4 — v1 is FROZEN.  v2 only behind an explicit negotiation flag and a
#: per-agent capability probe, and neither exists yet: AC-S0.6 confirmed both
#: published SDK versions pin protocol 1, so there is nothing to negotiate with.
ACP_PROTOCOL_VERSION = 1

#: The option kinds AC-S0.9 measured across the fleet, in the order the design
#: prefers them.  ``reject_always`` is deliberately ABSENT: no adapter offers it,
#: so "deny and remember" cannot be expressed and the certification row records
#: that rather than the design pretending otherwise.
PERMISSION_KINDS = ("allow_once", "allow_always", "reject_once")

#: The kinds that mean NO.  D11's timeout path selects one of these; where an
#: agent offered none, the caller must send ``session/cancel`` and then answer
#: ``cancelled`` — the only representable no-answer outcome, made conformant by
#: actually cancelling first.
DENIAL_KINDS = ("reject_once", "reject_always")


class PromptRefused(RuntimeError):
    """A second ``session/prompt`` was attempted while this client's turn is open.

    Raised rather than queued, and rather than sent.  AC-S1.3's fails-if is "a
    second prompt is sent"; the row that provoked it stays ``in_flight``, is
    re-claimed under a fresh fence with NO attempt consumed, and is delivered on
    the first ``stopReason``.  Making this loud here is what keeps that decision
    in the delivery tick where it belongs instead of in an agent's error handler.
    """


@dataclass
class AcpFrameLog:
    """Every frame, both directions, with a monotonic and a wall timestamp.

    Written eagerly and line-buffered: a log flushed at close is empty exactly
    when the process died, which is the case the evidence is for.
    """

    path: Path
    _handle: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", buffering=1, encoding="utf-8")

    def record(self, direction: str, frame: object, note: str | None = None) -> None:
        record: dict[str, Any] = {
            "wall": datetime.now(UTC).isoformat(),
            "dir": direction,
            "frame": frame,
        }
        if note:
            record["note"] = note
        self._handle.write(json.dumps(record, default=str) + "\n")

    def frames(self) -> list[dict[str, Any]]:
        """Read the log back.  The evidence AC-S1.19 and AC-S1.21 assert over."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class SessionSnapshot:
    """What this client knows about its own session, from its own stream.

    ``turn_open`` is the busy model in full: a prompt was written and no
    ``stopReason`` has been seen for it.  There is no wire fact to consult and no
    error class to catch — S0 proved the fleet reports neither.
    """

    session_id: str | None
    turn_open: bool
    open_request_id: int | None
    last_stop_reason: str | None
    steering_advertised: bool


def select_permission_option(options: list[dict[str, Any]], *, prefer: str) -> str | None:
    """Pick an option by KIND, falling back within the same intent.

    ``prefer`` is a kind, never an id.  AC-S0.9's finding is the reason: the
    kinds are portable across all five adapters that ask, and the ids are not —
    codex answers ``accept_execpolicy_amendment`` where the others answer
    ``allow``.  Selecting by id therefore works until the second adapter.

    Returns ``None`` when the agent offered nothing of the requested intent,
    which is a real answer and not an error: D11's timeout path uses exactly that
    ``None`` to decide it must cancel first and then answer ``cancelled``.
    """
    for option in options:
        if option.get("kind") == prefer:
            return str(option.get("optionId"))
    family = DENIAL_KINDS if prefer in DENIAL_KINDS else PERMISSION_KINDS
    for option in options:
        if option.get("kind") in family:
            return str(option.get("optionId"))
    return None


class AcpClient:
    """One ACP agent subprocess, framed over stdio.

    Threading is deliberately plain: one reader thread owns stdout and every
    other method is called from the receiver task.  The reader never writes the
    store and never blocks on anything but the pipe, so a wedged agent costs one
    parked thread rather than a stalled tick.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        frame_log: AcpFrameLog,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        permission_policy: Callable[[dict[str, Any]], str] | None = None,
        stderr_path: Path | None = None,
    ) -> None:
        self._argv = list(argv)
        self._log = frame_log
        self._cwd = cwd or os.getcwd()
        self._env = env
        self._stderr_path = stderr_path
        self._permission_policy = permission_policy or (lambda _params: "allow_once")

        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._turn_open = False
        self._open_request_id: int | None = None
        self._last_stop_reason: str | None = None
        self._steering_advertised = False
        self._stop_events: queue.Queue[str] = queue.Queue()
        self._updates: list[dict[str, Any]] = []
        self._busy_leaks: list[dict[str, Any]] = []
        self._stderr_handle: Any = None
        # D22: bumped by a respawn, so a handle minted against the old subprocess
        # cannot authorize a cancel against the new one. Carried here because the
        # actor is the only thing that knows a subprocess was replaced.
        self._lifecycle_generation = 0
        #: The callback id of the turn now in flight, set by the caller that
        #: wrote it. The wire has no field for it, so it is CAO's own bookkeeping
        #: and it is what lets a cut be named without reading the frame log.
        self._open_callback_id: str | None = None
        #: Tool calls the agent has opened and not closed in THIS turn, in the
        #: order they arrived. A cancel names them so D6b's cost is auditable.
        self._open_tool_calls: list[str] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> AcpClient:
        stderr = None
        if self._stderr_path is not None:
            self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_handle = self._stderr_path.open("wb")
            stderr = self._stderr_handle
        self._proc = subprocess.Popen(  # noqa: S603 — argv is built by the launch spec
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            cwd=self._cwd,
            env=self._env,
            bufsize=0,
            start_new_session=True,  # its own process group, so teardown can kill the GROUP
        )
        self._log.record("meta", {"argv": self._argv, "cwd": self._cwd})
        threading.Thread(target=self._read_loop, daemon=True).start()
        return self

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def lifecycle_generation(self) -> int:
        """D22's generation.  A respawn bumps it; a handle from before does not match."""
        return self._lifecycle_generation

    @property
    def open_callback_id(self) -> str | None:
        return self._open_callback_id

    def open_tool_call_ids(self) -> tuple[str, ...]:
        """The tool calls still open in this turn, in arrival order.

        Rendered HONESTLY as zero, one or many: AC-S1.23's fails-if includes "a
        multi-tool cancel is rendered as one", so the caller gets the list and
        not a count or a first element.
        """
        return tuple(self._open_tool_calls)

    def is_alive(self) -> bool:
        """Is the subprocess still running?

        The only locally observable evidence a write receipt can rest on. It
        detects a DEAD peer and says nothing about a wedged live one — the
        asymmetry ``ACP_WRITE_SETTLE_S`` was measured against.
        """
        return self._proc is not None and self._proc.poll() is None

    def bump_lifecycle_generation(self) -> int:
        """Record that this terminal's subprocess was replaced (D22)."""
        self._lifecycle_generation += 1
        return self._lifecycle_generation

    def close_session(self) -> bool:
        """``session/close`` where the agent advertises it.

        ``False`` for an adapter that does not — D14 names kiro and cline — which
        is not a failure but the fact that routes recovery to terminate-and-
        respawn instead of to a clean close.
        """
        if self._session_id is None:
            return False
        reply = self.request("session/close", {"sessionId": self._session_id}, timeout=15.0)
        return "result" in reply

    def session_state(self) -> SessionSnapshot:
        """This client's own answer, from its own stream.  No wire touch."""
        return SessionSnapshot(
            session_id=self._session_id,
            turn_open=self._turn_open,
            open_request_id=self._open_request_id,
            last_stop_reason=self._last_stop_reason,
            steering_advertised=self._steering_advertised,
        )

    @property
    def busy_leaks(self) -> list[dict[str, Any]]:
        """Busy-shaped errors received from the wire.  Expected to stay EMPTY.

        ACP has no busy class, so a frame that looks like one means an adapter
        invented one and the plane's model of that adapter is wrong.  It is
        surfaced rather than swallowed: ``DIAG-ACP-BUSY-LEAK`` is AC-S1.19's
        negative arm.
        """
        return list(self._busy_leaks)

    # -- the wire -----------------------------------------------------------

    def initialize(self, *, client_name: str = "cao", timeout: float = 90.0) -> dict[str, Any]:
        """Negotiate v1 and record the WHOLE ``_meta``, not just ``_meta.steering``.

        F4b: steering capability is DISCOVERED, never assumed, and two of its
        three wire shapes are vendor-namespaced.  Recording only the field the
        design expects would make an agent that advertises steering under its own
        namespace look like an agent that has none.
        """
        reply = self.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                "clientInfo": {"name": client_name, "version": "1"},
            },
            timeout=timeout,
        )
        result = reply.get("result") or {}
        blob = json.dumps(result)
        self._steering_advertised = "steering" in blob
        return reply

    def session_new(
        self, *, cwd: str, mcp_servers: list[dict[str, Any]] | None = None, timeout: float = 90.0
    ) -> dict[str, Any]:
        """Open a session.  ``mcpServers`` is D10's outbound-only MCP entry point."""
        reply = self.request(
            "session/new",
            {"cwd": cwd, "mcpServers": mcp_servers or []},
            timeout=timeout,
        )
        session_id = (reply.get("result") or {}).get("sessionId")
        if session_id:
            self._session_id = str(session_id)
        return reply

    def prompt(self, text: str, *, callback_id: str | None = None) -> int:
        """Write ONE prompt and return its request id.  Refuses while a turn is open.

        The refusal is the client-side busy model in one line.  AC-S1.19's
        assertion is over the frame log — CAO never sends a second
        ``session/prompt`` while its own state says mid-turn — and this is the
        only place that could violate it.
        """
        if self._turn_open:
            raise PromptRefused(
                "a turn is already open on this session; the row stays in_flight and is "
                "delivered on the first stopReason (AC-S1.3)"
            )
        request_id = self._send_request(
            "session/prompt",
            {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )
        self._turn_open = True
        self._open_request_id = request_id
        self._last_stop_reason = None
        self._open_callback_id = callback_id
        # A new turn starts with no open tool calls. Cleared here rather than on
        # settle so a cancel that never settles still reports the calls THIS turn
        # opened rather than the previous turn's.
        self._open_tool_calls = []
        return request_id

    def steer(self, text: str) -> None:
        """Rung 1's wire shape: a notification, never a second prompt.

        Sent only where the capability was DISCOVERED at ``initialize``; the
        caller checks, because refusing here would turn a capability question
        into a transport error.
        """
        self.notify(
            "_session/steering",
            {"sessionId": self._session_id, "content": [{"type": "text", "text": text}]},
        )

    def cancel(self) -> None:
        """``session/cancel`` for the session's current turn.

        A NOTIFICATION per the spec: the agent MUST stop model requests and tool
        calls, answer pending permissions ``cancelled``, and settle the original
        prompt with the ``cancelled`` stop reason.  The settle is what the caller
        awaits — against the PERSISTED deadline, never a recomputed one.
        """
        self.notify("session/cancel", {"sessionId": self._session_id})

    def await_stop_reason(self, timeout: float) -> str | None:
        """Block for the next ``stopReason``, or ``None`` if the deadline passes.

        ``None`` is the honest answer for an unsettled cancel, and the caller
        quarantines rather than assuming.  A timeout that returned ``"cancelled"``
        would make an agent that never answered indistinguishable from one that
        did, which is precisely the cell S0 measured as FAIL on four adapters.
        """
        try:
            return self._stop_events.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- framing ------------------------------------------------------------

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 120.0
    ) -> dict[str, Any]:
        request_id = self._send_request(method, params)
        with self._lock:
            pending = self._pending[request_id]
        try:
            return pending.get(timeout=timeout)
        except queue.Empty:
            self._log.record("note", {"timeout": method, "id": request_id})
            return {"id": request_id, "method": method, "timeout": True}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _send_request(self, method: str, params: dict[str, Any] | None) -> int:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = queue.Queue()
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return request_id

    def _write(self, frame: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("the ACP subprocess is not running")
        self._log.record(">>", frame)
        self._proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                self._log.record("<<raw", line)
                continue
            self._log.record("<<", frame)
            self._dispatch(frame)
        self._log.record("meta", {"stdout": "closed", "returncode": self._proc.poll()})
        # The stream ending is itself a turn-ending fact.  Leaving ``turn_open``
        # set would make a dead agent look permanently busy and would stall every
        # row addressed to it behind a stopReason that can never arrive.
        if self._turn_open:
            self._settle_turn("transport_closed")

    def _dispatch(self, frame: dict[str, Any]) -> None:
        if "id" in frame and "method" in frame:
            self._answer_agent_request(frame)
            return
        if "id" in frame:
            self._settle_reply(frame)
            return
        if frame.get("method") == "session/update":
            self._updates.append(frame)
            update = (frame.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate")
            tool_call_id = update.get("toolCallId")
            if kind == "tool_call" and tool_call_id:
                self._open_tool_calls.append(str(tool_call_id))
            elif kind == "tool_call_update" and tool_call_id:
                # Anything but an in-flight status closes it. A cancelled call
                # stays OPEN in this list on purpose: it is one of the calls the
                # cancel killed, and naming it is the point.
                if str(update.get("status", "")) in ("completed", "failed"):
                    self._open_tool_calls = [
                        call for call in self._open_tool_calls if call != str(tool_call_id)
                    ]
            if kind == "stop":
                self._settle_turn(str(update.get("stopReason") or "stop"))

    def _settle_reply(self, frame: dict[str, Any]) -> None:
        error = frame.get("error") or {}
        message = str(error.get("message", "")).lower()
        if error and ("busy" in message or error.get("code") == -32003):
            # ACP defines no busy class.  A frame shaped like one means an
            # adapter invented one; record it loudly and keep the client's own
            # state authoritative rather than branching on a vendor error.
            self._busy_leaks.append(frame)
            self._log.record("note", {"DIAG-ACP-BUSY-LEAK": frame})
        result = frame.get("result") or {}
        if frame.get("id") == self._open_request_id:
            if "stopReason" in result:
                self._settle_turn(str(result["stopReason"]))
            elif error:
                # A prompt that came back an ERROR ends the turn too, and this
                # line is here because a live round on 2026-09-16 proved it does
                # not go without saying: claude-agent-acp answered a prompt with
                # ``authentication_failed`` and, with only the ``stopReason``
                # branch, this client held ``turn_open`` forever. Every later row
                # for that receiver would then park ``acp_busy_retry`` against an
                # agent that was not busy but broken, until each one aged out at
                # its own deadline — a silent stall with no failing assertion
                # anywhere, which is the exact shape the plane exists to remove.
                #
                # Named distinctly from a real stop reason so the fold can tell
                # "the turn ended" from "the turn never started".
                self._settle_turn(f"error:{error.get('code', 'unknown')}")
        with self._lock:
            pending = self._pending.get(int(frame["id"]))
        if pending is not None:
            pending.put(frame)

    def _settle_turn(self, stop_reason: str) -> None:
        self._turn_open = False
        self._open_request_id = None
        self._last_stop_reason = stop_reason
        self._stop_events.put(stop_reason)

    def _answer_agent_request(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        params = frame.get("params") or {}
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": frame["id"]}
        if method == "session/request_permission":
            prefer = self._permission_policy(params)
            option_id = select_permission_option(params.get("options") or [], prefer=prefer)
            if option_id is None:
                # D11: where the agent offered nothing of the requested intent,
                # CANCEL FIRST and then answer ``cancelled`` — the only
                # representable no-answer outcome, made conformant by the cancel.
                self.cancel()
                reply["result"] = {"outcome": {"outcome": "cancelled"}}
            else:
                reply["result"] = {"outcome": {"outcome": "selected", "optionId": option_id}}
        else:
            reply["result"] = {}
        self._write(reply)

    # -- teardown -----------------------------------------------------------

    def terminate_process_group(self, *, grace_s: float) -> bool:
        """SIGTERM the GROUP, wait at most ``grace_s``, SIGKILL, prove absence.

        The group, not the process: an adapter launched through ``npx`` is a
        shell wrapping a node process, and terminating only the wrapper leaves
        the agent alive.  The SIGKILL leg is mandatory rather than defensive —
        S0 measured an adapter that never exits on SIGTERM at all — and the
        return value is the PROOF the recovery finalizer requires, not a request.
        """
        import signal
        import time

        from cli_agent_orchestrator.core.timing import ACP_TEARDOWN_POLL_S

        if self._proc is None:
            return True
        pgid = os.getpgid(self._proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            time.sleep(ACP_TEARDOWN_POLL_S)
        if self._proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._proc.wait(timeout=grace_s)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        return False

    def close(self) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        self._log.close()
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
