"""D15 / AC-S1.23 — an interrupt, folded by ``interrupt_id``, end to end.

AC-S1.23's fails-if is one sentence: *an interrupt is indistinguishable from an
ordinary delivery.* Before this module that was exactly true — ``cao diag``
showed a queue row and its attempts, and nothing anywhere said who cut whose
turn, when, or what tool call died with it.

**The interrupt id IS the callback id.** There is no second identifier (r13,
review r12 B5), so the fold key is the thing the caller already holds.

**Every phase has exactly ONE authoritative source**, and that is the property
this module is built around rather than a consequence of it. The fold reads:

* the interrupt's queue row and its dead-letter reason — the terminal phases;
* the typed ``delivery_attempt.detail`` — the sole source of
  ``prompt_ambiguous`` (r13), because an ambiguous write leaves no other trace;
* the ``interrupt_state`` row while one exists — the live phases.

It does NOT read the frame log. AC-S1.23's fails-if includes "attribution needs
the frame log": the frames are the adapter's evidence for what the WIRE did, and
a diagnosis that required them could not answer after a restart, when the
subprocess and its stream are gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cli_agent_orchestrator.core.delivery import DeadReason
from cli_agent_orchestrator.core.interrupt import AuditPhase, InterruptPhase

__all__ = [
    "PHASE_SOURCES",
    "InterruptFold",
    "fold_interrupt",
    "render_interrupt",
]

#: AC-S1.23's r13 check as DATA: every phase in the closed set, and the ONE
#: place its value may come from. A phase with two sources is a phase whose
#: answer depends on read order; a phase with none is one ``cao diag`` cannot
#: report. The static arm asserts this covers ``AuditPhase`` exactly.
PHASE_SOURCES: dict[str, str] = {
    AuditPhase.ADMITTED.value: "interrupt_state",
    AuditPhase.CLAIMED.value: "interrupt_state",
    AuditPhase.IDLE.value: "delivery_msg",
    AuditPhase.CANCELLED.value: "delivery_msg",
    AuditPhase.CANCEL_TIMEOUT.value: "delivery_dead",
    AuditPhase.PROMPT_AMBIGUOUS.value: "delivery_attempt.detail",
    AuditPhase.PENDING_EXPIRED.value: "delivery_dead",
    AuditPhase.WINDOW_LOST.value: "delivery_dead",
}

#: Which dead-letter reason means which terminal phase. The three no-dispatch
#: terminals ride ``kind=dead`` facts (D6b(7)), so this is where they are read.
_DEAD_REASON_PHASE = {
    DeadReason.INTERRUPT_CANCEL_TIMEOUT.value: AuditPhase.CANCEL_TIMEOUT.value,
    DeadReason.INTERRUPT_UNCLAIMED.value: AuditPhase.PENDING_EXPIRED.value,
    DeadReason.INTERRUPT_WINDOW_LOST.value: AuditPhase.WINDOW_LOST.value,
}


@dataclass(frozen=True)
class InterruptFold:
    """One interrupt, as ``cao diag`` reports it.

    Fields are NULLABLE BY PHASE and that is deliberate (D6b(7)): an idle
    interrupt has no cancelled request and no cut; a timed-out one has no
    presentation. Rendering a zero where there is no value would make "none" and
    "not applicable" the same reading.
    """

    interrupt_id: str
    found: bool = False
    phase: str | None = None
    phase_source: str | None = None
    principal: str | None = None
    terminal_id: str | None = None
    cut_callback_id: str | None = None
    cut_disposition: str | None = None
    cancelled_acp_request_id: str | None = None
    dead_tool_call_ids: tuple[str, ...] = ()
    admitted_at: datetime | None = None
    latency_ms: float | None = None
    latency_breach: bool = False
    live_phase: str | None = None
    detail: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def fold_interrupt(connection: Any, interrupt_id: str) -> InterruptFold:
    """Fold one interrupt by its callback id.  Never raises on a missing row.

    A missing interrupt answers ``found=False`` rather than raising, for the
    reason every other diag read does: an operator typing an id that turns out to
    be an ordinary callback should be told that, not handed a traceback.
    """
    ledger = connection.execute(
        "SELECT principal, terminal_id, admitted_at, forced, proxied_optin "
        "FROM interrupt_ledger WHERE interrupt_id = ?",
        (interrupt_id,),
    ).fetchone()
    message = connection.execute(
        "SELECT msg_id, receiver_id, state, urgency FROM delivery_msg " "WHERE idempotency_key = ?",
        (f"interrupt:{interrupt_id}",),
    ).fetchone()

    if ledger is None and message is None:
        return InterruptFold(interrupt_id=interrupt_id, found=False, detail="no such interrupt")

    fold: dict[str, Any] = {"interrupt_id": interrupt_id, "found": True}
    notes: list[str] = []
    if ledger is not None:
        fold["principal"] = ledger["principal"]
        fold["terminal_id"] = ledger["terminal_id"]
        fold["admitted_at"] = ledger["admitted_at"]
        if ledger["forced"]:
            notes.append("forced: the viewer waived the quota bounds")
        if ledger["proxied_optin"]:
            notes.append("proxied: admitted under CAO_VIEWER_LOCAL_PRINCIPAL_PROXIED")

    phase: str | None = None
    source: str | None = None

    if message is not None:
        fold["terminal_id"] = fold.get("terminal_id") or message["receiver_id"]
        # The typed attempt detail FIRST: it is the sole source of
        # ``prompt_ambiguous``, and an ambiguous write also leaves the row dead,
        # so reading the dead-letter first would report the wrong phase.
        attempt = connection.execute(
            "SELECT outcome, detail FROM delivery_attempt WHERE msg_id = ? "
            "ORDER BY claim_id DESC, rowid DESC LIMIT 1",
            (message["msg_id"],),
        ).fetchone()
        if attempt is not None and "prompt_ambiguous" in str(attempt["detail"] or ""):
            phase, source = AuditPhase.PROMPT_AMBIGUOUS.value, "delivery_attempt.detail"

        if phase is None and message["state"] == "dead":
            dead = connection.execute(
                "SELECT reason FROM delivery_dead WHERE msg_id = ?", (message["msg_id"],)
            ).fetchone()
            reason = str(dead["reason"]) if dead is not None else ""
            phase = _DEAD_REASON_PHASE.get(reason)
            source = "delivery_dead"
            if phase is None and reason:
                notes.append(f"died for a non-interrupt reason: {reason}")
        if phase is None and message["state"] == "delivered":
            phase, source = AuditPhase.CANCELLED.value, "delivery_msg"

    state = connection.execute(
        "SELECT phase, cut_callback_id, active_turn_request_id, deadline "
        "FROM interrupt_state WHERE terminal_id = ?",
        (fold.get("terminal_id"),),
    ).fetchone()
    if state is not None:
        fold["live_phase"] = state["phase"]
        fold["cut_callback_id"] = state["cut_callback_id"]
        fold["cancelled_acp_request_id"] = state["active_turn_request_id"]
        if phase is None and state["phase"] != InterruptPhase.NONE.value:
            phase, source = AuditPhase.CLAIMED.value, "interrupt_state"

    if phase is None:
        phase, source = AuditPhase.ADMITTED.value, "interrupt_state"

    fold["phase"] = phase
    fold["phase_source"] = source
    if fold.get("cut_callback_id"):
        # An unnamed cut is QUARANTINED: I records the cut and the unresolved-cut
        # view derives the warning from I, with no mutation to N (D6b(4)).
        fold["cut_disposition"] = "quarantined"
    fold["notes"] = tuple(notes)
    return InterruptFold(**fold)


def render_interrupt(fold: InterruptFold) -> str:
    """One interrupt, as text.  Nullable fields print ``-``, never ``0``."""
    if not fold.found:
        return f"interrupt {fold.interrupt_id}: not found ({fold.detail})"

    def _or_dash(value: object) -> str:
        return "-" if value in (None, "", ()) else str(value)

    lines = [
        f"interrupt {fold.interrupt_id}",
        f"  phase            {_or_dash(fold.phase)}  (source: {_or_dash(fold.phase_source)})",
        f"  principal        {_or_dash(fold.principal)}",
        f"  terminal         {_or_dash(fold.terminal_id)}",
        f"  admitted_at      {_or_dash(fold.admitted_at)}",
        f"  cut callback     {_or_dash(fold.cut_callback_id)}"
        f"  ({_or_dash(fold.cut_disposition)})",
        f"  cancelled turn   {_or_dash(fold.cancelled_acp_request_id)}",
        f"  dead tool calls  {_or_dash(', '.join(fold.dead_tool_call_ids))}",
        f"  live phase       {_or_dash(fold.live_phase)}",
    ]
    if fold.latency_ms is not None:
        breach = "  BREACH" if fold.latency_breach else ""
        lines.append(f"  latency_ms       {fold.latency_ms}{breach}")
    lines.extend(f"  note             {note}" for note in fold.notes)
    return "\n".join(lines)
