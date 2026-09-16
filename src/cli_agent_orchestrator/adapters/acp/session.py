"""D5's two wire ports, implemented over :class:`AcpClient`.

Until this module existed, ``MessageTransport`` and ``AgentSession`` had no
implementation anywhere in ``src`` — the only ones were test fakes, so the
registry had nothing to build a task from and the plane could not deliver
anything. That is the gap the S1 review named as B1.4, and these are the product
objects the fakes were standing in for.

Both are ADAPTERS in the strict sense D6b(3) uses: they own bytes, stream
demultiplexing and the runtime active-turn handle, they receive no store, and
they write no journal or queue row. They return typed events and the application
owner records them. Nothing here decides anything.

The division between the two is D5's and is worth keeping in view: the transport
SUBMITS and probes, the session CANCELS and tears down. They wrap the same
client because one subprocess is one session, but they are separate ports so the
call graph shows which half a caller needs — and so a seat that may not be
cancelled simply is not handed the second one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from cli_agent_orchestrator.adapters.acp.client import AcpClient, PromptRefused
from cli_agent_orchestrator.core.interrupt import (
    ActiveTurnHandle,
    CancelHandle,
    CancelOutcome,
    CancelRaceLost,
    CancelSettlement,
    InterruptPreparation,
    PreparationKind,
    SessionState,
    SettleKind,
    SubmitEnvelope,
    SubmitReceipt,
)
from cli_agent_orchestrator.core.timing import ACP_TEARDOWN_POLL_S, ACP_WRITE_SETTLE_S

__all__ = ["AcpAgentSession", "AcpMessageTransport"]

#: A UNIT CONVERSION, not a duration. ``CancelSettlement.settle_ms`` is reported
#: in milliseconds because that is the scale the measurements live at (the whole
#: fleet settled inside 0.273 s), and the factor is named so no bare 1000 appears
#: beside a duration.
_MS_PER_SECOND = 1000


class AcpMessageTransport:
    """``core.ports.MessageTransport`` over one ACP subprocess.

    ``submit`` writes ONE prompt and reports the LOCAL write receipt. D5 calls
    that "accepted" and the word is doing careful work: the bytes flushed and the
    flush survived ``ACP_WRITE_SETTLE_S`` without a locally observable failure.
    It is not an acknowledgement from the agent, and the settle window is the
    only thing standing between "the pipe took it" and "the peer is dead" —
    fault injection measured the first write after peer death failing within
    0.011 s, so one second is generous for a DEAD peer and useless for a wedged
    live one. The design says so rather than letting a flushed pipe stand in for
    a read message.

    ``prepare_interrupt`` touches no wire at all. This client is the only reader
    of its own stream, so it is authoritative for ``idle | active(handle)`` and
    the answer is exact rather than probed.
    """

    def __init__(
        self,
        terminal_id: str,
        client: AcpClient,
        *,
        nudge: Callable[[str], None] | None = None,
        settle_s: float = ACP_WRITE_SETTLE_S,
    ) -> None:
        self._terminal_id = terminal_id
        self._client = client
        self._nudge = nudge
        self._settle_s = settle_s
        self._turn_seq = 0

    def submit(self, *, terminal_id: str, envelope: SubmitEnvelope) -> SubmitReceipt:
        del terminal_id  # this transport is bound to one terminal at construction
        try:
            self._client.prompt(envelope.body)
        except PromptRefused:
            # The client's own mid-turn guard fired on a race between a caller's
            # check and this write. Not accepted, not ambiguous: nothing left.
            return SubmitReceipt(accepted=False, detail="turn_open")
        except Exception as exc:  # noqa: BLE001 — a transport fault is a typed receipt
            return SubmitReceipt(accepted=False, detail=exc.__class__.__name__)

        flushed_at = datetime.now(UTC)
        self._turn_seq += 1

        # THE SETTLE WINDOW. Without it "accepted" would mean only that ``write``
        # returned, which it does even when the peer died a microsecond earlier;
        # the review's S1 was that this frozen literal was cited everywhere and
        # waited on nowhere. A locally observable failure inside the window makes
        # the write AMBIGUOUS — the bytes may have been read before the peer went
        # — and ambiguity is the honest answer, never a guess in either direction.
        deadline = time.monotonic() + self._settle_s
        while time.monotonic() < deadline:
            if not self._client.is_alive():
                return SubmitReceipt(
                    accepted=True,
                    write_flushed_at=flushed_at,
                    ambiguous=True,
                    detail="peer_lost_inside_settle_window",
                )
            # The plane's one local poll cadence, shared with teardown: both are
            # "how finely do we watch a bound we already have", and a second
            # number here would be a second opinion about that.
            time.sleep(ACP_TEARDOWN_POLL_S)

        return SubmitReceipt(
            accepted=True,
            write_flushed_at=flushed_at,
            write_receipt_at=datetime.now(UTC),
            detail="acp",
        )

    def prepare_interrupt(self, *, terminal_id: str) -> InterruptPreparation:
        """``IDLE`` or ``CANCEL_REQUIRED{active_turn}``.  A probe, not a dispatch."""
        del terminal_id
        state = self._client.session_state()
        if not state.turn_open or state.session_id is None:
            return InterruptPreparation(kind=PreparationKind.IDLE)
        return InterruptPreparation(
            kind=PreparationKind.CANCEL_REQUIRED,
            active_turn=ActiveTurnHandle(
                terminal_id=self._terminal_id,
                lifecycle_generation=self._client.lifecycle_generation,
                session_id=state.session_id,
                acp_request_id=str(state.open_request_id),
                callback_id=self._client.open_callback_id or "",
                turn_seq=self._turn_seq,
            ),
        )

    def nudge(self, terminal_id: str) -> None:
        """D7.2 — tell the tick this receiver just went idle.

        A callback rather than a tick reference, because an adapter that held the
        tick could reach the queue, and adapters write no queue row. It is
        optional: with no nudge wired the 65-second reclaim floor still delivers,
        which AC-S1.14 requires to remain true.
        """
        if self._nudge is not None:
            self._nudge(terminal_id)


class AcpAgentSession:
    """``core.ports.AgentSession`` over the same subprocess.

    ``cancel_if_current`` is the one method whose contract is a COMPARISON: it
    checks the actor's exact current handle immediately before writing cancel
    bytes and writes nothing on a mismatch. The gap between deciding to cancel
    and writing it is exactly where the turn can move, and no care at the call
    site can close it — cancelling the wrong turn is r18's "stale turn cancel"
    mutant and it is silent when it happens.
    """

    def __init__(self, terminal_id: str, client: AcpClient) -> None:
        self._terminal_id = terminal_id
        self._client = client

    def cancel_if_current(self, expected: ActiveTurnHandle) -> CancelOutcome:
        state = self._client.session_state()
        if not state.turn_open:
            return CancelRaceLost(observed_state=SessionState.IDLE, observed_turn=None)
        if (
            state.session_id != expected.session_id
            or str(state.open_request_id) != expected.acp_request_id
            or self._client.lifecycle_generation != expected.lifecycle_generation
        ):
            # A DIFFERENT turn is live. Write nothing and report what is there;
            # the caller re-prepares under the same reservation.
            return CancelRaceLost(observed_state=SessionState.ACTIVE, observed_turn=None)
        self._client.cancel()
        return CancelHandle(active_turn=expected, sent_at=datetime.now(UTC))

    def await_cancel(self, handle: CancelHandle, deadline: datetime) -> CancelSettlement:
        """Wait for the settle against the PERSISTED deadline, never a fresh one.

        ``deadline`` is the instant ``begin_cancel`` computed from the receiver
        task's single clock sample and wrote to the row. After a restart it is
        the only honest bound, because the sample that produced it is gone.
        """
        began = time.monotonic()
        remaining = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
        stop_reason = self._client.await_stop_reason(timeout=remaining)
        elapsed = time.monotonic() - began
        if stop_reason != "cancelled":
            # Includes ``end_turn`` — an adapter that settles with the WRONG stop
            # reason has not cancelled, and laundering it into ``cancelled`` is
            # what the certification row's typed reason exists to prevent.
            return CancelSettlement(
                kind=SettleKind.UNSETTLED, settle_ms=round(elapsed * _MS_PER_SECOND, 3)
            )
        return CancelSettlement(
            kind=SettleKind.CANCELLED,
            cancelled_acp_request_id=handle.active_turn.acp_request_id,
            settle_ms=round(elapsed * _MS_PER_SECOND, 3),
            dead_tool_call_ids=tuple(self._client.open_tool_call_ids()),
        )

    def close(self) -> bool:
        """``session/close`` where the row advertises it; ``False`` otherwise.

        ``False`` is not a failure — it is the honest answer for the adapters
        that have no close (D14 names kiro and cline), and it is what routes the
        caller to the terminate-and-respawn path instead.
        """
        return self._client.close_session()

    def terminate_process_group(self, *, grace_s: float) -> bool:
        return self._client.terminate_process_group(grace_s=grace_s)
