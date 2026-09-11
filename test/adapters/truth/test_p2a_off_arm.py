"""AC-2a, the off arm — the two producers this phase adds write NOTHING (D9).

The criterion's shape matters as much as its content: *"With
``CAO_WORKER_TRUTH_STATUS`` unset over the same session, the count of events
whose ``producer`` is the claude_code tailer or the hook is NIL, and rows there
are a FAILURE rather than a curiosity."*  That is phase 3's AC-3a shape, and it
is what turns "no behaviour change" from an assertion into a measurement.

The criterion is scoped to the two producers this phase adds; the phase-1
producers keep writing in this arm, which is correct and is asserted here so a
future reader does not read the scope as an oversight.
"""

from __future__ import annotations

import json
from pathlib import Path

from cli_agent_orchestrator.adapters.truth import (
    claude_hooks,
    claude_transcript,
    legacy_egress,
    pane_classification,
    wiring,
)
from cli_agent_orchestrator.core.events import EventKind, Producer

from .conftest import FakeClock, FakeEventStore, FakeStateStore

TERMINAL = "t-claude"
SESSION = "sess-1"

#: The two producers sub-phase 2a adds.  Both write kinds phase 1 also writes, so
#: the count that discriminates is by PRODUCER, not by kind — a nil count of
#: ``prompt.awaiting`` would pass on a build where the hook producer ran and the
#: mapping was broken.
_PHASE2_PRODUCERS = {Producer.HOOK, Producer.JSONL}


def _drive_everything(transcript: Path) -> None:
    """Exercise every phase-2 producer this sub-phase adds, in one place."""
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": SESSION,
                    "message": {"content": [{"type": "text"}]},
                }
            )
            + "\n"
        )
    source = claude_transcript.source_for(TERMINAL)
    if source is not None:
        source.poll_once()
    claude_hooks.record_interaction_marker(TERMINAL, "question_open", hook_event="PreToolUse")
    pane_classification.record_pane_classification(
        TERMINAL, "idle", None, "incremental", "accepted", "COMPLETED"
    )


def test_the_off_arm_writes_no_row_from_either_new_producer(
    store: FakeEventStore, clock: FakeClock, state_store: FakeStateStore, tmp_path: Path
) -> None:
    """Rows here are a failure, not a curiosity.

    "The switch was ignored" cannot be expressed as a missing ``if`` in a
    producer: there is no code path from a hook to the database that does not go
    through the install guard in the composition root, so this failing means the
    guard itself was deleted — which is exactly what an A/B suite must see.
    """
    wiring.reset_producers()
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.touch()

    _drive_everything(transcript)

    assert store.rows == []
    assert state_store.source_touches == []


def test_the_on_arm_writes_from_both_new_producers(
    ingest_on: FakeEventStore, tmp_path: Path
) -> None:
    """The other half of the arm difference.

    A green off arm proves nothing on its own: a producer that was never wired at
    all would pass it perfectly, which is why every acceptance criterion in this
    phase is a DIFFERENCE between arms rather than a green run in one.
    """
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.touch()

    _drive_everything(transcript)

    producers = {row.producer for row in ingest_on.rows}
    assert _PHASE2_PRODUCERS <= producers
    assert EventKind.PROMPT_AWAITING.value in ingest_on.kinds(TERMINAL)
    assert EventKind.TURN_STARTED.value in ingest_on.kinds(TERMINAL)
    assert EventKind.STATUS_PANE_CLASSIFIED.value in ingest_on.kinds(TERMINAL)


def test_the_phase_one_producers_are_untouched_by_this_criterion(
    ingest_on: FakeEventStore,
) -> None:
    """The scope is deliberate and is recorded so it does not read as an oversight.

    Phase 1's egress producer keeps writing in the off arm of the STATUS switch,
    because that switch is not phase 1's — ``CAO_WORKER_TRUTH_INGEST`` is, and it
    is a different variable on purpose, so a phase-1 rollback and a phase-2
    rollback stay independent.
    """
    legacy_egress.record_legacy_publish(
        object(), TERMINAL, "idle", "incremental", "incremental", "accepted", "COMPLETED"
    )

    rows = ingest_on.of_kind(EventKind.STATUS_LEGACY_PUBLISHED, TERMINAL)
    assert len(rows) == 1
    assert rows[0].producer is Producer.PANE


def test_every_row_the_new_producers_write_carries_a_known_prefix(
    ingest_on: FakeEventStore, tmp_path: Path
) -> None:
    """AC-2a's second half, scoped to what this phase adds.

    The criterion reads "every event appended during the session carries
    one of them", which cannot hold literally at this anchor: phase 1's codex
    tailer keys its own refs ``rollout:``, a fourth prefix D4's constructor does
    not know about. Scoped to phase 2's producers it holds exactly, and the
    discrepancy is recorded in ``test/core/test_source_ref.py`` rather than
    smoothed over.
    """
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.touch()

    _drive_everything(transcript)

    for row in ingest_on.rows:
        if row.producer not in _PHASE2_PRODUCERS and row.producer is not Producer.PANE:
            continue
        assert row.source_ref is not None
        assert row.source_ref.split("#")[0].split(":")[0] + ":" in {
            "transcript:",
            "hook:",
            "pane:",
        }
