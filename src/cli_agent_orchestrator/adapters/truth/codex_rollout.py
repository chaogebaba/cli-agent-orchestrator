"""The codex rollout JSONL tail — phase 1's only authoritative producer (AC4).

Decision U6 is JSONL-first: the file codex writes for its own reasons is the
truth, and the phase-2 hooks are an accelerator on top of it.  This producer
therefore declares ``is_authoritative = True``, and the projector's source-level
precedence (r9) leans on that declaration: while this tailer is healthy, a pane
observation may no longer decide that a codex terminal is busy.

**It emits only what the file carries.**  A census of a live rollout
(2026-09-02, 427 files under ``~/.codex/sessions``) fixes the four record shapes:

===========================  ==================================================
rollout record               emitted event
===========================  ==================================================
``session_meta``             ``session.started`` / ``session.resumed``
``event_msg`` ``task_started``   ``turn.started``
``event_msg`` ``task_complete``  ``turn.ended``
``response_item`` ``message``
with ``role == "user"``      ``submission.confirmed``
===========================  ==================================================

``usage.capped`` and ``process.exited`` are NOT emitted here, however tempting:
the rollout records neither.  A cap is a pane/legacy-egress observation and an
exit belongs to the liveness probe, which is its single owner.  Inventing either
from the absence of records is how a diagnostic surface starts lying.

Three properties are load-bearing and each is a phase-1 mutant if lost:

* **The offset is keyed by the RESOLVED PATH, never by ``terminal_id`` (B5).**  A
  terminal outlives its rollout files — kill, respawn, resume, and the same
  terminal id points at a different file.  Keying by terminal would carry a stale
  offset into a new file and either skip its head or replay it.
* **Rotation is detected by inode and size, never by name.**  Same path, new
  inode, or a file that shrank, means a different file; the offset restarts.
* **A first attach with no persisted offset starts at EOF, never at 0.**  A
  resumed codex session's rollout already contains the whole prior conversation.
  Starting at 0 would replay every historical turn as if it had just happened —
  the single worst thing a truth log can do.

That last rule has a consequence worth stating plainly: a resumed session's
``session_meta`` sits at byte 0 and is therefore never read, so no
``session.resumed`` row is written for it.  That is correct rather than
unfortunate.  The record describes a boundary that happened before this producer
attached, and emitting it would be a replay wearing a different name.

The tailer never parses a partial line.  Only bytes up to the last newline are
consumed, and the offset advances to that newline, so a record caught mid-write
is read whole on the next poll.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from cli_agent_orchestrator.adapters.truth.wiring import current_runtime, emit
from cli_agent_orchestrator.core.events import Confidence, EventDraft, EventKind, Producer
from cli_agent_orchestrator.core.timing import ROLLOUT_POLL_MS

__all__ = [
    "CodexRolloutSource",
    "attach",
    "rollout_resolution_hook",
    "detach",
    "latest_submission_source_ref",
    "reset_sources",
    "source_for",
]

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

#: Ceiling on how many bytes one poll consumes.  A rollout grows by kilobytes per
#: turn, so this is never reached in normal operation; it exists so that
#: attaching to a pathological file cannot block the event loop, and so the
#: synchronous catch-up read performed on the dispatch path (see
#: :func:`latest_submission_source_ref`) has a bounded cost.
MAX_POLL_BYTES = 4 * 1024 * 1024

#: How many per-path cursors are retained.  A COUNT, not a duration, so §4c's
#: "durations live only in core/timing.py" does not apply.  See
#: :func:`_evict_stale_cursors_locked` for why the table needs a bound at all.
MAX_TRACKED_CURSORS = 512


@dataclass
class _FileCursor:
    """Where the tail has read up to in ONE rollout file.

    Keyed by resolved path in :data:`_cursors`.  ``inode`` and ``size`` together
    are the rotation detector: a changed inode is a new file at the same path,
    and a size below what was already consumed is a truncation.  Either resets
    the cursor to the head of the new content.
    """

    offset: int = 0
    inode: int | None = None
    size: int = 0
    seeded: bool = False


_lock = threading.RLock()
#: resolved path string -> cursor.  B5: NEVER keyed by ``terminal_id``.
_cursors: dict[str, _FileCursor] = {}
#: terminal_id -> the live source for it.
_sources: dict[str, "CodexRolloutSource"] = {}


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
    a detach followed by a re-attach to the same rollout re-read nothing.  The
    price is that a long-lived server accumulates one entry per rollout path it
    has ever seen, and the server is meant to run for weeks.  Each entry is tiny,
    so this is a slow leak rather than a fast one, which is exactly the kind that
    is never noticed and never fixed.

    Eviction is oldest-first by insertion order and never touches a cursor a LIVE
    source is using, so the B5 guarantee holds for every terminal that could still
    care about it.  A cursor evicted after its source is gone costs at most one
    EOF re-seed if that same path is ever attached again.
    """
    if len(_cursors) < MAX_TRACKED_CURSORS:
        return
    live = {source._path_key for source in _sources.values()}
    for path in list(_cursors):
        if len(_cursors) < MAX_TRACKED_CURSORS:
            return
        if path not in live:
            del _cursors[path]


