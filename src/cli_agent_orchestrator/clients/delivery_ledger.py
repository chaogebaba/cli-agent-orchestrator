"""F642 — delivery-ledger spine: enums and pure decision logic.

This module holds the *vocabulary* and the *pure* (DB-free) decision logic of the
delivery-ledger spine designed in
``orchestrator/blueprints/f642-delivery-ledger-spine.md``. The SQLAlchemy tables
live in :mod:`cli_agent_orchestrator.clients.database` (they must share that
module's ``Base.metadata``); everything here is import-cheap and unit-testable in
isolation, which is what lets the condition de-dup rule (D7/AC21) and the
exhaustion rule (D2/AC19/AC23) be asserted as pure functions.

The design in one sentence per structure (blueprint §2):

* ``delivery_ledger`` — one authoritative row per message id (PK ``message_id``),
  carrying ``state``, ``applicable_carriers``, first-emission/ack facts,
  ``suppressed_reason``, and ``blocked_reason``/``blocked_since``.
* ``delivery_emission`` — append-only carrier log, ``UNIQUE(message_id, carrier)``;
  the row is the CLAIM, the ``outcome`` is the RESULT (D3/S2).
* ``condition_ledger`` — append-only decision log (autoincrement PK), one row per
  ``ConditionDelivery.deliver()`` exit; de-dup is a RULE over the log, not a
  constraint in it (D7).
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, NamedTuple, Optional, Sequence


# ─── delivery_ledger.state (D1/D2/D13) ─────────────────────────────────────────
class LedgerState(str, Enum):
    """The authoritative per-id delivery state (blueprint §2, D1/D2/D13).

    ``pending`` → ``emitted`` → ``acked`` is the happy path. ``suppressed`` and
    ``undeliverable`` are the terminal decline states (D2's exhaustion rule).
    ``superseded`` and ``expired`` mirror the ``InboxModel.status`` terminal
    states a message can reach with no carrier acting (D13) — a ledger that omits
    them would leave those rows reading ``pending`` forever.
    """

    PENDING = "pending"
    EMITTED = "emitted"
    ACKED = "acked"
    SUPPRESSED = "suppressed"
    UNDELIVERABLE = "undeliverable"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


TERMINAL_LEDGER_STATES: frozenset[LedgerState] = frozenset(
    {
        LedgerState.ACKED,
        LedgerState.SUPPRESSED,
        LedgerState.UNDELIVERABLE,
        LedgerState.SUPERSEDED,
        LedgerState.EXPIRED,
    }
)
"""States after which ``blocked_reason``/``blocked_since`` are stale by
construction and MUST be cleared (D12, §3)."""


# ─── delivery_emission.carrier (D2) ────────────────────────────────────────────
class Carrier(str, Enum):
    """The four message carriers (blueprint §2, D2).

    ``condition_inbox`` is deliberately NOT a member: a suppressed condition has
    no message id, so condition events live in ``condition_ledger`` (r1/B3).
    """

    NATIVE = "native"
    DOORBELL = "doorbell"
    HOOK = "hook"
    REPLAY = "replay"


class EmissionOutcome(str, Enum):
    """The RESULT recorded on a claim row (D3/S2).

    ``pending`` — claim held, not yet emitted. ``succeeded`` — the carrier spoke.
    ``failed`` — the emit failed; the carrier keeps its claim and may retry under
    the SAME row (``attempts`` increments), so a dropped socket does not burn the
    carrier. ``carrier_unavailable`` — the carrier was in the stored applicable
    set at routing time but is no longer applicable at emit time (r3/S2); a
    TERMINAL, non-retryable outcome that counts toward exhaustion and names why.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CARRIER_UNAVAILABLE = "carrier_unavailable"


class SuppressedReason(str, Enum):
    """Why a surface DECLINED to speak (delivery_ledger.suppressed_reason)."""

    BUSY_CLASS = "busy_class"
    ALREADY_ACKED = "already_acked"
    DEDUP_EPOCH = "dedup_epoch"


class BlockedReason(str, Enum):
    """Why an id has not been emitted YET — a wait, not a decline (D12)."""

    AWAITING_IDLE = "awaiting_idle"


class UndeliverableReason(str, Enum):
    """Why an id reached ``undeliverable`` (D2/D8/S1)."""

    CARRIERS_EXHAUSTED = "carriers_exhausted"
    RECEIVER_GONE = "receiver_gone"


class AckActor(str, Enum):
    """Who consumed the message (D4). The watermark alone cannot tell these
    apart; the ledger records the actor."""

    EXPLICIT = "explicit"  # an explicit ``ack_messages`` call
    HOOK = "hook"  # the ``supervisor-inbox-drain.sh`` silent ack


# ─── D2/S2: carrier exhaustion ─────────────────────────────────────────────────
# Outcomes that count as TERMINAL for a carrier when deciding exhaustion.
# ``failed`` counts only once retries are exhausted; ``carrier_unavailable`` is
# always terminal. A carrier still ``pending`` or with retries remaining is
# OUTSTANDING and blocks exhaustion.
_TERMINAL_EMISSION_OUTCOMES: frozenset[EmissionOutcome] = frozenset(
    {EmissionOutcome.FAILED, EmissionOutcome.CARRIER_UNAVAILABLE}
)


class EmissionView(NamedTuple):
    """A read-only projection of one ``delivery_emission`` row for the pure
    exhaustion rule. ``retryable`` is False once ``failed`` has run out of
    retries or the outcome is ``carrier_unavailable``."""

    carrier: Carrier
    outcome: EmissionOutcome
    retryable: bool


def is_carrier_exhausted(view: EmissionView) -> bool:
    """A single carrier has reached a terminal, non-retryable outcome (D2/S2)."""
    return view.outcome in _TERMINAL_EMISSION_OUTCOMES and not view.retryable


def carriers_exhausted(
    applicable: Iterable[Carrier],
    emissions: Sequence[EmissionView],
    *,
    acked: bool,
) -> bool:
    """D2/S2: has every APPLICABLE carrier reached a terminal outcome while the
    id is still unacked?

    Evaluated against the STORED applicable set (``applicable_carriers``), so a
    carrier that never armed (absent from the set) neither fires ``undeliverable``
    early nor blocks it forever. A carrier in the set with NO emission row yet is
    outstanding (still owed an attempt). ``carrier_unavailable`` counts as a
    terminal outcome — a WS that disarms after routing cannot leave the predicate
    permanently unsatisfiable (r3/S2, AC23).
    """
    if acked:
        return False
    applicable_set = set(applicable)
    if not applicable_set:
        # No applicable carrier at all: the id can never be emitted, so an
        # unacked message is trivially undeliverable (receiver-gone / no-surface).
        return True
    by_carrier: dict[Carrier, EmissionView] = {}
    for view in emissions:
        if view.carrier in applicable_set:
            by_carrier[view.carrier] = view
    for carrier in applicable_set:
        latest = by_carrier.get(carrier)
        if latest is None:
            return False  # carrier owed an attempt — still outstanding
        if not is_carrier_exhausted(latest):
            return False
    return True


# ─── D5: kind → surfaces map (blueprint §4) ────────────────────────────────────
class Surfaces(NamedTuple):
    """Which of the three surfaces a condition kind reaches (blueprint §4)."""

    fleet: bool
    bus: bool
    inbox: bool


# The routing policy as DATA, not code branches (#494's explicit ask, D5). A kind
# with NO entry defaults to ``inbox=False`` and is reported as unmapped (AC6) —
# NEVER a silent push. Keys are ``ConditionKind`` *values* (the UPPERCASE strings
# "CAPPED", "BUSY", … from providers.condition) so this table has no import
# dependency on that module while still matching its vocabulary exactly.
KIND_SURFACES: dict[str, Surfaces] = {
    "BUSY": Surfaces(fleet=True, bus=True, inbox=False),
    "CAPPED": Surfaces(fleet=True, bus=True, inbox=True),
    "AUTH_EXPIRED": Surfaces(fleet=True, bus=True, inbox=True),
    "DIALOG_BLOCKED": Surfaces(fleet=True, bus=True, inbox=True),
    "PROC_EXITED": Surfaces(fleet=True, bus=True, inbox=True),
    "HOST_UNREACHABLE": Surfaces(fleet=True, bus=True, inbox=True),
    "NET_INTERRUPTED": Surfaces(fleet=True, bus=True, inbox=False),
    "TRANSIENT_OVERLOAD": Surfaces(fleet=True, bus=True, inbox=False),
    "CONTEXT_EXHAUSTED": Surfaces(fleet=True, bus=True, inbox=True),
}

# Default for an unmapped kind (AC6): fleet + bus carry it, inbox does NOT.
_DEFAULT_SURFACES = Surfaces(fleet=True, bus=True, inbox=False)


def surfaces_for_kind(kind: str) -> Surfaces:
    """Resolve the surfaces for a condition kind; unmapped ⇒ ``inbox=False``."""
    return KIND_SURFACES.get(kind, _DEFAULT_SURFACES)


def is_kind_mapped(kind: str) -> bool:
    """AC6: whether a kind has an explicit map entry (an unmapped kind is
    reported rather than silently defaulting to a push)."""
    return kind in KIND_SURFACES


def busy_class_declines_inbox(kind: str) -> bool:
    """D5: does this kind's map decline the INBOX leg while still firing fleet +
    bus? Such a delivery writes ``decision='delivered'`` with
    ``suppressed_reason='busy_class'`` — the memory is still set (:574), so the
    row stays INSIDE the de-dup comparison (r3/B1)."""
    surf = surfaces_for_kind(kind)
    return surf.fleet and surf.bus and not surf.inbox


# ─── D7: condition-plane decisions and the durable de-dup rule (AC21) ───────────
class ConditionDecision(str, Enum):
    """One value per live ``deliver()`` exit path (blueprint §2, r3/B1).

    * ``delivered`` — the delivered exit (condition.py sets ``_last``).
    * ``deduped`` — the de-dup branch (``prev == key``; touches no memory).
    * ``gated`` — the confidence gate (``should_deliver`` False; touches nothing).
    * ``cleared`` — ``deliver(cond=None)`` (pops ``_last``); tuple is NULL.

    The de-dup rule reads the latest row whose decision is a MEMORY-UPDATING one
    (``delivered`` or ``cleared``) and suppresses only when that row is a
    ``delivered`` with an equal tuple. ``gated`` and ``deduped`` rows are written
    for audit and SKIPPED by the comparison (r3/B1, AC21(d)/AC24).
    """

    DELIVERED = "delivered"
    DEDUPED = "deduped"
    GATED = "gated"
    CLEARED = "cleared"


# The decisions that MOVE the durable memory — exactly {delivered, cleared},
# mirroring the two ``deliver()`` exits that touch ``_last`` (condition.py). The
# de-dup comparison considers only rows carrying one of these decisions.
MEMORY_UPDATING_DECISIONS: frozenset[ConditionDecision] = frozenset(
    {ConditionDecision.DELIVERED, ConditionDecision.CLEARED}
)


class ConditionTuple(NamedTuple):
    """F611's de-dup tuple, carried as DATA not as a key (D7). ``subtype`` is a
    string; a ``cleared`` row has ``kind``/``subtype`` NULL and is represented by
    ``None`` where a tuple is expected (the rule never compares a cleared row's
    tuple)."""

    kind: str
    subtype: str
    epoch: int


class ConditionLogRow(NamedTuple):
    """A read-only projection of one ``condition_ledger`` row for the pure rule.

    ``id`` is the autoincrement PK (append order). ``tuple_`` is ``None`` for a
    ``cleared`` row (NULL kind/subtype). Rows are compared newest-first by ``id``.
    """

    id: int
    decision: ConditionDecision
    tuple_: Optional[ConditionTuple]


def latest_memory_row(rows: Sequence[ConditionLogRow]) -> Optional[ConditionLogRow]:
    """Return the latest (highest ``id``) row whose decision is memory-updating
    (``delivered`` or ``cleared``), skipping ``gated`` and ``deduped`` audit rows
    (r3/B1). ``None`` if the terminal has no such row yet."""
    best: Optional[ConditionLogRow] = None
    for row in rows:
        if row.decision not in MEMORY_UPDATING_DECISIONS:
            continue
        if best is None or row.id > best.id:
            best = row
    return best


def should_suppress_condition(
    incoming: ConditionTuple,
    rows: Sequence[ConditionLogRow],
) -> bool:
    """D7 durable de-dup rule (AC21).

    Suppress the incoming condition IFF the latest memory-updating row for the
    terminal is a ``delivered`` whose tuple equals ``incoming``. This reproduces
    F611's one-deep ``prev == key`` (condition.py) durably:

    * (a) ``CAPPED → CAPPED``: latest memory row is the first delivered CAPPED →
      SUPPRESS the second. ✔
    * (b) ``CAPPED → DIALOG_BLOCKED → CAPPED``: latest is the DIALOG delivered
      row → tuple differs → DELIVER. ✔
    * (c) ``CAPPED → clear → CAPPED``: latest is the ``cleared`` row (not a
      ``delivered``) → DELIVER; the NULL tuple is never compared. ✔
    * (d) ``CAPPED → gated(LOW) → CAPPED``: the ``gated`` row is skipped, latest
      memory row is still the first delivered CAPPED → SUPPRESS the second. ✔

    The ``busy_class`` case stays inside the comparison because such a delivery is
    a ``delivered`` row (its memory is set); D5 removes only the inbox leg.
    """
    latest = latest_memory_row(rows)
    if latest is None:
        return False
    if latest.decision is not ConditionDecision.DELIVERED:
        return False  # a ``cleared`` latest row re-arms
    return latest.tuple_ == incoming
