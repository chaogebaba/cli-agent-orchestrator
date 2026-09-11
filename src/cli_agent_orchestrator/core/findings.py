"""Typed invariant findings (WP-ARCH phase 1, AC8).

The blueprint's diagnosability decision (U9) is that a quirk must be
reconstructable from stored rows with one command.  Findings are the half of
that which does not wait to be asked: when an invariant breaks, a typed row is
written with a COUNT and a sample event, and ``cao diag findings`` lists them.
That replaces the prose watchdog pings, which could neither be counted nor
joined to the event that caused them.

Repeats increment rather than accumulate.  A projector that sees the same
impossible cell four hundred times must not write four hundred rows — it must
write one row whose ``count`` is four hundred, keeping the FIRST sample, because
the first occurrence is the one whose surrounding timeline still explains
anything.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["RETIRED_FINDING_CODES", "Finding", "FindingCode", "FindingState"]


class FindingCode(StrEnum):
    """The phase-1 check vocabulary.

    ``DIAG_BAD_TRANSITION``   — an ``Anomalous`` cell was applied (AC1/AC6 (d)).
    ``DIAG_GHOST_TRANSITION`` — a decision row cites no evidence, so nothing
                                explains why the server acted.
    ``DIAG_LEGACY_DISAGREE``  — the state projection and the legacy published
                                status disagreed for longer than one heartbeat.
                                **Accepted but no longer raised**, from WP-ARCH
                                phase 2 (D9b): see ``DIAG_PANE_DISAGREE`` below.
    ``DIAG_PANE_DISAGREE``    — the same disagreement, read from the pane's own
                                ``status.pane_classified`` record rather than
                                from the publish (D5).
    ``DIAG_PRODUCER_DISAGREE`` — two PRODUCERS disagree about one terminal: a
                                derived event that source precedence muted
                                asserted a state the projection is not in (D9b).
                                A different question from the two above, which
                                compare the projection against a RECORD; this one
                                compares two live producers at the moment the
                                projector chose between them, which is the only
                                moment both readings exist.

    Phase 2's D9b makes that a RENAME WITH HISTORY rather than a rename.  Rows
    already carry the old code and ``cao diag findings`` reads them, so the new
    code is ADDED, the old one is RETAINED in the enum as accepted-but-never-
    raised, and both print.  Deleting the old member would orphan its rows in the
    very table phase 1 built to be the evidence base; renaming the string in place
    would make ``count`` on a repeat ambiguous across the cutover boundary — the
    same reading would be two findings before and after, or one finding whose
    count spans two different checks.

    The repointing itself is D5's, and it is not cosmetic.  The old check compares
    the projection against ``status.legacy_published``; the moment phase 2's D1
    publishes the projection through that same egress, the check compares the
    projection with itself and reports agreement forever.  The new one reads the
    pane's own reading, which stays independent.
    ``DIAG_MIGRATION_FAILED`` — the phase-1 migrator raised (AC5).  Written into
                                the ``finding`` table, which is why that table is
                                created FIRST and in its own transaction: it has
                                to survive its own migration failing.

    Phase 3 names SIX codes (D5, as amended by A1).  Sub-phase 3a landed the two
    D9's boot guard raised; 3b added the two its own transitions can reach.  The
    remaining two (``DIAG-DUP-DELIVERY`` and ``DIAG-DOUBLE-WAKE``) belong to 3c
    and land with the code that can raise them.  A finding code no code path
    reaches is a promise the enum cannot keep, and ``cao diag findings`` would
    offer an operator a filter that never matches — which is exactly why a code
    that STOPS being reachable is listed in :data:`RETIRED_FINDING_CODES` rather
    than left looking live.

    ``DIAG_QUEUE_ORPHAN_GUARD``   — the boot guard demoted the requested switch
                                    position to ``drain`` because live
                                    non-terminal rows were outstanding (D9).
                                    This is also how an operator learned that an
                                    UNSET variable had started the delivery
                                    machinery: the guard overrode the default,
                                    and the finding was the notice.
                                    RETIRED from WP-ARCH 3c — accepted, never
                                    raised; see below.
    ``DIAG_BARRIER_OPEN_AT_FLIP`` — a boot requested ``on`` while a callback
                                    barrier was still OPEN, so the flip was held
                                    back — at ``drain`` over an occupied queue,
                                    else at ``off`` — rather than splitting that
                                    barrier's members across two tables (D9).
                                    RETIRED from WP-ARCH 3c — accepted, never
                                    raised; see below.

    **What an OPEN callback barrier means under always-on** (3c slice 4 ruling).
    It does not gate delivery, and it must never gain the power to again. The
    barrier is a per-MESSAGE hold: its members sit at ``status='held'`` and
    become ``pending`` when the barrier completes, at which point the tick picks
    them up like any other row. That is a property of those rows and of nothing
    else. The deleted guard let an OPEN barrier change the whole subsystem's
    MODE at boot — demoting a requested ``on`` to ``drain`` or ``off`` — which
    was defensible only while two carriers existed and a barrier's members could
    be split across two tables. With one carrier there is nothing to split, and
    a fleet-wide mode change triggered by one held message is disproportionate
    to the condition by construction. If a barrier state is ever worth acting
    on again, the action is a FINDING naming it, never a mode change.

    Those two are the boot guard's, and the boot guard is gone.  It resolved
    ``CAO_DELIVERY_QUEUE``'s three positions against the queue's occupancy, which
    was a decision worth making while the queue and the legacy inbox were both
    carriers: ``off`` was the pre-flip default and ``drain`` was the way back out
    of ``on`` without stranding the rows enqueued while it was on.  3c deletes
    the legacy carriers, so there is nothing to roll back to, one position is
    left, and a resolution over one value raises nothing.  They are RETIRED here
    rather than deleted, by this module's own rule: rows written before the
    cutover stay readable after it.
    ``DIAG_DELIVERY_TIME_BOUND``  — a row died on a TIME bound rather than an
                                    attempt bound: ``dead_by`` passed, the
                                    dialog ceiling elapsed, or the caller's own
                                    expiry came first (D12).  It exists because
                                    ``cao diag <msg_id>`` is pull-based, and a
                                    loss discoverable only by asking is the
                                    failure class this phase removes.
    ``DIAG_SEAT_WAKE_UNREACHABLE`` — the supervisor seat's native carrier could
                                    not be reached, or reached the wrong
                                    injector, or went three leases unconfirmed
                                    (§A1.4).  Raised ONCE PER OPEN EPOCH rather
                                    than once per row: ``k`` messages behind one
                                    dead socket are one condition, not ``k``.
                                    Its four reasons are
                                    :class:`~cli_agent_orchestrator.core.delivery.WakeOutcome`'s
                                    non-emitting members plus ``unverified_streak``,
                                    each classified to a bound by A1.4's table.
    """

    DIAG_BAD_TRANSITION = "DIAG-BAD-TRANSITION"
    DIAG_GHOST_TRANSITION = "DIAG-GHOST-TRANSITION"
    DIAG_LEGACY_DISAGREE = "DIAG-LEGACY-DISAGREE"
    DIAG_MIGRATION_FAILED = "DIAG-MIGRATION-FAILED"
    DIAG_QUEUE_ORPHAN_GUARD = "DIAG-QUEUE-ORPHAN-GUARD"
    DIAG_BARRIER_OPEN_AT_FLIP = "DIAG-BARRIER-OPEN-AT-FLIP"
    DIAG_PANE_DISAGREE = "DIAG-PANE-DISAGREE"
    DIAG_PRODUCER_DISAGREE = "DIAG-PRODUCER-DISAGREE"
    DIAG_STATUS_GUARD = "DIAG-STATUS-GUARD"
    DIAG_DELIVERY_TIME_BOUND = "DIAG-DELIVERY-TIME-BOUND"
    DIAG_SEAT_WAKE_UNREACHABLE = "DIAG-SEAT-WAKE-UNREACHABLE"
    #: The migrator deleted delivery rows written in the retired shadow mode
    #: (#738 / F883).  Raised once per upgrade, fleet-wide; ``detail`` carries
    #: the per-table counts.  The rows were observational copies of messages
    #: the legacy inbox owned, so deleting them loses no delivery.
    DIAG_SHADOW_ROWS_RETIRED = "DIAG-SHADOW-ROWS-RETIRED"
    #: The tick adopted a PENDING legacy ``inbox`` row that had no
    #: ``delivery_msg`` counterpart, enqueuing it and retiring the legacy row in
    #: one transaction (WP-ARCH 3c).  Counted per receiver because the number is
    #: the question: adoption is the fallback path, so a count that climbs says
    #: the write-through keeps losing its ``BEGIN IMMEDIATE`` race, which is a
    #: defect to fix rather than a state to live in.  A row reaching here was
    #: previously carried by the legacy doorbell and the seat-wake reconcile,
    #: both deleted in this slice; without adoption it has no carrier at all.
    DIAG_LEGACY_ROW_ADOPTED = "DIAG-LEGACY-ROW-ADOPTED"
    #: A CERTIFIED terminal's source went silent for ``NO_SIGNAL_S`` and the
    #: sweep degraded it to ``no_signal`` (WP-HERDR §6(ii) / WP-ARCH 2b).  It
    #: exists because §6(ii) makes that state STICKY: the terminal stays
    #: projected, publishes ``unknown``, and the pane is never handed the
    #: lifecycle back — which is the right trade (see ``publisher``'s module
    #: docstring) but is indistinguishable, from outside, from a worker that is
    #: simply quiet.  Delivery to it withholds for as long as it lasts, so hours
    #: of a dead certified stream would otherwise be a status outage with no
    #: name.  Deduped PER TERMINAL, because one stale source is one problem however
    #: many sweeps re-confirm it.  ``count`` is the number of SWEEPS that
    #: observed the stale source, which is not the same as how long it has been
    #: stale: a missed tick, a slowed one, or a server restart all under-report
    #: it.  The DURATION is ``last_seen_at - first_seen_at``, refreshed by the
    #: same writes, and that is what ``cao diag findings`` prints in its ``for``
    #: column.
    DIAG_CERTIFIED_SOURCE_STALE = "DIAG-CERTIFIED-SOURCE-STALE"
    #: herdr answered ``pane get`` with ``agent_status: unknown`` for a pane CAO
    #: owns, so the herdr backend has NO native status for that seat and the
    #: caller silently falls back to pane scraping (F926 / #778).  Counted per
    #: window because the number and the identity are both the question: which
    #: seats have no native truth, and how often.  The cause measured on herdr
    #: 0.9.0 is a gap in herdr's own bundled agent-detection manifests — ``pi``
    #: ships ONE rule (``working``) and ``cline`` only ``working``/``blocked``,
    #: so a pane of either at its prompt matches no rule and is reported
    #: ``unknown``, while ``codex`` (which ships ``idle`` rules) resolves.  That
    #: is not CAO's to fix, which is exactly why it must be counted rather than
    #: swallowed: the fallback is silent, and a silent fallback on the cheap
    #: lanes is indistinguishable from working native status.
    DIAG_HERDR_STATUS_UNKNOWN = "DIAG-HERDR-STATUS-UNKNOWN"


#: Codes that remain readable but which no code path raises any more (D9b).
#: ``cao diag findings`` prints them, retention keeps their samples, and a check
#: that starts raising one again is a defect a test can name.  Retiring a code
#: this way rather than by deletion is what keeps a finding written before a
#: cutover readable after it.
#:
#: The two delivery codes joined WP-ARCH 3c.  Both were D9's boot guard's, and
#: the guard resolved a switch between the queue and the legacy inbox; 3c deletes
#: the legacy carriers, so the switch has one position and the guard has nothing
#: to resolve.  A server that ran the guard wrote these rows, and an operator
#: reading ``cao diag findings`` on an upgraded server must still be able to see
#: what an earlier boot decided — which is the whole reason this set exists
#: rather than a delete.
RETIRED_FINDING_CODES: frozenset[FindingCode] = frozenset(
    {
        FindingCode.DIAG_LEGACY_DISAGREE,
        FindingCode.DIAG_QUEUE_ORPHAN_GUARD,
        FindingCode.DIAG_BARRIER_OPEN_AT_FLIP,
    }
)


class FindingState(StrEnum):
    """Whether a finding is still standing.

    Retention reads this: events named by an OPEN finding's ``sample_event_id``
    are never pruned, so the evidence for an unresolved problem outlives the
    30-day horizon.  A resolved finding releases its sample.
    """

    OPEN = "open"
    RESOLVED = "resolved"


class Finding(BaseModel):
    """One deduplicated invariant breach.

    ``dedupe_key`` is what distinguishes two breaches of the SAME code on the
    same terminal — for ``DIAG-BAD-TRANSITION`` it is the cell (``capped->busy``),
    for ``DIAG-LEGACY-DISAGREE`` the disagreeing pair.  It is caller-supplied
    because only the check knows what "the same problem again" means.
    ``terminal_id`` is the empty string, never ``NULL``, for fleet-wide findings:
    SQLite treats NULLs as distinct in a UNIQUE index, which would silently
    defeat the deduplication this whole model exists for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(min_length=1)
    code: FindingCode
    terminal_id: str = ""
    dedupe_key: str = ""
    detail: str = ""
    sample_event_id: str | None = None
    count: int = Field(default=1, ge=1)
    first_seen_at: datetime
    last_seen_at: datetime
    state: FindingState = FindingState.OPEN

    @field_validator("first_seen_at", "last_seen_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("finding timestamps must be timezone-aware (UTC)")
        return value
