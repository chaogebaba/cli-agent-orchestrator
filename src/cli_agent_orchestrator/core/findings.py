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

__all__ = ["Finding", "FindingCode", "FindingState"]


class FindingCode(StrEnum):
    """The phase-1 check vocabulary.

    ``DIAG_BAD_TRANSITION``   — an ``Anomalous`` cell was applied (AC1/AC6 (d)).
    ``DIAG_GHOST_TRANSITION`` — a decision row cites no evidence, so nothing
                                explains why the server acted.
    ``DIAG_LEGACY_DISAGREE``  — the shadow projection and the legacy published
                                status disagreed for longer than one heartbeat.
    ``DIAG_MIGRATION_FAILED`` — the phase-1 migrator raised (AC5).  Written into
                                the ``finding`` table, which is why that table is
                                created FIRST and in its own transaction: it has
                                to survive its own migration failing.

    Phase 3 adds the two codes D9's boot guard raises.  The phase names five in
    all; the other three (``DIAG-DUP-DELIVERY``, ``DIAG-DOUBLE-WAKE`` and
    ``DIAG-DELIVERY-TIME-BOUND``) belong to sub-phases 3b and 3c and land with
    the code that can raise them.  A finding code no code path reaches is a
    promise the enum cannot keep, and ``cao diag findings`` would offer an
    operator a filter that never matches.

    ``DIAG_QUEUE_ORPHAN_GUARD``   — the boot guard demoted the requested switch
                                    position to ``drain`` because live
                                    non-terminal rows were outstanding (D9).
                                    This is also how an operator learns that an
                                    UNSET variable started the delivery
                                    machinery: the guard overrides the default,
                                    and the finding is the notice.
    ``DIAG_BARRIER_OPEN_AT_FLIP`` — a boot requested ``on`` while a callback
                                    barrier was still OPEN, so the flip was held
                                    at ``shadow`` rather than splitting that
                                    barrier's members across two tables (D9).
    """

    DIAG_BAD_TRANSITION = "DIAG-BAD-TRANSITION"
    DIAG_GHOST_TRANSITION = "DIAG-GHOST-TRANSITION"
    DIAG_LEGACY_DISAGREE = "DIAG-LEGACY-DISAGREE"
    DIAG_MIGRATION_FAILED = "DIAG-MIGRATION-FAILED"
    DIAG_QUEUE_ORPHAN_GUARD = "DIAG-QUEUE-ORPHAN-GUARD"
    DIAG_BARRIER_OPEN_AT_FLIP = "DIAG-BARRIER-OPEN-AT-FLIP"


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
