"""D5 — the comparison is repointed before the feed, and the rename carries history.

D5's hazard is the one this phase can cause and no borrowed precedent names.
Dark launching's shared invariant — Scientist is "only safe for wrapping methods
that aren't changing data", Envoy's mirrored responses "are always ignored" — is
that the candidate stays causally inert with respect to the control.  D1 ends
that: the moment the projection publishes through the legacy egress, the
published status is CAUSED by the projection, and a check comparing the two
reports perfect agreement forever.  It would not break.  It would go quiet.

So two things land in 2a, before any feed exists: the check reads the pane's own
record instead of the publish, and every publish names its feeder so the
agreement report can drop the echoes.  §12 states the ordering as a build note —
"Write D5 before D1" — because that report is the evidence 2b's gate rests on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cli_agent_orchestrator.adapters.truth.legacy_egress import fed_by
from cli_agent_orchestrator.core.events import (
    PROJECTION_ORIGIN,
    Confidence,
    DecisionKind,
    EventKind,
    Producer,
    WorkerEvent,
)
from cli_agent_orchestrator.core.findings import (
    RETIRED_FINDING_CODES,
    FindingCode,
)
from cli_agent_orchestrator.core.states import WorkerState
from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S

from ..conftest import Rig

TERMINAL = "t-1"


# -- the check reads the pane's record, not the publish ----------------------


def test_the_check_ignores_the_publish_and_reads_the_classification(rig: Rig) -> None:
    """The repointing itself, shown as a difference rather than asserted.

    A ``status.legacy_published`` disagreeing for an hour raises nothing now; the
    same disagreement carried by a ``status.pane_classified`` raises.  Pointing the
    check at "the raw classification the pane path still computes" — the r2 draft —
    would not have been enough: a computation is not a record, and for a sourced
    terminal nothing was writing one.
    """
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(PANE_HEARTBEAT_S * 10)
    assert rig.checks(TERMINAL) is False, "the publish is no longer the comparison"

    rig.classified(TERMINAL, "idle")
    rig.clock.advance(PANE_HEARTBEAT_S + 1)
    assert rig.checks(TERMINAL) is True

    assert len(rig.findings.list_findings(code=FindingCode.DIAG_PANE_DISAGREE)) == 1
    assert rig.findings.list_findings(code=FindingCode.DIAG_LEGACY_DISAGREE) == []


def test_a_pane_classification_moves_no_state(rig: Rig) -> None:
    """D1c's row is EVIDENCE, not an observation of the worker.

    Giving it an implied state would put the pane path back in charge of the
    projection through a door phase 2 built for the opposite purpose, and I1 would
    be false as designed rather than as implemented.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    assert rig.state_of(TERMINAL) is WorkerState.BUSY

    rig.classified(TERMINAL, "idle")

    assert rig.state_of(TERMINAL) is WorkerState.BUSY


# -- the rename carries its history (D9b) ------------------------------------


def test_the_old_code_is_retained_and_never_raised(rig: Rig) -> None:
    """Deleting the member would orphan its rows in the table phase 1 built to BE
    the evidence base; renaming the string in place would make ``count`` on a
    repeat ambiguous across the cutover boundary."""
    assert FindingCode.DIAG_LEGACY_DISAGREE in FindingCode
    assert RETIRED_FINDING_CODES == frozenset({FindingCode.DIAG_LEGACY_DISAGREE})


def test_a_pre_cutover_finding_is_still_readable_after_the_rename(rig: Rig) -> None:
    """The continuity fixture §12 asks for: a row written under the old code, read
    after the rename, listed beside the new one."""
    rig.findings.record(
        FindingCode.DIAG_LEGACY_DISAGREE,
        terminal_id=TERMINAL,
        dedupe_key="busy|idle",
        detail="written before the cutover",
    )
    rig.findings.record(
        FindingCode.DIAG_PANE_DISAGREE,
        terminal_id=TERMINAL,
        dedupe_key="busy|idle",
        detail="written after it",
    )

    codes = {finding.code for finding in rig.findings.list_findings()}
    assert codes == {FindingCode.DIAG_LEGACY_DISAGREE, FindingCode.DIAG_PANE_DISAGREE}
    assert len(rig.findings.list_findings(code=FindingCode.DIAG_LEGACY_DISAGREE)) == 1


# -- fed_by, the field that keeps a publish from confirming itself ----------


def _row(
    seq: int,
    kind: EventKind | DecisionKind,
    payload: dict[str, object],
    *,
    decision: DecisionKind | None = None,
    producer: Producer = Producer.PANE,
    confidence: Confidence = Confidence.DERIVED,
) -> WorkerEvent:
    at = datetime(2026, 9, 5, 12, 0, seq, tzinfo=timezone.utc)
    return WorkerEvent(
        event_id=f"e{seq}",
        terminal_id=TERMINAL,
        seq=seq,
        kind=kind,
        producer=producer,
        confidence=confidence,
        observed_at=at,
        ingested_at=at,
        payload=payload,
        decision=decision,
        evidence="e0" if decision is not None else None,
    )


def test_a_publish_the_projection_fed_is_marked_as_an_echo() -> None:
    """D5's exclusion, at the function that makes it.

    This used to be asserted through the AC10 agreement report's counters; the
    report went with shadow-live mode (#738), so the rule is asserted where it
    actually lives. The rule itself is unchanged and still load-bearing: once D1
    publishes the projection through this same egress, a reader that treated the
    echo as an independent legacy observation would be comparing the projection
    with itself and reporting perfect agreement forever.

    MUTANT: return ``Producer.PANE.value`` unconditionally and the first case
    fails — every publish then looks pane-fed, which is the silent-agreement bug.
    """
    assert fed_by(PROJECTION_ORIGIN) == PROJECTION_ORIGIN
    assert fed_by("pane") == Producer.PANE.value
    assert fed_by("anything-else") == Producer.PANE.value


def test_a_row_written_before_phase_2_is_read_as_pane_fed() -> None:
    """Rows from phase 1 carry no ``fed_by`` at all, and absent must mean "pane".

    That is what was true then. Treating a missing field as the projection would
    silently reclassify the whole of phase 1's history as echoes.
    """
    legacy = _row(1, EventKind.STATUS_LEGACY_PUBLISHED, {"latched_status": "processing"})

    assert "fed_by" not in legacy.payload
    assert fed_by(legacy.payload.get("fed_by", "pane")) == Producer.PANE.value
