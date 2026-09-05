"""The claude_code transcript JSONL tail — phase 2's authoritative source (D3).

U6 settled *"JSONL-first, hooks as accelerator"*, and D3 makes the split
evidential rather than stylistic: **the transcript records what the worker said
and did**, so it carries the session, turn, tool and submission kinds; it does
NOT record that the worker is waiting on a human, and the hooks do (D3b).  So the
tailer owns everything below and the hook producer owns ``prompt.awaiting`` and
``prompt.answered``, and neither owns the other's kinds.

``turn.ended`` is the tailer's, which the r1 draft got wrong by giving it to the
``Stop`` hook on the belief that no explicit end-of-turn marker existed.  The
census refuted that: the transcript writes ``{"type":"system","subtype":
"turn_duration"}`` with ``durationMs`` and ``messageCount``, at the same Stop
point, so the hook would buy no latency over a line already being written.

The alternatives were rejected on liveness, not on taste.  *Hooks only:* a hook
process is armed by seat events and is silent when it dies, so it has no way to
notice its own absence — the exact failure shape phase 3 traces for #604's
client-side watcher.  A tailer's ``last_source_probe_at`` IS that notice.  This is
log-based change data capture, Debezium's argument: reading the log gives "the
complete list of all data changes in their exact order of application", and a
restarted reader "resumes from where it left off without losing or duplicating
events" — a reader that died and came back can re-derive the truth, which a hook
process cannot do.

**The census that fixes the table below** (two live transcripts, 42,846 lines /
62 MB and 192 lines, 2026-09-04) produced four findings, and each is a way the
naive implementation would be wrong:

1. ``type`` is NOT a closed set of conversation records.  The large file
   interleaves sidecar types with no uuid chain — ``attachment`` 4,475,
   ``queue-operation`` 2,301, ``permission-mode``/``mode``/``last-prompt``/
   ``bridge-session``/``atis-latch`` 401 each, ``ai-title`` 378 — and a third
   transcript sampled independently carried further kinds the census never saw
   (``agent-name``, ``file-history-snapshot``, ``file-history-delta``).  So the
   rule is a rule and not a list: **the tailer consumes an ALLOW-SET of the kinds
   it maps and ignores every other ``.type``, filtering before it touches the
   uuid chain.**  A builder who turned the census enumeration into the filter's
   allow-set would break the tail on the next unlisted sidecar type.
2. ``turn.ended`` is explicit — see above.  No pane read, no hook.
3. ``type:"user"`` is NOT a turn start.  Tool RESULTS are also ``user`` records:
   1,077 with ``message.content[0].type == "tool_result"`` against 20 with
   ``"text"``.  The start-of-turn test is therefore ``content[0].type == "text"``,
   and queued prompts appear twice under one ``promptId``, so turn starts are
   deduped on it.
4. A resume writes the SAME file.  Every sampled file carries exactly one
   non-null ``sessionId`` equal to its filename, so a resume produces a fresh
   ``SessionStart`` — and a fresh append-only row in ``transcript_bindings`` —
   not a new transcript.  The adapter re-attaches on a binding EPOCH and must not
   treat one as a new file, which is what makes AC-2a's resumed-session criterion
   ("no event is replayed and none is skipped") pass rather than a coincidence.

``isSidechain`` was ``true`` in no file across the project directory, so subagent
turns are not interleaved into a worker's own transcript in this deployment.  The
tailer does not depend on that field and treats its absence as the observed case
rather than as a guarantee.

The five rules the codex adapter already follows are followed here, and each
exists because it was paid for once (parent §4 AC4):

* the offset is keyed by the **resolved path**, never by ``terminal_id`` (B5);
* rotation is detected by **inode and size**, never by name;
* a first attach with no persisted offset starts at **EOF**, so a resumed session
  replays nothing;
* every poll that **stats** the file bumps ``last_source_probe_at``, which is
  what makes source health observable — including a poll that finds nothing new,
  because a quiet transcript is a healthy transcript;
* the adapter emits **only kinds the file actually carries**.

The path is not discovered here.  It is handed in, exactly as the codex path is:
the shipped ``SessionStart`` hook self-reports ``session_id`` and
``transcript_path``, the server validates the path resolves under the provider
home, and the adapter attaches on that binding.  New code never reaches into
legacy to resolve it (parent N2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.adapters.truth.wiring import emit, producer_runtime
from cli_agent_orchestrator.core.events import (
    Confidence,
    EventDraft,
    EventKind,
    Producer,
    SourceRefScheme,
    source_ref,
)
from cli_agent_orchestrator.core.timing import ROLLOUT_POLL_MS

__all__ = [
    "MAX_POLL_BYTES",
    "MAX_TRACKED_CURSORS",
    "TRANSCRIPT_ALLOW_SET",
    "ClaudeTranscriptSource",
    "attach",
    "detach",
    "latest_submission_source_ref",
    "reset_sources",
    "source_for",
]

logger = logging.getLogger(__name__)

#: Ceiling on how many bytes one poll consumes.  Larger than the codex adapter's
#: because a claude transcript record can embed a whole file read, and the census
#: file was 62 MB for 42,846 lines — but still a ceiling, so attaching to a
#: pathological file cannot block the event loop.
MAX_POLL_BYTES = 4 * 1024 * 1024

#: How many per-path cursors are retained.  A COUNT, not a duration, so §4c's
#: "durations live only in core/timing.py" does not apply.
MAX_TRACKED_CURSORS = 512

#: Census finding 1: the ALLOW-SET, not the census enumeration.  Only these three
#: ``.type`` values carry a mapped kind; every other value is ignored BEFORE the
#: uuid chain is touched, which is what makes an unlisted sidecar type harmless
#: rather than fatal.  Growing this set is a deliberate act with a census behind
#: it, exactly as ``EventKind`` is.
TRANSCRIPT_ALLOW_SET: frozenset[str] = frozenset({"user", "assistant", "system"})

#: Census finding 2: the explicit end-of-turn marker's ``system.subtype``.  Its
#: siblings — ``stop_hook_summary`` (317), ``compact_boundary`` (9),
#: ``away_summary`` (3) — are boundaries of other things and assert no turn.
_TURN_DURATION_SUBTYPE = "turn_duration"


@dataclass
class _FileCursor:
    """Where the tail has read up to in ONE transcript file.

    Keyed by resolved path in :data:`_cursors`.  ``inode`` and ``size`` together
    are the rotation detector: a changed inode is a new file at the same path,
    and a size below what was already consumed is a truncation.  Either resets
    the cursor to the head of the new content.

    ``seen_prompt_ids`` is census finding 3's dedupe: a queued prompt appears
    TWICE under one ``promptId``, so a turn start is emitted for the first
    appearance only.  It belongs to the cursor rather than to the source because
    the cursor is what survives a detach/re-attach to the same file — a terminal
    that re-attached and re-emitted a turn start for a prompt it had already
    reported would be replaying, which is the one thing the EOF rule exists to
    prevent.
    """

    offset: int = 0
    inode: int | None = None
    size: int = 0
    seeded: bool = False
    seen_prompt_ids: set[str] = field(default_factory=set)


_lock = threading.RLock()
#: resolved path string -> cursor.  B5: NEVER keyed by ``terminal_id``.
_cursors: dict[str, _FileCursor] = {}
#: terminal_id -> the live source for it.
_sources: dict[str, "ClaudeTranscriptSource"] = {}


def reset_sources() -> None:
    """Drop every source and cursor.  For tests and for a re-installed bootstrap."""
    with _lock:
        sources = list(_sources.values())
        _sources.clear()
        _cursors.clear()
    for source in sources:
        source.stop_sync()


def _cursor_for(path: str) -> _FileCursor:
    with _lock:
        cursor = _cursors.get(path)
        if cursor is None:
            _evict_stale_cursors_locked()
            cursor = _FileCursor()
            _cursors[path] = cursor
        return cursor


def _evict_stale_cursors_locked() -> None:
    """Keep the cursor table bounded.  Caller holds ``_lock``.

    Cursors deliberately OUTLIVE their sources — that is B5, and it is what makes
    a detach followed by a re-attach to the same transcript re-read nothing.  The
    price is one entry per transcript path the server has ever seen, on a server
    meant to run for weeks: a slow leak, which is the kind that is never noticed.

    Eviction is oldest-first by insertion order and never touches a cursor a LIVE
    source is using, so the B5 guarantee holds for every terminal that could still
    care about it.
    """
    if len(_cursors) < MAX_TRACKED_CURSORS:
        return
    live = {source.path_key for source in _sources.values()}
    for path in list(_cursors):
        if len(_cursors) < MAX_TRACKED_CURSORS:
            return
        if path not in live:
            del _cursors[path]


class ClaudeTranscriptSource:
    """Tails one terminal's claude_code transcript.

    Satisfies :class:`~cli_agent_orchestrator.core.ports.EventSource` structurally.
    Runs as an asyncio task in the one process (U7) when a loop is available;
    :meth:`poll_once` is the whole of the work and is callable synchronously,
    which is what makes this testable without a loop.
    """

    def __init__(
        self,
        terminal_id: str,
        path: Path | str,
        session_id: str = "",
        *,
        resumed: bool = False,
    ) -> None:
        self.terminal_id = terminal_id
        self.path = Path(path)
        self.path_key = str(self.path)
        self.session_id = session_id
        self._resumed = resumed
        self._task: asyncio.Task[None] | None = None
        self._stopping = threading.Event()
        self._last_submission_ref: str | None = None
        self._announced_session = False
        self._seed_cursor()

    def _seed_cursor(self) -> None:
        """Decide the starting offset AT ATTACH TIME, once per path.

        The EOF rule is about the moment the tail attaches, not about the first
        poll that happens to succeed.  Deferring it to the first successful stat
        looks equivalent and is not: a FRESH session is attached before the file
        exists, and by the time it does it already holds the session header and
        the first turn.  Treating that as "first stat, so start at EOF" would
        silently skip the whole beginning of every new session.

        So: a file that exists at attach starts at its current size; a file that
        does not yet exist starts at 0, which is its EOF at this instant and also
        its head.  A cursor that already exists for this PATH is left alone — B5,
        and what makes a resume (census finding 4: the SAME file, a new binding
        epoch) replay nothing.
        """
        cursor = _cursor_for(self.path_key)
        with _lock:
            if cursor.seeded:
                return
            cursor.seeded = True
            try:
                stat = self.path.stat()
            except OSError:
                cursor.offset = 0
                cursor.inode = None
                cursor.size = 0
                return
            cursor.offset = stat.st_size
            cursor.inode = stat.st_ino
            cursor.size = stat.st_size

    # -- EventSource ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "claude_transcript"

    @property
    def is_authoritative(self) -> bool:
        """True.  This is the declaration source-level precedence reads (r9)."""
        return True

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name=f"claude-transcript:{self.terminal_id}")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def stop_sync(self) -> None:
        """Signal the loop to end without awaiting it — for teardown from sync code."""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    async def _run(self) -> None:
        interval = ROLLOUT_POLL_MS / 1000.0
        while not self._stopping.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - the never-break-the-server rule
                logger.debug(
                    "claude transcript poll failed for %s", self.terminal_id, exc_info=True
                )
            await asyncio.sleep(interval)

    # -- the work ------------------------------------------------------------

    @property
    def last_submission_source_ref(self) -> str | None:
        """``source_ref`` of the most recent ``submission.confirmed`` seen.

        What D7's replacement for the codex confirm ladder reads, and what makes
        I4 true for claude_code: a submission is confirmed by a record the worker
        itself wrote, joined by ``source_ref`` rather than by a timestamp window.
        """
        return self._last_submission_ref

    def poll_once(self) -> int:
        """Read every complete new line and emit its events.  Returns rows emitted.

        Every poll that STATS the file bumps ``last_source_probe_at``, including
        one that finds nothing new.  A tailer that only reported liveness when the
        file grew would degrade an idle terminal, and degrading an idle terminal
        is how a cutover turns a quiet worker into a status outage.
        """
        runtime = producer_runtime()
        if runtime is None:
            return 0

        try:
            stat = self.path.stat()
        except OSError:
            # Not there yet (a fresh session before the first write) or gone.
            # Neither an error nor a source-health signal: the projector's
            # NO_SIGNAL_S horizon is what notices a source that never comes back.
            return 0

        self._touch_source_probe(runtime)

        cursor = _cursor_for(self.path_key)
        with _lock:
            rotated = cursor.inode is not None and (
                cursor.inode != stat.st_ino or stat.st_size < cursor.offset
            )
            if rotated:
                # A new file at the same path, or a truncation.  Its head is
                # genuinely new content, so read from 0 rather than carrying the
                # old file's offset into it — and forget the prompt ids, which
                # belonged to the file that is gone.
                cursor.offset = 0
                cursor.seen_prompt_ids.clear()
            cursor.inode = stat.st_ino
            cursor.size = stat.st_size
            start_offset = cursor.offset

        if stat.st_size <= start_offset:
            return 0

        try:
            with self.path.open("rb") as handle:
                handle.seek(start_offset)
                chunk = handle.read(MAX_POLL_BYTES)
        except OSError:
            return 0

        newline = chunk.rfind(b"\n")
        if newline < 0 and len(chunk) >= MAX_POLL_BYTES:
            # A single record longer than the whole poll window.  Waiting for a
            # newline that will never arrive inside the window would stall this
            # terminal's tail FOREVER — and, worse, ``last_source_probe_at`` would
            # keep being bumped, so the projector would go on believing the source
            # healthy while it had in fact stopped reporting.  A silent permanent
            # stall is the worst outcome available, so the window is skipped, the
            # oversized record is lost, and the loss is logged.
            logger.warning(
                "claude transcript record exceeds %d bytes at %s#%d; skipping it to "
                "keep the tail moving",
                MAX_POLL_BYTES,
                self.path_key,
                start_offset,
            )
            with _lock:
                cursor.offset = start_offset + len(chunk)
            return 0
        if newline < 0:
            # One incomplete line so far.  Consume nothing; the next poll reads it
            # whole.  The tailer NEVER parses a partial line.
            return 0
        complete = chunk[: newline + 1]

        emitted = 0
        line_offset = start_offset
        for raw in complete.split(b"\n")[:-1]:
            record_offset = line_offset
            line_offset += len(raw) + 1
            text = raw.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            emitted += self._emit_for_record(record, record_offset, cursor, runtime)

        with _lock:
            cursor.offset = start_offset + len(complete)
        return emitted

    def _touch_source_probe(self, runtime: Any) -> None:
        state_store = runtime.state_store
        if state_store is None:
            return
        try:
            state_store.touch_source_probe(self.terminal_id, probed_at=runtime.clock.now())
        except Exception:
            logger.debug("touch_source_probe failed for %s", self.terminal_id, exc_info=True)

    def _source_ref(self, record: dict[str, Any], offset: int) -> str:
        """``transcript:<resolved path>#<record uuid>`` (§5).

        A record with no ``uuid`` — the ``system``/``turn_duration`` line carries
        ``parentUuid`` rather than one of its own — falls back to its parent and
        then to its byte offset, which is unique within the file.  A ``source_ref``
        missing its discriminator would look like provenance and identify nothing,
        which is worse than a null because a reader would believe it.
        """
        discriminator = record.get("uuid") or record.get("parentUuid") or f"@{offset}"
        return source_ref(SourceRefScheme.TRANSCRIPT, self.path_key, discriminator)

    def _emit_for_record(
        self, record: dict[str, Any], offset: int, cursor: _FileCursor, runtime: Any
    ) -> int:
        # Census finding 1: the allow-set filter runs FIRST, before anything reads
        # the uuid chain.  An unlisted sidecar type is ignored here and can never
        # reach the classifier below.
        record_type = record.get("type")
        if not isinstance(record_type, str) or record_type not in TRANSCRIPT_ALLOW_SET:
            return 0

        kinds = self._classify(record, cursor)
        if not kinds:
            return 0

        ref = self._source_ref(record, offset)
        emitted = 0
        for kind in kinds:
            stored = emit(
                EventDraft(
                    terminal_id=self.terminal_id,
                    kind=kind,
                    producer=Producer.JSONL,
                    confidence=Confidence.AUTHORITATIVE,
                    observed_at=runtime.clock.now(),
                    source_ref=ref,
                    payload={
                        "record_type": record_type,
                        "subtype": record.get("subtype"),
                        "session_id": record.get("sessionId"),
                        "transcript_timestamp": record.get("timestamp"),
                        "uuid": record.get("uuid"),
                    },
                )
            )
            if stored is None:
                continue
            emitted += 1
            if kind is EventKind.SUBMISSION_CONFIRMED:
                self._last_submission_ref = ref
        return emitted

    def _classify(self, record: dict[str, Any], cursor: _FileCursor) -> tuple[EventKind, ...]:
        """Map one transcript record to the kinds it asserts.

        Returns a TUPLE because one record can assert several: census finding 3's
        start-of-turn ``user`` record is both ``turn.started`` and — it is the
        worker's own record of the prompt reaching it — ``submission.confirmed``.
        Splitting those across two records would mean inventing one.

        Returns empty for everything else, which is the common case and the right
        default: streaming detail is not an event (the OpenCode lesson, audit §6).

        The session announcement PRECEDES the record's own kinds rather than
        replacing them.  §5 defines ``session.started`` as "the first record of
        the file", and that record is also whatever else it is — usually the first
        turn.  Returning the session kind alone would swallow it, which is a whole
        turn lost at the head of every fresh session, in the arm where the tailer
        has the least other evidence.
        """
        own = self._own_kinds(record, cursor)
        if not own:
            # A record that asserts nothing announces nothing either.  The
            # alternative — announcing the session on the first record that merely
            # passed the allow-set — would open a session on a
            # ``stop_hook_summary``, which is five times more common than the
            # turn marker and says nothing about a session having begun.
            return ()
        session_kind = self._session_kind(record)
        return (session_kind, *own) if session_kind is not None else own

    def _own_kinds(self, record: dict[str, Any], cursor: _FileCursor) -> tuple[EventKind, ...]:
        """What this record asserts on its own, ignoring the session announcement."""
        record_type = record.get("type")

        if record_type == "system":
            if record.get("subtype") == _TURN_DURATION_SUBTYPE:
                return (EventKind.TURN_ENDED,)
            return ()

        if record_type == "assistant":
            return (
                (EventKind.TOOL_CALLED,) if self._first_content_type(record) == "tool_use" else ()
            )

        if record_type == "user":
            content_type = self._first_content_type(record)
            if content_type == "tool_result":
                # Census finding 3: a tool result is a ``user`` record too, and by
                # a factor of fifty (1,077 against 20).  Reading ``type == "user"``
                # as a turn start would make almost every tool result one.
                return (EventKind.TOOL_RESULT,)
            if content_type != "text":
                return ()
            prompt_id = record.get("promptId") or record.get("prompt_id")
            if isinstance(prompt_id, str) and prompt_id:
                with _lock:
                    if prompt_id in cursor.seen_prompt_ids:
                        # A queued prompt appears twice under one id.  The second
                        # appearance is the same submission arriving again, not a
                        # second turn.
                        return ()
                    cursor.seen_prompt_ids.add(prompt_id)
            return (EventKind.TURN_STARTED, EventKind.SUBMISSION_CONFIRMED)

        return ()

    @staticmethod
    def _first_content_type(record: dict[str, Any]) -> str | None:
        """``message.content[0].type``, or ``None`` when the record has no block.

        The FIRST block, not any block, and that is census finding 3's rule rather
        than a convenience: a turn-start prompt leads with ``text`` and a tool
        result leads with ``tool_result``.  Scanning for a matching block anywhere
        in the list would classify an assistant message that happens to contain
        both a text block and a ``tool_use`` as whichever the search hit first.

        Defensive about shape at every hop.  A record that fails the allow-set
        never reaches here, but the records that pass it are still written by
        another program on its own schedule, and a producer that raised on an
        unexpected shape would stall the tail on one malformed line.
        """
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, list) or not content:
            return None
        first = content[0]
        if not isinstance(first, dict):
            return None
        block_type = first.get("type")
        return block_type if isinstance(block_type, str) else None

    def _session_kind(self, record: dict[str, Any]) -> EventKind | None:
        """``session.started`` for the file's first record, ``session.resumed`` for
        a record whose ``sessionId`` differs from the binding (§5).

        Announced ONCE per attach.  The two are mutually exclusive and the resume
        test comes first: a source attached on a resume epoch is resuming even if
        the record it happens to see first is the file's first, which is the case
        the ``--resume`` criterion turns on.
        """
        if self._announced_session:
            return None
        record_session = record.get("sessionId")
        differs = (
            isinstance(record_session, str)
            and bool(record_session)
            and bool(self.session_id)
            and record_session != self.session_id
        )
        if self._resumed or differs:
            self._announced_session = True
            return EventKind.SESSION_RESUMED
        self._announced_session = True
        return EventKind.SESSION_STARTED


def attach(
    terminal_id: str,
    path: Path | str | None,
    session_id: str = "",
    *,
    resumed: bool = False,
) -> None:
    """Hand the resolved transcript path to the tailer.  Idempotent and cheap.

    Called from the ``transcript-binding`` route's handler, which is where the
    binding epoch arrives.  Census finding 4 is what shapes the re-attach rule: a
    resume writes the SAME file under a NEW binding epoch, so a second attach for
    the same path must keep the cursor.  Only a DIFFERENT path is a different
    file, and that path gets its own cursor.

    A re-attach for the same path with a different ``session_id`` is a resume
    epoch: the source is replaced so it announces ``session.resumed`` once, while
    the cursor — which belongs to the file — carries on where it was.  That is the
    whole of AC-2a's resumed-session criterion, and the reason it is written
    against a cursor keyed by path rather than by terminal.
    """
    if path is None or not terminal_id:
        return
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        path_key = str(Path(path))
        with _lock:
            existing = _sources.get(terminal_id)
            if (
                existing is not None
                and existing.path_key == path_key
                and existing.session_id == session_id
            ):
                return
        epoch_change = existing is not None and existing.path_key == path_key
        if existing is not None:
            existing.stop_sync()
        source = ClaudeTranscriptSource(
            terminal_id, path, session_id, resumed=resumed or epoch_change
        )
        with _lock:
            _sources[terminal_id] = source
        source.poll_once()
        _schedule(source)
    except Exception:  # pragma: no cover - the never-break-the-server rule
        logger.debug("claude transcript attach failed for %s", terminal_id, exc_info=True)


def _schedule(source: ClaudeTranscriptSource) -> None:
    """Start the polling task when a loop is running; otherwise stay synchronous.

    The binding route runs inside the server's loop, but tests and the
    synchronous catch-up read have none.  Asking for a loop that is not there
    raises, so its absence is treated as "this process drives the tail by hand".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        asyncio.ensure_future(source.start())
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not schedule claude transcript tail", exc_info=True)


def detach(terminal_id: str) -> None:
    """Stop tailing for one terminal.  The path's cursor is deliberately KEPT.

    B5 again: the cursor belongs to the file, not to the terminal.
    """
    with _lock:
        source = _sources.pop(terminal_id, None)
    if source is not None:
        source.stop_sync()


def source_for(terminal_id: str) -> ClaudeTranscriptSource | None:
    with _lock:
        return _sources.get(terminal_id)


def latest_submission_source_ref(terminal_id: str, *, catch_up: bool = True) -> str | None:
    """The ``source_ref`` of this terminal's newest ``submission.confirmed``.

    ``catch_up`` forces ONE synchronous poll first, for the same reason the codex
    adapter does: a dispatch can exit earlier than the tailer's next tick, and a
    row that cited nothing merely because the poll had not come round would be
    indistinguishable from a genuinely unconfirmed send.
    """
    source = source_for(terminal_id)
    if source is None:
        return None
    if catch_up:
        try:
            source.poll_once()
        except Exception:
            logger.debug("catch-up poll failed for %s", terminal_id, exc_info=True)
    return source.last_submission_source_ref