class CodexRolloutSource:
    """Tails one terminal's rollout file.

    Satisfies :class:`~cli_agent_orchestrator.core.ports.EventSource` structurally.
    Runs as an asyncio task in the one process (U7) when a loop is available;
    :meth:`poll_once` is the whole of the work and is callable synchronously,
    which is what makes this testable without a loop and what lets the dispatch
    path force a catch-up read.
    """

    def __init__(
        self,
        terminal_id: str,
        path: Path | str,
        *,
        resumed: bool = False,
    ) -> None:
        self.terminal_id = terminal_id
        self.path = Path(path)
        self._path_key = str(self.path)
        self._resumed = resumed
        self._task: asyncio.Task[None] | None = None
        self._stopping = threading.Event()
        self._last_submission_ref: str | None = None
        self._seed_cursor()

    def _seed_cursor(self) -> None:
        """Decide the starting offset AT ATTACH TIME, once per path.

        The EOF rule is about the moment the tail attaches, not about the first
        poll that happens to succeed.  Deferring it to the first successful stat
        looks equivalent and is not: a FRESH session is attached before codex has
        created the file, and by the time it exists it already holds the session
        header and the first turn.  Treating that as "first stat, so start at
        EOF" would silently skip the whole beginning of every new session.

        So: a file that exists at attach starts at its current size; a file that
        does not yet exist starts at 0, which is its EOF at this instant and also
        its head.  A cursor that already exists for this PATH is left alone —
        that is B5, and it is what makes detach/re-attach re-read nothing.
        """
        cursor = _cursor_for(self._path_key)
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
        return "codex_rollout"

    @property
    def is_authoritative(self) -> bool:
        """Always True.  This is the declaration source-level precedence reads."""
        return True

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name=f"codex-rollout:{self.terminal_id}")

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
                logger.debug("codex rollout poll failed for %s", self.terminal_id, exc_info=True)
            await asyncio.sleep(interval)

    # -- the work ------------------------------------------------------------

    @property
    def last_submission_source_ref(self) -> str | None:
        """``source_ref`` of the most recent ``submission.confirmed`` seen.

        The dispatch hook (AC4c) cites this on a ``confirmed`` delivery so the
        decision row and the worker-truth row join by ``source_ref`` rather than
        by a timestamp window — N3's stamping window is retired (r9).
        """
        return self._last_submission_ref

    def poll_once(self) -> int:
        """Read every complete new line and emit its events.  Returns rows emitted.

        Every poll that STATS the file bumps ``last_source_probe_at``, which is
        the column source health is measured from — including a poll that finds
        nothing new.  A quiet rollout is a healthy rollout, and a tailer that only
        reported liveness when the file grew would degrade an idle terminal.
        """
        runtime = current_runtime()
        if runtime is None:
            return 0

        try:
            stat = self.path.stat()
        except OSError:
            # The file is not there yet (a fresh session before codex creates it)
            # or has gone.  Not an error and not a source-health signal: the
            # projector's NO_SIGNAL_S horizon is what notices a source that never
            # comes back.
            return 0

        self._touch_source_probe(runtime)

        cursor = _cursor_for(self._path_key)
        with _lock:
            rotated = cursor.inode is not None and (
                cursor.inode != stat.st_ino or stat.st_size < cursor.offset
            )
            if rotated:
                # A new file at the same path, or a truncation.  Its head is
                # genuinely new content, so read from 0 rather than carrying the
                # old file's offset into it.
                cursor.offset = 0
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
            # terminal's tail FOREVER: no further event, and — worse —
            # ``last_source_probe_at`` would keep being bumped, so the projector
            # would go on believing the source is healthy while it has in fact
            # stopped reporting.  A silent permanent stall is the worst outcome
            # available here, so the window is skipped, the oversized record is
            # lost (its remainder fails to parse on the next poll and is
            # discarded), and the loss is logged.
            logger.warning(
                "codex rollout record exceeds %d bytes at %s#%d; skipping it to "
                "keep the tail moving",
                MAX_POLL_BYTES,
                self._path_key,
                start_offset,
            )
            with _lock:
                cursor.offset = start_offset + len(chunk)
            return 0
        if newline < 0:
            # A single incomplete line so far.  Consume nothing; the next poll
            # reads it whole.
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
            emitted += self._emit_for_record(record, record_offset, runtime)

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

    def _source_ref(self, offset: int) -> str:
        return f"rollout:{self._path_key}#{offset}"

    def _emit_for_record(self, record: dict[str, Any], offset: int, runtime: Any) -> int:
        kind = self._classify(record)
        if kind is None:
            return 0
        payload = record.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        draft = EventDraft(
            terminal_id=self.terminal_id,
            kind=kind,
            producer=Producer.JSONL,
            confidence=Confidence.AUTHORITATIVE,
            observed_at=runtime.clock.now(),
            source_ref=self._source_ref(offset),
            payload={
                "record_type": record.get("type"),
                "payload_type": payload_dict.get("type"),
                "rollout_timestamp": record.get("timestamp"),
                "ordinal": record.get("ordinal"),
            },
        )
        stored = emit(draft)
        if stored is None:
            return 0
        if kind is EventKind.SUBMISSION_CONFIRMED:
            self._last_submission_ref = draft.source_ref
        return 1

    def _classify(self, record: dict[str, Any]) -> EventKind | None:
        """Map one rollout record to an event kind, or ``None`` to ignore it.

        Ignoring is the common case and the right default: ``token_count``,
        ``reasoning``, ``custom_tool_call`` and ``item_completed`` are the bulk of
        a rollout and none of them is a boundary.  Streaming detail is not an
        event (the OpenCode lesson, audit §6).
        """
        record_type = record.get("type")
        payload = record.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}

        if record_type == "session_meta":
            return (
                EventKind.SESSION_RESUMED
                if self._is_resume(payload_dict)
                else (EventKind.SESSION_STARTED)
            )
        if record_type == "event_msg":
            payload_type = payload_dict.get("type")
            if payload_type == "task_started":
                return EventKind.TURN_STARTED
            if payload_type == "task_complete":
                return EventKind.TURN_ENDED
            return None
        if record_type == "response_item":
            if payload_dict.get("type") == "message" and payload_dict.get("role") == "user":
                return EventKind.SUBMISSION_CONFIRMED
            return None
        return None

    def _is_resume(self, payload: dict[str, Any]) -> bool:
        """Whether this ``session_meta`` describes a resumed session.

        The attach-time declaration is the load-bearing signal: the provider knows
        it is resuming (``fork_context.mode == "resume"``) and hands that in.  The
        payload probe below is a forward-compatible second opinion — the
        2026-09-02 census of 427 rollout files found NO resume marker in
        ``session_meta`` (every file carried the same key set and exactly one
        ``session_meta``), so codex today records a resume by writing a fresh
        rollout rather than by flagging one.  If a future codex adds a marker,
        this picks it up without another census.
        """
        if self._resumed:
            return True
        if payload.get("thread_source") == "resumed":
            return True
        return any(key in payload for key in ("resumed_from", "parent_id", "rollout_id"))


