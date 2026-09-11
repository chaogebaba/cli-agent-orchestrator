"""What is left of the delivery ladder: one DB predicate (WP-ARCH 3c K7).

This module was 2022 lines and the home of §5c's obligation ladder — rung 1's
native re-push, rung 2's pane nudge, the escalation display-message, the stranded
and re-resolve sweeps, the pending indicator, the health warnings, and
``convergence_tick`` that drove them all. Slice 3 deletes every one of them.

**Why the whole ladder goes, and why nothing replaces it.** The ladder existed to
get a message in front of a receiver that had not acknowledged one, by escalating
through progressively louder carriers. The queue does that job with a lease and
an attempt budget: a row is re-offered once per lease period and dies on a bound,
and every re-offer is a durable row rather than a decision replayed from memory.
Keeping both meant two escalation authorities over one message, which is the
duplicate-delivery family this phase closes.

**The blueprint's A2.3 said this file was the §5c tick's home; it was already
not.** ``convergence_tick`` returned immediately whenever the queue owned
delivery, and its only production driver was a member of the watchdog's own kill
list. The real owner is ``app/delivery/tick.py``. Amendment A3 records the
correction, and this file's reduction is what it looks like in code.

**What survives, and why only this.** ``is_target_confirmed_dead`` is a
fourteen-line DB predicate with one external caller
(``services/conversation_reconcile.py``). It reads a tombstone; it has nothing to
do with the ladder and would have to be rewritten somewhere else to delete it
here. Everything else in the file had either no caller outside itself or a caller
that dies in the same phase.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["is_target_confirmed_dead"]


def is_target_confirmed_dead(terminal_id: str, db: Session) -> bool:
    """D6: DB-only deadness check. No tmux call. ``db`` is REQUIRED (S2 — no default)."""
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel

    # A terminal is confirmed dead if a tombstone exists for its current generation
    # and no newer activated incarnation exists.
    tombstone = (
        db.query(PaneExitTombstoneModel.id)
        .filter(PaneExitTombstoneModel.terminal_id == terminal_id)
        .first()
    )
    return tombstone is not None
