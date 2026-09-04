"""What legacy tells the shadow queue, as values (WP-ARCH phase 3a).

``app`` may not import legacy, so it cannot read the inbox table, the attempt
table or the mailbox row.  Every fact the mirror writer needs therefore arrives
as one of the values below, assembled on the legacy side and handed in.  That is
the same shape phase 1 used — a legacy hook builds an ``EventDraft`` and calls
``emit`` — and it buys the same two things: the new tree stays testable with no
database, and the comparison cannot be rigged, because this package has no way
to look at the other side's answer.

The vocabularies here are LEGACY's, deliberately unmapped.  Mapping happens once,
in :mod:`~app.delivery.mirror`, where each cell is argued.  Translating at the
collection site instead would scatter the argument across ``services/`` and
``clients/``, which is where the fork's existing delivery logic already lives in
six places that disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "LegacyAttempt",
    "LegacyEnqueue",
    "LegacyOutcome",
    "LegacyVeto",
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


@dataclass(frozen=True)
class LegacyAttempt:
    """One row of ``inbox_delivery_attempt``, as legacy settled it.

    ``ordinal`` is the attempt's position in the row's own history, oldest
    first.  It becomes the shadow attempt's ``claim_id``, which needs saying
    because it looks like a type pun and is not: a shadow row is never leased, so
    no real claim ever issues a token for it, and the ordinal is both unique per
    message and deterministic — which is what makes recording the same legacy
    attempt twice a no-op rather than a second row.

    ``outcome`` and ``reason`` are legacy's plain strings (``settle_delivery_
    attempt`` takes ``outcome: str`` with no enum), preserved verbatim so the
    mapping can be argued in one place and audited from ``detail`` afterwards.
    """

    ordinal: int
    outcome: str
    started_at: datetime
    carrier: str = "legacy"
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LegacyOutcome:
    """What became of one inbox row, and how it got there.

    Collected by re-reading the legacy row rather than by trusting the edge that
    triggered the observation.  Several legacy writers can end a row — the
    delivered compare-and-set, the expiry sweep, the F578 supersession, three
    separate cancel sites — and they do not fire in a guaranteed order.  Reading
    the CURRENT status makes a missed edge self-correcting: the next observation
    of that row, from any edge, still sees the truth.
    """

    legacy_message_id: int
    status: str
    failure_reason: str | None = None
    attempts: tuple[LegacyAttempt, ...] = ()


@dataclass(frozen=True)
class LegacyVeto:
    """An injection the legacy path declined before any attempt was opened.

    §7a requires a veto be RECORDED rather than dropped, because a dropped veto
    is the difference between "we tried and were refused" and "nothing happened",
    and the second is what made #604 unreadable from the stored rows.

    ``reason`` is one of ``InjectSafetyResult``'s reasons.  ``gate_episode``
    carries the dialog gate's episode identifier when there was one, which is
    the fact that distinguishes a genuine dialog hold from a probe that could not
    be verified.
    """

    legacy_message_ids: tuple[int, ...]
    reason: str
    at: datetime
    gate_episode: str | None = None
    context: dict[str, str] = field(default_factory=dict)
