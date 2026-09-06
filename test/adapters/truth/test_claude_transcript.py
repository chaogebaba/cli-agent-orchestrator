"""The claude_code transcript tailer (WP-ARCH phase 2, D3 / §5).

Every case here is one of the census findings or one of the five tailer rules,
because those are the two places this adapter can be wrong in a way that a
plausible implementation would pass anyway.  The census is what makes the
difference concrete: reading ``type == "user"`` as a turn start looks obviously
right and would misclassify 1,077 tool results out of 1,097 ``user`` records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.adapters.truth import claude_transcript, wiring
from cli_agent_orchestrator.core.events import EventKind

from .conftest import FakeClock, FakeEventStore, FakeStateStore

TERMINAL = "t-claude"
SESSION = "sess-1"


def _write(path: Path, *records: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _user_text(uuid: str, prompt_id: str | None = None, session: str = SESSION) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "sessionId": session,
        "message": {"content": [{"type": "text", "text": "do the thing"}]},
    }
    if prompt_id is not None:
        record["promptId"] = prompt_id
    return record


def _tool_result(uuid: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": SESSION,
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    }


def _tool_use(uuid: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": SESSION,
        "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
    }


def _turn_duration(uuid: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "turn_duration",
        "parentUuid": uuid,
        "sessionId": SESSION,
        "durationMs": 4200,
        "messageCount": 6,
    }


def _source() -> claude_transcript.ClaudeTranscriptSource:
    """The live tailer for :data:`TERMINAL`, asserted present.

    ``source_for`` returns ``None`` for a terminal with no tailer, and every use
    below has just attached one. Narrowing here rather than at each call site
    turns "attach silently did nothing" into a named failure instead of an
    ``AttributeError`` twenty lines later, and satisfies strict typing as a
    by-product rather than by an ignore comment.
    """
    source = claude_transcript.source_for(TERMINAL)
    assert source is not None, "no tailer is attached for the terminal"
    return source


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return tmp_path / f"{SESSION}.jsonl"


# -- the allow-set (census finding 1) ---------------------------------------


def test_unlisted_sidecar_types_are_ignored_and_cannot_break_the_tail(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """Census finding 1: ``type`` is not a closed set of conversation records.

    The large census file interleaves ``attachment`` 4,475 times, ``queue-
    operation`` 2,301, ``ai-title`` 378 — and a third transcript sampled
    independently carried further kinds the census never saw.  So the rule is an
    ALLOW-SET, not a deny-list of the ones we happen to know: an unlisted type
    must be inert, and the turn after it must still be read.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(
        transcript,
        {"type": "attachment", "id": 1},
        {"type": "queue-operation", "op": "push"},
        {"type": "file-history-delta"},
        {"type": "a-type-invented-after-this-test-was-written"},
        _user_text("u1"),
    )
    _source().poll_once()

    assert ingest_on.kinds(TERMINAL) == [
        "session.started",
        "turn.started",
        "submission.confirmed",
    ]


def test_the_allow_set_is_exactly_the_three_mapped_types() -> None:
    """The SET, asserted by equality, not merely its effect.

    The behavioural case above is not enough on its own, and a mutation run is
    what showed it: widening the allow-set to the census enumeration —
    ``attachment``, ``queue-operation``, ``ai-title`` — changes no output today,
    because those types fall through the classifier to no kind anyway, so the
    case above passes on the mutant.  What the widening actually breaks is the
    RULE: the allow-set is meant to be exactly the types that carry a mapped
    kind, so an unlisted sidecar can never reach the uuid chain.  A set that
    drifts toward "everything the census happened to see" is the enumeration the
    census explicitly warned against turning into a filter, and the next
    unlisted type is then one classifier branch away from mattering.
    """
    assert claude_transcript.TRANSCRIPT_ALLOW_SET == {"user", "assistant", "system"}


def test_the_allow_set_filter_runs_before_the_uuid_chain(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """A sidecar with NO uuid must not reach anything that reads one.

    Sidecar records carry no ``uuid``/``parentUuid`` chain at all, which is why
    the census states the filter order as part of the rule.  A record that got as
    far as the classifier would still be ignored, but it would have been ignored
    by luck rather than by design, and the next sidecar shape would decide it.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, {"type": "permission-mode"}, {"type": "mode"}, {"type": "last-prompt"})
    assert _source().poll_once() == 0
    assert ingest_on.rows == []


# -- turn starts, tool results and the promptId dedupe (census finding 3) ----


def test_a_tool_result_is_a_user_record_and_is_not_a_turn_start(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """Census finding 3, the one a plausible implementation gets wrong.

    ``type == "user"`` held 1,077 tool results against 20 text prompts in the
    census file — a factor of fifty.  Reading the type alone would report fifty
    turns for every real one and put the projection in ``busy`` permanently.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _tool_result("r1"), _tool_result("r2"))
    _source().poll_once()

    assert ingest_on.kinds(TERMINAL) == ["session.started", "tool.result", "tool.result"]


