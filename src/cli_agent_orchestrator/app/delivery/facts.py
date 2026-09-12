"""What legacy tells the queue, as values (WP-ARCH phase 3).

``app`` may not import legacy, so it cannot read the inbox table, the attempt
table or the mailbox row.  The one fact the write-through needs therefore arrives
as the value below, assembled on the legacy side and handed in.  That is the same
shape phase 1 used — a legacy hook builds an ``EventDraft`` and calls ``emit`` —
and it buys the new tree staying testable with no database.

Sub-phase 3a passed four more facts this way, for the observational mirror that
compared the queue against legacy without serving anything.  That mode is retired
(#738) and they went with it: a flag flip is accepted by a grok-box live round
now, not by a dark deployment writing copies beside the real traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "LegacyEnqueue",
]


@dataclass(frozen=True)
class LegacyEnqueue:
    """One inbox row, as it stands immediately after its insert commits.

    ``receiver_id`` is the DURABLE mailbox id where the legacy row has one, and
    the terminal id otherwise.  §5 item 2 makes that the queue's addressing rule
    and it is what closes #33: a fresh incarnation is a new generation of the
    same mailbox and inherits the pending rows, rather than starting behind an
    empty registry.  The legacy row carries both — ``logical_receiver_id`` and
    ``receiver_id`` — and the collector picks the mailbox id when it is there.

    ``content_hash`` is legacy's own F475 key, copied rather than recomputed.
    Recomputing it here would mean two implementations of one normalisation, free
    to disagree about whether a frozen-pin attestation counts as content.

    ``created_at`` is the legacy row's, not the observation's: the queue's
    deadline runs from message creation, and a mirror that stamped its own
    arrival time would measure the hook's latency instead.
    """

    legacy_message_id: int
    receiver_id: str
    sender_id: str
    message: str
    status: str
    created_at: datetime
    orchestration_type: str = ""
    is_callback: bool = False
    expire_after_s: int | None = None
    supersede_key: str | None = None
    content_hash: str | None = None
    park_warm: bool = False
    barrier_id: int | None = None
    barrier_member_key: str | None = None
    enqueue_generation: int | None = None
    idempotency_key: str | None = None
