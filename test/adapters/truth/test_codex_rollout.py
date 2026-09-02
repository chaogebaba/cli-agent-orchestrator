"""AC4 — the codex rollout tail (WP-ARCH F725 #581, lane B).

The record shapes below are the ones a 2026-09-02 census of the live
``~/.codex/sessions`` tree actually contains, not invented ones: ``session_meta``
first, then ``event_msg`` records whose ``payload.type`` is ``task_started`` /
``task_complete`` / ``token_count``, ``response_item`` records carrying
``message`` with a ``role``, and a great deal of ``reasoning`` and
``custom_tool_call`` noise that must produce nothing.

The blueprint's named run — kill, respawn, then resume a codex worker on the same
terminal, with no event replayed and none skipped — is
``test_respawn_onto_a_new_rollout_replays_nothing``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.truth import codex_rollout
from cli_agent_orchestrator.core.events import EventKind

from .conftest import FakeEventStore, FakeStateStore

SESSION_META = {
    "timestamp": "2026-09-02T20:55:54.692Z",
    "ordinal": 0,
    "type": "session_meta",
    "payload": {"id": "01a063e8", "session_id": "01a063e8", "source": "exec"},
}
TASK_STARTED = {"type": "event_msg", "payload": {"type": "task_started"}}
TASK_COMPLETE = {"type": "event_msg", "payload": {"type": "task_complete"}}
TOKEN_COUNT = {"type": "event_msg", "payload": {"type": "token_count", "total": 12}}
USER_TURN = {"type": "response_item", "payload": {"type": "message", "role": "user"}}
DEV_TURN = {"type": "response_item", "payload": {"type": "message", "role": "developer"}}
REASONING = {"type": "response_item", "payload": {"type": "reasoning"}}
TOOL_CALL = {"type": "response_item", "payload": {"type": "custom_tool_call"}}


def _write(path: Path, *records: dict[str, object], append: bool = False) -> None:
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


@pytest.fixture
def rollout(tmp_path: Path) -> Path:
    return tmp_path / "rollout-2026-09-02T20-55-54-01a063e8.jsonl"


def test_off_reads_nothing(store: FakeEventStore, rollout: Path) -> None:
    _write(rollout, SESSION_META, TASK_STARTED)
    codex_rollout.attach("t1", rollout)
    assert codex_rollout.source_for("t1") is None
    assert store.rows == []


def test_fresh_session_read_from_the_head(ingest_on: FakeEventStore, rollout: Path) -> None:
    """A file that does not exist yet has size 0, so its head IS the EOF attach."""
    codex_rollout.attach("t1", rollout)
    _write(rollout, SESSION_META, TASK_STARTED, TASK_COMPLETE)
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["session.started", "turn.started", "turn.ended"]


def test_first_attach_to_an_existing_file_starts_at_eof(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    """The single most important rule here: a resume must not replay its history."""
    _write(rollout, SESSION_META, TASK_STARTED, USER_TURN, TASK_COMPLETE)
    codex_rollout.attach("t1", rollout)
    assert ingest_on.rows == []
    _write(rollout, TASK_STARTED, append=True)
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["turn.started"]


def test_only_the_four_blueprint_kinds_are_emitted(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    codex_rollout.attach("t1", rollout)
    _write(
        rollout,
        SESSION_META,
        TOKEN_COUNT,
        REASONING,
        TOOL_CALL,
        DEV_TURN,
        TASK_STARTED,
        USER_TURN,
        TASK_COMPLETE,
    )
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == [
        "session.started",
        "turn.started",
        "submission.confirmed",
        "turn.ended",
    ]


def test_never_emits_usage_capped_or_process_exited(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    """The rollout records neither; inventing them is how a truth log starts lying."""
    codex_rollout.attach("t1", rollout)
    _write(
        rollout,
        SESSION_META,
        {"type": "event_msg", "payload": {"type": "error", "message": "usage limit"}},
        {"type": "event_msg", "payload": {"type": "shutdown_complete"}},
    )
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED) == []
    assert ingest_on.of_kind(EventKind.PROCESS_EXITED) == []


def test_events_are_authoritative_jsonl(ingest_on: FakeEventStore, rollout: Path) -> None:
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED)
    source = codex_rollout.source_for("t1")
    source.poll_once()
    row = ingest_on.rows[0]
    assert row.producer.value == "jsonl"
    assert row.confidence.value == "authoritative"
    assert source.is_authoritative is True


def test_source_ref_names_the_path_and_the_record_offset(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED, USER_TURN)
    codex_rollout.source_for("t1").poll_once()
    first, second = ingest_on.rows
    assert first.source_ref == f"rollout:{rollout}#0"
    expected = len(json.dumps(TASK_STARTED)) + 1
    assert second.source_ref == f"rollout:{rollout}#{expected}"
    assert codex_rollout.latest_submission_source_ref("t1") == second.source_ref


def test_a_partial_trailing_line_is_not_parsed_until_complete(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    codex_rollout.attach("t1", rollout)
    rollout.write_text(json.dumps(TASK_STARTED) + "\n" + json.dumps(TASK_COMPLETE)[:20])
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["turn.started"]
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(TASK_COMPLETE)[20:] + "\n")
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["turn.started", "turn.ended"]


def test_malformed_lines_are_skipped_not_fatal(ingest_on: FakeEventStore, rollout: Path) -> None:
    codex_rollout.attach("t1", rollout)
    rollout.write_text("not json\n\n" + json.dumps(TASK_STARTED) + "\n")
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["turn.started"]


def test_repeated_polls_do_not_re_emit(ingest_on: FakeEventStore, rollout: Path) -> None:
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED)
    source = codex_rollout.source_for("t1")
    for _ in range(5):
        source.poll_once()
    assert len(ingest_on.rows) == 1


def test_rotation_by_inode_restarts_the_cursor(
    ingest_on: FakeEventStore, rollout: Path, tmp_path: Path
) -> None:
    """Same NAME, new inode.  Detecting rotation by name would miss this entirely."""
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED)
    source = codex_rollout.source_for("t1")
    source.poll_once()
    replacement = tmp_path / "replacement.jsonl"
    _write(replacement, SESSION_META, TASK_COMPLETE)
    replacement.replace(rollout)
    source.poll_once()
    assert ingest_on.kinds("t1") == ["turn.started", "session.started", "turn.ended"]


def test_truncation_restarts_the_cursor(ingest_on: FakeEventStore, rollout: Path) -> None:
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED, TASK_STARTED, TASK_STARTED)
    source = codex_rollout.source_for("t1")
    source.poll_once()
    assert len(ingest_on.rows) == 3
    _write(rollout, TASK_COMPLETE)  # truncating rewrite, now much shorter
    source.poll_once()
    assert ingest_on.kinds("t1")[-1] == "turn.ended"


def test_respawn_onto_a_new_rollout_replays_nothing(
    ingest_on: FakeEventStore, tmp_path: Path
) -> None:
    """The blueprint's named run: kill, respawn, resume, same terminal id.

    The second rollout carries the whole prior conversation, as a codex resume
    does.  The cursor is keyed by PATH (B5), so the new file gets its own cursor
    and its own EOF attach; a cursor keyed by ``terminal_id`` would have carried
    the first file's offset into the second and emitted a slice of replayed
    history that happened to sit past that byte.
    """
    first = tmp_path / "rollout-first.jsonl"
    codex_rollout.attach("t1", first)
    _write(first, SESSION_META, TASK_STARTED, TASK_COMPLETE)
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["session.started", "turn.started", "turn.ended"]

    # The worker dies and is resumed; codex writes a NEW rollout containing the
    # copied history plus whatever happens next.
    second = tmp_path / "rollout-second.jsonl"
    _write(second, SESSION_META, TASK_STARTED, TASK_COMPLETE, USER_TURN)
    codex_rollout.attach("t1", second, resumed=True)
    assert ingest_on.kinds("t1") == ["session.started", "turn.started", "turn.ended"]

    _write(second, TASK_STARTED, append=True)
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1")[-1] == "turn.started"
    assert len(ingest_on.rows) == 4


def test_re_attaching_the_same_path_does_not_restart_the_tail(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    """``_resolve_rollout_file`` is polled during verification; attach is hot."""
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED)
    source = codex_rollout.source_for("t1")
    source.poll_once()
    for _ in range(10):
        codex_rollout.attach("t1", rollout)
    assert codex_rollout.source_for("t1") is source
    assert len(ingest_on.rows) == 1


def test_a_resumed_attach_labels_session_meta_as_resumed(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    codex_rollout.attach("t1", rollout, resumed=True)
    _write(rollout, SESSION_META)
    codex_rollout.source_for("t1").poll_once()
    assert ingest_on.kinds("t1") == ["session.resumed"]


def test_every_poll_that_stats_the_file_bumps_source_health(
    ingest_on: FakeEventStore, state_store: FakeStateStore, rollout: Path
) -> None:
    """A quiet rollout is a healthy rollout — silence must not degrade a terminal."""
    _write(rollout, TASK_STARTED)
    codex_rollout.attach("t1", rollout)
    before = len(state_store.source_touches)
    for _ in range(3):
        codex_rollout.source_for("t1").poll_once()
    assert len(state_store.source_touches) == before + 3
    assert ingest_on.rows == []  # nothing new in the file: liveness is a COLUMN


def test_a_missing_file_is_not_a_source_health_signal(
    ingest_on: FakeEventStore, state_store: FakeStateStore, tmp_path: Path
) -> None:
    codex_rollout.attach("t1", tmp_path / "never-created.jsonl")
    codex_rollout.source_for("t1").poll_once()
    assert state_store.source_touches == []
    assert ingest_on.rows == []


def test_attach_ignores_a_none_path_and_an_empty_terminal(
    ingest_on: FakeEventStore, rollout: Path
) -> None:
    codex_rollout.attach("t1", None)
    codex_rollout.attach("", rollout)
    assert codex_rollout.source_for("t1") is None
    assert ingest_on.rows == []


def test_detach_keeps_the_paths_cursor(ingest_on: FakeEventStore, rollout: Path) -> None:
    """B5 again: the cursor belongs to the file, so re-attaching re-reads nothing."""
    codex_rollout.attach("t1", rollout)
    _write(rollout, TASK_STARTED)
    codex_rollout.source_for("t1").poll_once()
    codex_rollout.detach("t1")
    codex_rollout.attach("t1", rollout)
    codex_rollout.source_for("t1").poll_once()
    assert len(ingest_on.rows) == 1


@pytest.mark.asyncio
async def test_the_tail_runs_as_an_asyncio_task(ingest_on: FakeEventStore, rollout: Path) -> None:
    """U7: one process, the tail is a task in it."""
    _write(rollout, TASK_STARTED)
    source = codex_rollout.CodexRolloutSource("t1", rollout)
    await source.start()
    try:
        assert source._task is not None
        assert source.name == "codex_rollout"
    finally:
        await source.stop()
    assert source._task is None
