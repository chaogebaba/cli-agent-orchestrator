"""AC-3a's agreement report: the shadow queue against the legacy inbox.

The criterion is a DIFFERENCE between arms, in the shape phase 1 used for AC10
(``app/worker_truth/agreement.py``): over one live multi-lane session each id's
terminal state in the shadow queue matches the legacy inbox outcome, and every
disagreement is classified.  A green run with the switch off cannot read as a
pass, because with the switch off there are no rows to compare and the report is
INVALID rather than perfect.

That last point is the one worth stating twice, and it is why the content floor
below exists.  An empty comparison is "no evidence".  A report that averaged a
fabricated 1.0 over zero comparisons would make the emptiest possible session —
the one where the feature never ran — look like the strongest possible result.

**The three classifications.**

``queue_early``   the shadow row reached a terminal state while the legacy row is
                  still in flight.  Expected in small numbers: the mirror settles
                  from the status it observes, and legacy's own compare-and-set
                  can lag its attempt settlement.
``legacy_early``  the legacy row ended and the shadow row is still ``ready``.
                  This is the missed-outcome case §7a anticipates — a restart
                  mid-flight, or an edge that never fired — and it is counted
                  rather than hidden, because the alternative is a mirror that
                  looks perfect by declining to notice.
``genuine``       both sides ended and they ended DIFFERENTLY.  This is the only
                  class that says the mapping or the mirror is wrong, and it is
                  the number a reader should go to first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from cli_agent_orchestrator.app.delivery.mirror import LEGACY_TERMINAL_MAP
from cli_agent_orchestrator.core.delivery import MsgState, QueueMessage, QueueMode

__all__ = [
    "AgreementReport",
    "IdComparison",
    "MIN_MESSAGES",
    "MIN_RECEIVERS",
    "MIN_TERMINAL_EACH_SIDE",
    "ReceiverAgreement",
    "build_delivery_agreement",
]

#: Content floor, in phase 1's spirit and calibrated to what a multi-lane session
#: actually produces.  Below any of these the report is INVALID: not a failure,
#: but not evidence either, and the distinction is what stops a two-minute smoke
#: run being attached to a gate as an acceptance record.
MIN_RECEIVERS = 2
MIN_MESSAGES = 20
MIN_TERMINAL_EACH_SIDE = 5


@dataclass(frozen=True)
class IdComparison:
    """One message id, both sides."""

    msg_id: str
    legacy_message_id: int | None
    receiver_id: str
    queue_state: MsgState
    legacy_status: str
    agrees: bool
    classification: str | None = None


@dataclass(frozen=True)
class ReceiverAgreement:
    """Per-receiver roll-up, which is the grain an operator reads at."""

    receiver_id: str
    messages: int = 0
    comparable: int = 0
    agreements: int = 0
    comparisons: list[IdComparison] = field(default_factory=list)

    @property
    def agreement_rate(self) -> float | None:
        """``None``, not 1.0, when nothing was comparable.

        A receiver nobody could compare has no rate, and averaging a fabricated
        1.0 into the fleet summary is exactly how an empty session would come to
        look perfect.
        """
        if self.comparable == 0:
            return None
        return self.agreements / self.comparable


@dataclass(frozen=True)
class AgreementReport:
    """The fleet summary AC-3a requires attached."""

    valid: bool
    invalid_reasons: list[str]
    receivers: list[ReceiverAgreement]
    total_messages: int
    queue_terminal: int
    legacy_terminal: int
    generated_at: datetime | None = None

    @property
    def comparisons(self) -> list[IdComparison]:
        return [row for receiver in self.receivers for row in receiver.comparisons]

    @property
    def total_comparable(self) -> int:
        return sum(receiver.comparable for receiver in self.receivers)

    @property
    def total_agreements(self) -> int:
        return sum(receiver.agreements for receiver in self.receivers)

    @property
    def agreement_rate(self) -> float | None:
        if self.total_comparable == 0:
            return None
        return self.total_agreements / self.total_comparable

    def classification_counts(self) -> dict[str, int]:
        counts = {"queue_early": 0, "legacy_early": 0, "genuine": 0}
        for row in self.comparisons:
            if row.classification is not None:
                counts[row.classification] = counts.get(row.classification, 0) + 1
        return counts


def build_delivery_agreement(
    rows: Iterable[QueueMessage],
    legacy_status: Mapping[int, str],
    *,
    generated_at: datetime | None = None,
) -> AgreementReport:
    """Compare every shadow row against the legacy status of the row it mirrors.

    ``legacy_status`` is handed in rather than read, for the layering reason that
    runs through this whole package: ``app`` may not import legacy, so it cannot
    look at the inbox table.  That is also what makes the comparison honest —
    this function has no way to make its own side agree.

    Only ``mode='shadow'`` rows are compared.  A live row has no legacy
    counterpart by construction (post-flip the legacy inbox is read-only), so
    including one would count a missing legacy status as a disagreement.
    """
    per_receiver: dict[str, list[IdComparison]] = {}
    queue_terminal = 0
    legacy_terminal = 0
    total = 0

    for row in rows:
        if row.mode is not QueueMode.SHADOW:
            continue
        total += 1
        status = (
            legacy_status.get(row.legacy_message_id) if row.legacy_message_id is not None else None
        )
        status = status if status is not None else ""
        expected = LEGACY_TERMINAL_MAP.get(status)
        legacy_is_terminal = expected is not None
        if row.terminal:
            queue_terminal += 1
        if legacy_is_terminal:
            legacy_terminal += 1

        agrees = False
        classification: str | None = None
        if row.terminal and legacy_is_terminal:
            assert expected is not None  # narrowed by legacy_is_terminal
            agrees = row.state is expected[0]
            classification = None if agrees else "genuine"
        elif row.terminal:
            classification = "queue_early"
        elif legacy_is_terminal:
            classification = "legacy_early"
        else:
            # Neither side has ended.  Not a comparison at all — counting an
            # in-flight pair as an agreement would let a session of messages
            # nothing had finished delivering report a perfect rate.
            classification = None

        per_receiver.setdefault(row.receiver_id, []).append(
            IdComparison(
                msg_id=row.msg_id,
                legacy_message_id=row.legacy_message_id,
                receiver_id=row.receiver_id,
                queue_state=row.state,
                legacy_status=status,
                agrees=agrees,
                classification=classification,
            )
        )

    receivers = [
        ReceiverAgreement(
            receiver_id=receiver_id,
            messages=len(comparisons),
            comparable=sum(
                1 for row in comparisons if row.agrees or row.classification is not None
            ),
            agreements=sum(1 for row in comparisons if row.agrees),
            comparisons=comparisons,
        )
        for receiver_id, comparisons in sorted(per_receiver.items())
    ]

    reasons = _floor_violations(
        receivers=len(receivers),
        messages=total,
        queue_terminal=queue_terminal,
        legacy_terminal=legacy_terminal,
    )
    return AgreementReport(
        valid=not reasons,
        invalid_reasons=reasons,
        receivers=receivers,
        total_messages=total,
        queue_terminal=queue_terminal,
        legacy_terminal=legacy_terminal,
        generated_at=generated_at,
    )


def _floor_violations(
    *, receivers: int, messages: int, queue_terminal: int, legacy_terminal: int
) -> list[str]:
    """Why this report is not evidence, if it is not.

    Both terminal counts are floored, not just one.  A run where the legacy side
    ended plenty of rows and the queue ended none would otherwise pass the floor
    and then report a perfect legacy-early rate, which reads as a finding about
    timing when it is actually the mirror writer never having run.
    """
    reasons: list[str] = []
    if receivers < MIN_RECEIVERS:
        reasons.append(f"{receivers} receiver(s), need >= {MIN_RECEIVERS} (multi-lane session)")
    if messages < MIN_MESSAGES:
        reasons.append(f"{messages} shadow row(s), need >= {MIN_MESSAGES}")
    if queue_terminal < MIN_TERMINAL_EACH_SIDE:
        reasons.append(
            f"{queue_terminal} terminal queue row(s), need >= {MIN_TERMINAL_EACH_SIDE} "
            "(a queue side that never ends anything is not a comparison)"
        )
    if legacy_terminal < MIN_TERMINAL_EACH_SIDE:
        reasons.append(
            f"{legacy_terminal} terminal legacy row(s), need >= {MIN_TERMINAL_EACH_SIDE}"
        )
    return reasons