def attach(terminal_id: str, path: Path | str | None, *, resumed: bool = False) -> None:
    """Hook point 1 — the resolved rollout path is HANDED IN.

    New code never reaches into ``providers/codex.py`` to resolve a path (N2):
    ``_resolve_rollout_file`` already does it by ``session_uuid``, and duplicating
    that resolution would be a second implementation of the identity rules B5 r7
    exists to enforce.

    Idempotent and cheap.  ``_resolve_rollout_file`` is called on a poll during
    submission verification, so this runs many times per dispatch; re-attaching
    the same path for the same terminal returns immediately.  A DIFFERENT path
    for a known terminal is a respawn or a resume: the old source stops and a new
    one attaches, which restarts at the new file's EOF under its own cursor.
    """
    if path is None or not terminal_id:
        return
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        path_key = str(Path(path))
        with _lock:
            existing = _sources.get(terminal_id)
            if existing is not None and existing._path_key == path_key:
                return
        if existing is not None:
            existing.stop_sync()
        source = CodexRolloutSource(terminal_id, path, resumed=resumed)
        with _lock:
            _sources[terminal_id] = source
        source.poll_once()
        _schedule(source)
    except Exception:  # pragma: no cover - the never-break-the-provider rule
        logger.debug("codex rollout attach failed for %s", terminal_id, exc_info=True)