def test_an_assistant_tool_use_is_a_tool_call(ingest_on: FakeEventStore, transcript: Path) -> None:
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _tool_use("a1"))
    _source().poll_once()

    assert ingest_on.kinds(TERMINAL) == ["session.started", "tool.called"]


def test_a_start_of_turn_record_is_both_a_turn_and_a_submission(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """One record asserts two kinds, and splitting them would mean inventing one.

    ``submission.confirmed`` is what I4 rests on: a submission is confirmed by a
    record the WORKER wrote, never by pane content.  The record that proves the
    prompt reached the worker is the same record that starts the turn.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"))
    _source().poll_once()

    assert ingest_on.kinds(TERMINAL) == [
        "session.started",
        "turn.started",
        "submission.confirmed",
    ]
    ref = ingest_on.of_kind(EventKind.SUBMISSION_CONFIRMED, TERMINAL)[0].source_ref
    assert ref == claude_transcript.latest_submission_source_ref(TERMINAL, catch_up=False)


def test_a_queued_prompt_appearing_twice_under_one_prompt_id_is_one_turn(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """Census finding 3's second half: queued prompts appear twice.

    Both appearances are the same submission arriving again, not a second turn.
    A tailer that counted both would double every queued dispatch in the log the
    agreement report reads.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1", prompt_id="p-1"), _user_text("u2", prompt_id="p-1"))
    _source().poll_once()

    assert ingest_on.kinds(TERMINAL) == [
        "session.started",
        "turn.started",
        "submission.confirmed",
    ]


def test_two_distinct_prompt_ids_are_two_turns(ingest_on: FakeEventStore, transcript: Path) -> None:
    """The dedupe must not swallow a genuine second turn."""
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1", prompt_id="p-1"), _user_text("u2", prompt_id="p-2"))
    _source().poll_once()

    assert ingest_on.of_kind(EventKind.TURN_STARTED, TERMINAL).__len__() == 2


# -- turn.ended (census finding 2) ------------------------------------------


def test_turn_ended_comes_from_the_explicit_marker_not_from_a_hook(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """Census finding 2, which refuted the r1 draft.

    r1 gave ``turn.ended`` to the ``Stop`` hook on the belief that no explicit
    end-of-turn marker existed.  It does, written at the same Stop point, so the
    hook would buy no latency over a line already being written — and a hook can
    be silent when it dies while a tailer's probe cannot.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _turn_duration("u1"))
    _source().poll_once()

    assert ingest_on.kinds(TERMINAL) == ["session.started", "turn.ended"]


@pytest.mark.parametrize("subtype", ["stop_hook_summary", "compact_boundary", "away_summary"])
def test_the_other_system_subtypes_assert_no_turn(
    ingest_on: FakeEventStore, transcript: Path, subtype: str
) -> None:
    """``turn_duration``'s siblings are boundaries of other things.

    They appear far more often than it does (317 ``stop_hook_summary`` against 63
    ``turn_duration`` in the census), so a tailer that matched on ``type ==
    "system"`` would end five turns for every one that really ended.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, {"type": "system", "subtype": subtype, "uuid": "s1"})
    _source().poll_once()

    assert ingest_on.rows == []


# -- the five tailer rules ---------------------------------------------------


def test_a_first_attach_to_an_existing_file_starts_at_eof(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """The single worst thing a truth log can do is replay history as if it were now.

    A resumed session's transcript already holds the whole prior conversation.
    """
    _write(transcript, _user_text("old1"), _user_text("old2"), _turn_duration("old2"))
    claude_transcript.attach(TERMINAL, transcript, SESSION)

    assert ingest_on.rows == []


def test_a_file_that_does_not_exist_yet_starts_at_its_head(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """The EOF rule is about the ATTACH INSTANT, not the first successful stat.

    A fresh session is attached before the file exists, and by the time it does it
    already holds the session header and the first turn.  Deferring the seed to
    the first stat looks equivalent and would silently skip the beginning of every
    new session.
    """
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"))
    _source().poll_once()

    assert "turn.started" in ingest_on.kinds(TERMINAL)


def test_a_poll_that_finds_nothing_still_bumps_the_source_probe(
    ingest_on: FakeEventStore, state_store: FakeStateStore, transcript: Path
) -> None:
    """A quiet transcript is a healthy transcript.

    A tailer that only reported liveness when the file GREW would let the
    projector degrade an idle terminal after ``NO_SIGNAL_S``, which is a cutover
    turning a quiet worker into a status outage.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    before = len(state_store.source_touches)
    assert _source().poll_once() == 0
    assert len(state_store.source_touches) == before + 1


def test_a_missing_file_is_not_a_source_health_signal(
    ingest_on: FakeEventStore, state_store: FakeStateStore, transcript: Path
) -> None:
    """A file that is not there yet must not be reported as a live probe.

    Bumping the column for a file that could not be stat-ed would tell the
    projector the source is healthy while it has learned nothing — the exact
    inversion ``_source_healthy``'s "never probed is NOT healthy" rule exists to
    avoid, arriving from the producer's side.
    """
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    state_store.source_touches.clear()
    assert _source().poll_once() == 0
    assert state_store.source_touches == []


def test_rotation_is_detected_by_inode_and_size_not_by_name(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """A truncation is a different file at the same path.

    Carrying the old offset into it would skip the new file's head, which is
    exactly the content a rotation makes newest.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"))
    _source().poll_once()
    assert len(ingest_on.of_kind(EventKind.TURN_STARTED, TERMINAL)) == 1

    # The poll that OBSERVES the truncation is what detects it. Writing a
    # replacement of the same length between two polls is invisible to any
    # inode-and-size detector, so a test that skipped this poll would be asserting
    # something no tailer of this shape can do.
    transcript.write_text("")  # truncate: size below the consumed offset
    _source().poll_once()
    _write(transcript, _user_text("u2"))
    _source().poll_once()

    assert len(ingest_on.of_kind(EventKind.TURN_STARTED, TERMINAL)) == 2


def test_a_partial_line_is_never_parsed(ingest_on: FakeEventStore, transcript: Path) -> None:
    """Only bytes up to the last newline are consumed.

    A record caught mid-write is read whole on the next poll instead of being
    discarded as unparseable, which is the difference between a slow tail and a
    lossy one.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user_text("u1"))[:-8])  # no newline, truncated json
    assert _source().poll_once() == 0
    assert ingest_on.rows == []

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user_text("u1"))[-8:] + "\n")
    _source().poll_once()

    assert "turn.started" in ingest_on.kinds(TERMINAL)


def test_the_source_ref_carries_the_transcript_scheme(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """§5's shape, and AC-2a's "every event carries one of the three prefixes"."""
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"))
    _source().poll_once()

    for row in ingest_on.read(TERMINAL):
        assert row.source_ref is not None
        assert row.source_ref.startswith("transcript:")
        assert row.source_ref.endswith("#u1")


def test_a_record_without_a_uuid_falls_back_rather_than_losing_its_provenance(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """The ``turn_duration`` line carries ``parentUuid`` rather than a ``uuid``."""
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _turn_duration("parent-1"))
    _source().poll_once()

    ref = ingest_on.of_kind(EventKind.TURN_ENDED, TERMINAL)[0].source_ref
    assert ref is not None and ref.endswith("#parent-1")


# -- the resume epoch (census finding 4) ------------------------------------


def test_a_resume_epoch_replays_nothing_and_skips_nothing(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """AC-2a's resumed-session criterion, and the one that catches a cursor keyed
    by ``terminal_id`` or a tail restarted at 0.

    Census finding 4: a resume writes the SAME file under a NEW binding epoch, so
    the cursor — which belongs to the FILE — must carry on where it was while the
    source announces the epoch.  Keying the cursor by terminal would replay the
    whole conversation; restarting at EOF would skip whatever landed in between.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"))
    _source().poll_once()
    before = [row.source_ref for row in ingest_on.read(TERMINAL)]

    # The worker is killed and resumed: a new binding epoch, same file.
    claude_transcript.attach(TERMINAL, transcript, "sess-2")
    _write(transcript, _user_text("u2", session="sess-2"))
    _source().poll_once()

    refs = [row.source_ref for row in ingest_on.read(TERMINAL)]
    assert refs[: len(before)] == before, "a replayed record"
    assert any(ref is not None and ref.endswith("#u2") for ref in refs), "a skipped record"

    kinds = ingest_on.kinds(TERMINAL)
    # The first attach opened the session; the resume epoch says RESUMED, not
    # STARTED. The distinction is load-bearing in the projection: ``session.started``
    # asserts STARTING and ``session.resumed`` asserts IDLE, because a resumed
    # session did not start a process — it re-attached to one that is ready for
    # work. Announcing a resume as a start would make ``idle -> starting``, which
    # the transition table classes ANOMALOUS, fire on every ordinary resume.
    assert kinds.count("session.started") == 1
    assert kinds.count("session.resumed") == 1
    assert kinds.index("session.started") < kinds.index("session.resumed")


def test_re_attaching_the_same_epoch_is_a_no_op(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """The binding route fires on every SessionStart, so attach must be cheap and
    idempotent — and must not manufacture a resume out of a repeat."""
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    first = claude_transcript.source_for(TERMINAL)
    claude_transcript.attach(TERMINAL, transcript, SESSION)

    assert claude_transcript.source_for(TERMINAL) is first
    assert ingest_on.rows == []


def test_detach_keeps_the_cursor_so_a_re_attach_re_reads_nothing(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """B5: the cursor belongs to the file, not to the terminal."""
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"))
    _source().poll_once()
    count = len(ingest_on.rows)

    claude_transcript.detach(TERMINAL)
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _source().poll_once()

    assert len(ingest_on.rows) == count


def test_a_terminal_that_moves_to_a_different_transcript_follows_it(
    ingest_on: FakeEventStore, tmp_path: Path
) -> None:
    """B5's discriminating case: ONE terminal, TWO files.

    The case above cannot tell a path-keyed cursor from a terminal-keyed one —
    with a single terminal on a single path the two keys coincide, and a mutation
    run is what showed that.  This one separates them.  B5's own reasoning is that
    a terminal OUTLIVES its transcripts: kill, respawn, and the same terminal id
    points at a different file.  Keyed by terminal, the second attach finds an
    identical key, returns early as a repeat, and goes on tailing the FIRST
    file — so everything the worker writes after the respawn is silently lost,
    while ``last_source_probe_at`` keeps being bumped and the projector goes on
    believing the source is healthy.  That is the worst failure available to a
    tailer: not wrong data, but confident silence.
    """
    first = tmp_path / "sess-1.jsonl"
    second = tmp_path / "sess-2.jsonl"
    first.touch()
    claude_transcript.attach(TERMINAL, first, "sess-1")
    _write(first, _user_text("old"))
    _source().poll_once()

    second.touch()
    claude_transcript.attach(TERMINAL, second, "sess-2")
    _write(second, _user_text("new", session="sess-2"))
    _source().poll_once()

    refs = [row.source_ref for row in ingest_on.read(TERMINAL)]
    assert any(
        ref is not None and ref.endswith("#new") for ref in refs
    ), "the tailer did not follow the terminal to its new transcript"
    assert _source().path_key == str(second)


# -- the switch --------------------------------------------------------------


def test_with_ingestion_off_the_tailer_writes_nothing_and_attaches_nothing(
    store: FakeEventStore, clock: FakeClock, transcript: Path
) -> None:
    """AC-2a's off arm, for this producer.

    Rows in the off arm are a FAILURE rather than a curiosity: "no behaviour
    change" is a measurement here, not an assertion, and the only way for a
    producer to reach the store is through the install guard in the composition
    root.
    """
    wiring.reset_producers()
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    _write(transcript, _user_text("u1"), _turn_duration("u1"))

    assert claude_transcript.source_for(TERMINAL) is None
    assert store.rows == []


def test_a_poll_after_the_switch_goes_off_writes_nothing(
    ingest_on: FakeEventStore, transcript: Path
) -> None:
    """The guard is read per poll, not only at attach.

    ``shutdown_worker_truth`` disarms the producers first, deliberately, so a
    tailer whose task has not yet noticed must not append into a closing pool.
    """
    transcript.touch()
    claude_transcript.attach(TERMINAL, transcript, SESSION)
    source = _source()
    wiring.reset_producers()
    _write(transcript, _user_text("u1"))

    assert source.poll_once() == 0
    assert ingest_on.rows == []


def test_the_tailer_declares_itself_authoritative(transcript: Path) -> None:
    """The declaration source-level precedence reads (r9).

    Without it the projector would go on applying pane observations for a
    claude_code terminal even while this source was healthy, and phase 2 would
    have shipped a source that changed nothing.
    """
    source = claude_transcript.ClaudeTranscriptSource(TERMINAL, transcript, SESSION)
    assert source.is_authoritative is True
    assert source.name == "claude_transcript"