def _schedule(source: CodexRolloutSource) -> None:
    """Start the polling task when a loop is running; otherwise stay synchronous.

    ``_resolve_rollout_file`` is called from both async server code and plain
    synchronous provider code (and from tests with no loop at all).  Asking for a
    loop that is not there raises, so the absence of one is treated as "this
    process drives the tail by hand", which is exactly what the tests and the
    dispatch-path catch-up read do.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        asyncio.ensure_future(source.start())
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not schedule codex rollout tail", exc_info=True)


def detach(terminal_id: str) -> None:
    """Stop tailing for one terminal.  The path's cursor is deliberately KEPT.

    B5 again: the cursor belongs to the file, not to the terminal.  A terminal
    that detaches and re-attaches to the same rollout must not re-read what it
    already consumed, and a terminal that attaches to a different file gets that
    file's own cursor.
    """
    with _lock:
        source = _sources.pop(terminal_id, None)
    if source is not None:
        source.stop_sync()


def source_for(terminal_id: str) -> CodexRolloutSource | None:
    with _lock:
        return _sources.get(terminal_id)


def latest_submission_source_ref(terminal_id: str, *, catch_up: bool = True) -> str | None:
    """The ``source_ref`` of this terminal's newest ``submission.confirmed``.

    ``catch_up`` forces ONE synchronous poll first.  The dispatch hook runs at the
    exit of ``send_input``, which can be earlier than the tailer's next 500 ms
    tick, and a ``delivery.attempt`` row that cited nothing merely because the
    poll had not come round yet would be indistinguishable from a genuinely
    unconfirmed send.  The cost is one ``stat`` plus a bounded read of the bytes
    written since the last poll — the same order of I/O the legacy verify path
    (``_rollout_has_user_event``) already performs on this exact code path.
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


def rollout_resolution_hook(func: F) -> F:
    """Hook point 1 — hand the resolved rollout path to the tailer.

    Wraps ``CodexProvider._resolve_rollout_file``.  A decorator rather than a
    statement, because that method has five return points (exact UUID match,
    identity-validated ambiguity, mtime fallback, resume seed, newest-file
    fallback) and a hook that covered only some of them would tail the right file
    for a fresh session and the wrong one — or none — for a resumed one.

    With ingestion off the wrapper delegates straight through: no try, no extra
    work, no change to what the provider returns.  With it on it still returns
    the provider's value unchanged; :func:`attach` is idempotent and this method
    is called repeatedly while a submission is being verified.
    """

    @functools.wraps(func)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        resolved = func(self, *args, **kwargs)
        if current_runtime() is None:
            return resolved
        try:
            resume_getter = getattr(self, "resume_session_uuid", None)
            resumed = bool(resume_getter()) if callable(resume_getter) else False
            attach(str(getattr(self, "terminal_id", "")), resolved, resumed=resumed)
        except Exception:  # pragma: no cover - the never-break-the-provider rule
            logger.debug("rollout resolution hook failed", exc_info=True)
        return resolved

    return wrapper  # type: ignore[return-value]
