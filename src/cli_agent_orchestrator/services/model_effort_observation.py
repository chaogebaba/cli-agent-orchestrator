"""F826 (#683) — per-provider model + reasoning-effort observation.

Two kinds of evidence, never conflated (D1):

* **SELECTED** — what the CLI will use for the *next* request. Only Claude
  produces this, through the ``status_emit`` sidecar (a true selection
  emitter). Marker ``[L]`` (live).
* **OBSERVED** — the last observed evidence: a request, a response, or a state
  checkpoint. codex / pi tail their own logs (``[R]``); kiro reads a session
  checkpoint (``[R]``, kind ``checkpoint``); a codex settings-change record
  upgrades to ``[L]``.

Each Model / Effort **field** carries its own independent observation (astra
Q4 / SHOULD-1): model and effort need not share evidence, kind or age. The
projector (``fleet_service.build_fleet``) merges these additively with the
*configured* DB columns, which are NEVER overwritten (D1 / Do-NOT).

Marker precedence L > R > S > C > ? (D1). Decay: an ``[L]`` value whose sidecar
``event_time`` is older than 3 refresh intervals (4.5 s) becomes ``[S]``,
evaluated at ``build_fleet`` time against the sidecar's OWN ``event_time``,
never poll/render age (S6). A conflict between an observation and the
configured value adds ``!`` (D1) — that comparison is the projector's job; this
module reports the raw observation and the source's own event time.

Reader discipline (D2 / D6 / SHOULD-2, astra):

* Bind by exact terminal → file identity handed in by the caller, NEVER "newest
  file in a directory".
* Cache key = ``(binding_generation, path, mtime_ns, size)``; an unchanged file
  costs one ``stat`` and returns the cached projection. Dropping ``mtime_ns``
  from the key is the AC6 "stale relabel" mutant.
* Fixed read budget: bounded tail (≤ 64 KB) for the sidecar / pi; a bounded,
  cached-offset **incremental forward scan** for codex (D4/D6 amended for
  codex — its ``turn_context`` is head-clustered, so an EOF tail sees nothing
  in most real sessions); a bounded whole-file read for the small kiro
  checkpoint.
* Per-terminal isolation: one bad source raises nothing to its neighbours; a
  50 ms per-source deadline and the read budget are enforced by the caller
  (``build_fleet`` wraps each call). No subprocess per GET.
* No transcript / rollout body text is retained or surfaced (AC4): only the
  allowlisted scalars model / effort / event_time / kind leave this module.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "Marker",
    "ObservationKind",
    "Observation",
    "ProviderObservation",
    "REFRESH_INTERVAL_NS",
    "DECAY_NS",
    "TAIL_BUDGET_BYTES",
    "SIDECAR_RETENTION_NS",
    "observe_claude",
    "observe_codex",
    "observe_pi",
    "observe_kiro",
    "observe_provider",
    "sweep_stale_sidecars",
    "reset_caches",
]


# --- constants --------------------------------------------------------------

#: The Claude statusLine ``refreshInterval`` (D3), in nanoseconds.
REFRESH_INTERVAL_NS: int = 1_500_000_000
#: An ``[L]`` value older than 3 refresh intervals (4.5 s) decays to ``[S]``
#: (D1). Measured against the sidecar's own ``event_time`` (S6).
DECAY_NS: int = 3 * REFRESH_INTERVAL_NS
#: The fixed tail read budget for tail-based adapters (sidecar / pi) — AC5.
TAIL_BUDGET_BYTES: int = 64 * 1024
#: codex: first-pass whole-file scan ceiling (ruling). Beyond it, scan head +
#: tail only and render ``[?]`` if no signal.
CODEX_FIRST_PASS_MAX_BYTES: int = 16 * 1024 * 1024
#: codex: per-poll incremental read ceiling (ruling); remainder carried forward.
CODEX_INCREMENTAL_MAX_BYTES: int = 1 * 1024 * 1024
#: codex: head/tail window sizes used only when the first pass exceeds the cap.
CODEX_HEAD_BYTES: int = 256 * 1024
CODEX_TAIL_BYTES: int = 64 * 1024
#: Sidecar retention (S3): a sidecar OUTLIVES its terminal so `[S] exited` can
#: be shown, but is swept 14 days after its last write to bound growth.
SIDECAR_RETENTION_NS: int = 14 * 24 * 60 * 60 * 1_000_000_000


class Marker(str, Enum):
    """The single-character cell marker (D1). Precedence L > R > S > C > ?."""

    LIVE = "L"  # SELECTED — current selection from a valid emitter
    OBSERVED = "R"  # last observed request/response/checkpoint evidence
    STALE = "S"  # a previous observation whose validity cannot be established
    CONFIGURED = "C"  # configured-only (launch resolution); projector-supplied
    UNKNOWN = "?"  # neither observation nor configured value


class ObservationKind(str, Enum):
    """The semantic kind of an OBSERVED value, shown in the row details (D1)."""

    SELECTED = "selected"  # a settings/selection event (→ [L])
    REQUEST = "request"  # a request record
    RESPONSE = "response"  # a response/assistant record
    CHECKPOINT = "checkpoint"  # a state checkpoint (kiro session file)


@dataclass(frozen=True, slots=True)
class Observation:
    """One independent per-field observation (astra Q4 / SHOULD-1).

    ``value`` — the observed model or effort string, or ``None`` when the source
    carried nothing usable. ``marker`` — L/R/S/?/... (C is projector-supplied,
    never emitted here). ``event_time_ns`` — the SOURCE's own event time (the
    emitter's wall clock for the sidecar; the record timestamp otherwise), which
    the decay rule and out-of-order rejection read (D3/S2/S6). ``source`` — a
    short provenance token for the details line (never a body). ``seq`` — a
    DIAGNOSTIC only; the reader NEVER rejects on it (S2). ``validity`` — a short
    reason string when the value is stale/unusable.
    """

    value: Optional[str]
    marker: Marker
    kind: Optional[ObservationKind] = None
    event_time_ns: Optional[int] = None
    source: Optional[str] = None
    seq: Optional[int] = None
    validity: Optional[str] = None

    @classmethod
    def unknown(cls, *, validity: Optional[str] = None) -> "Observation":
        return cls(value=None, marker=Marker.UNKNOWN, validity=validity)


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """The per-terminal result: independent model + effort observations (D2)."""

    model: Observation
    effort: Observation

    @classmethod
    def unknown(cls, *, validity: Optional[str] = None) -> "ProviderObservation":
        return cls(
            model=Observation.unknown(validity=validity),
            effort=Observation.unknown(validity=validity),
        )


# --- per-file cache ---------------------------------------------------------
#
# SHOULD-2 / D2: key = (binding_generation, path, mtime_ns, size). An unchanged
# file returns the cached projection after a single stat. Dropping mtime_ns is
# the AC6 stale-relabel mutant. codex additionally persists a forward-scan
# cursor keyed by RESOLVED PATH (not terminal id) so a respawn/resume to a new
# file gets its own cursor — the same B5 rule the truth-runtime tailer uses.

_lock = threading.RLock()


@dataclass
class _CacheEntry:
    key: tuple[Any, ...]
    result: ProviderObservation


@dataclass
class _CodexCursor:
    """Forward-scan cursor for one codex rollout PATH (ruling / B5).

    ``offset`` — byte position the scan has consumed up to. ``inode`` / ``size``
    detect rotation/truncation; ``mtime_ns`` detects a same-SIZE in-place
    rewrite (B2 r1): a rewrite that keeps the byte length changes mtime but not
    size, so without this the cursor sits at EOF, reads nothing, and reprojects
    stale values. ``model`` / ``effort`` / ``event_time_ns`` / ``kind`` hold the
    LAST turn_context / thread_settings_applied seen so far, so a poll that
    appends only unrelated records keeps the prior evidence. ``over_cap`` records
    that the first pass hit the 16 MB ceiling and only the head+tail were scanned.
    """

    offset: int = 0
    inode: Optional[int] = None
    size: int = 0
    mtime_ns: Optional[int] = None
    seeded: bool = False
    model: Optional[str] = None
    effort: Optional[str] = None
    event_time_ns: Optional[int] = None
    kind: ObservationKind = ObservationKind.REQUEST
    over_cap: bool = False


_cache: dict[str, _CacheEntry] = {}
_codex_cursors: dict[str, _CodexCursor] = {}


def reset_caches() -> None:
    """Drop every cached projection and codex cursor. For tests."""
    with _lock:
        _cache.clear()
        _codex_cursors.clear()


def sweep_stale_sidecars(observe_dir: Path, *, now_ns: Optional[int] = None) -> int:
    """S3: unlink observe sidecars older than :data:`SIDECAR_RETENTION_NS`.

    The sidecar deliberately outlives its terminal (``cleanup()`` never touches
    ``observe/`` — Do-NOT), so growth is bounded here instead: one file per
    terminal ever spawned, swept 14 days after its last modification. Best
    effort and bounded (one ``listdir`` + one ``stat`` per file); never raises.
    Returns the number of files removed. Callers throttle it (e.g. once per
    fleet build is far too often — see the projector's own guard).
    """
    now = time.time_ns() if now_ns is None else now_ns
    removed = 0
    try:
        entries = list(observe_dir.glob("*.json"))
    except OSError:
        return 0
    for entry in entries:
        try:
            st = entry.stat()
        except OSError:
            continue
        if (now - st.st_mtime_ns) > SIDECAR_RETENTION_NS:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _stat_key(path: Path, generation: Any) -> Optional[tuple[Any, ...]]:
    """The cache key, or ``None`` when the file is absent."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (generation, str(path), st.st_mtime_ns, st.st_size)


def _cached(cache_id: str, key: tuple[Any, ...]) -> Optional[ProviderObservation]:
    with _lock:
        entry = _cache.get(cache_id)
        if entry is not None and entry.key == key:
            return entry.result
    return None


def _store(cache_id: str, key: tuple[Any, ...], result: ProviderObservation) -> None:
    with _lock:
        _cache[cache_id] = _CacheEntry(key=key, result=result)


def _read_tail(path: Path, budget: int) -> bytes:
    """Read at most ``budget`` bytes from the END of ``path``. Bounded (AC5)."""
    with open(path, "rb") as handle:
        try:
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            size = 0
        if size > budget:
            handle.seek(size - budget)
        return handle.read(budget)


def _iter_json_lines(chunk: bytes) -> "Iterator[dict[str, Any]]":
    """Yield parsed JSON objects from complete newline-terminated lines.

    A partial final line (no trailing newline) is dropped — never parsed
    mid-write. Only ``dict`` records are yielded.
    """
    newline = chunk.rfind(b"\n")
    if newline < 0:
        return
    for raw in chunk[: newline + 1].split(b"\n"):
        text = raw.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            yield record


# --- Claude adapter (sidecar → [L], with decay to [S]) ----------------------


def observe_claude(
    sidecar_path: Path,
    *,
    generation: Any = None,
    now_ns: Optional[int] = None,
    last_accepted_event_ns: Optional[int] = None,
    exited: bool = False,
) -> ProviderObservation:
    """Read one Claude ``observe/<terminal_id>.json`` sidecar (D3).

    The sidecar is a tiny JSON object written atomically by the emitter:
    ``{terminal_id, claude_session_id, model, effort, event_time}``. This is the
    only SELECTED (``[L]``) source. Ordering is by ``event_time`` (S2/D3): a
    sidecar whose ``event_time`` is not greater than ``last_accepted_event_ns``
    is rejected (stale, kept ``[S]``). Decay (D1/S6): an ``[L]`` value older than
    :data:`DECAY_NS` at read time becomes ``[S]``. An exited/reaped terminal
    shows its last value ``[S]`` with detail "exited" (D1) — the sidecar
    deliberately OUTLIVES the terminal (S3), so this still resolves.
    """
    now = time.time_ns() if now_ns is None else now_ns
    key = _stat_key(sidecar_path, generation)
    cache_id = f"claude:{sidecar_path}"
    if key is None:
        # No sidecar yet (existing terminal / pre-relaunch, D7): honest unknown.
        return ProviderObservation.unknown(validity="no observation yet")

    def _decay(value: Optional[str], event_ns: Optional[int]) -> Observation:
        if value is None:
            return Observation.unknown()
        if (
            last_accepted_event_ns is not None
            and event_ns is not None
            and (event_ns <= last_accepted_event_ns)
        ):
            return Observation(
                value=value,
                marker=Marker.STALE,
                kind=ObservationKind.SELECTED,
                event_time_ns=event_ns,
                source="claude_statusline",
                validity="superseded",
            )
        if exited:
            return Observation(
                value=value,
                marker=Marker.STALE,
                kind=ObservationKind.SELECTED,
                event_time_ns=event_ns,
                source="claude_statusline",
                validity="exited",
            )
        if event_ns is not None and (now - event_ns) > DECAY_NS:
            age_s = (now - event_ns) / 1e9
            return Observation(
                value=value,
                marker=Marker.STALE,
                kind=ObservationKind.SELECTED,
                event_time_ns=event_ns,
                source="claude_statusline",
                validity=f"no fresh sidecar for {age_s:.1f}s",
            )
        return Observation(
            value=value,
            marker=Marker.LIVE,
            kind=ObservationKind.SELECTED,
            event_time_ns=event_ns,
            source="claude_statusline",
        )

    # The sidecar is tiny; a bounded read is trivially within budget.
    try:
        raw = _read_tail(sidecar_path, TAIL_BUDGET_BYTES)
        record = json.loads(raw.decode("utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ProviderObservation.unknown(validity="sidecar unreadable")
    if not isinstance(record, dict):
        return ProviderObservation.unknown(validity="sidecar malformed")

    event_ns = record.get("event_time")
    event_ns = event_ns if isinstance(event_ns, int) else None
    model_val = record.get("model")
    effort_val = record.get("effort")
    result = ProviderObservation(
        model=_decay(model_val if isinstance(model_val, str) else None, event_ns),
        effort=_decay(effort_val if isinstance(effort_val, str) else None, event_ns),
    )
    _store(cache_id, key, result)
    return result


# --- codex adapter (incremental forward scan → [R] / [L]) -------------------


def _codex_scan_chunk(cursor: _CodexCursor, chunk: bytes, base_offset: int) -> None:
    """Fold complete records from ``chunk`` into ``cursor``, newest wins.

    ``turn_context`` → model/effort, kind REQUEST (``[R]``).
    ``event_msg`` ``thread_settings_applied`` → nested ``thread_settings``,
    kind SELECTED (``[L]``) — a settings CHANGE is the CLI's own selection edge.
    Only the allowlisted scalars are copied; the record timestamp becomes the
    event time (converted to ns).
    """
    for record in _iter_json_lines(chunk):
        rtype = record.get("type")
        ts_ns = _codex_ts_ns(record.get("timestamp"))
        if rtype == "turn_context":
            payload = record.get("payload")
            payload = payload if isinstance(payload, dict) else record
            model = payload.get("model")
            effort = payload.get("effort")
            if isinstance(model, str):
                cursor.model = model
            if isinstance(effort, str):
                cursor.effort = effort
            cursor.kind = ObservationKind.REQUEST
            cursor.event_time_ns = ts_ns
        elif rtype == "event_msg":
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "thread_settings_applied":
                settings = payload.get("thread_settings")
                if isinstance(settings, dict):
                    model = settings.get("model")
                    effort = settings.get("effort") or settings.get("reasoning_effort")
                    if isinstance(model, str):
                        cursor.model = model
                    if isinstance(effort, str):
                        cursor.effort = effort
                    cursor.kind = ObservationKind.SELECTED
                    cursor.event_time_ns = ts_ns


def _codex_ts_ns(timestamp: Any) -> Optional[int]:
    """Convert a codex ISO-8601 rollout timestamp to ns, best-effort."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        from datetime import datetime

        text = timestamp.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1e9)
    except (ValueError, OverflowError):
        return None


def observe_codex(
    rollout_path: Path,
    *,
    generation: Any = None,
) -> ProviderObservation:
    """Observe codex model/effort by INCREMENTAL FORWARD SCAN (D4/D6 amended).

    codex writes ``turn_context`` (and ``thread_settings_applied``) near the
    session HEAD, not per turn (evidence: 2/5 recent rollouts had the last
    signal within 64 KB of EOF). So an EOF tail is wrong here. Instead:

    * First observation of a path: one streaming pass over the file recording
      the LAST ``turn_context`` / ``thread_settings_applied``. Capped at
      :data:`CODEX_FIRST_PASS_MAX_BYTES`; beyond the cap, scan head
      :data:`CODEX_HEAD_BYTES` + tail :data:`CODEX_TAIL_BYTES` and render
      ``[?]`` if no signal found.
    * Subsequent polls: mtime_ns+size cache decides staleness; on change read
      ONLY ``[cursor.offset, EOF)`` capped at
      :data:`CODEX_INCREMENTAL_MAX_BYTES`, carrying any remainder forward.

    Precedence: ``thread_settings_applied`` → ``[L]``; ``turn_context`` → ``[R]``.
    """
    key = _stat_key(rollout_path, generation)
    cache_id = f"codex:{rollout_path}"
    if key is None:
        return ProviderObservation.unknown(validity="no rollout yet")
    cached = _cached(cache_id, key)
    if cached is not None:
        return cached

    with _lock:
        cursor = _codex_cursors.get(str(rollout_path))
        if cursor is None:
            cursor = _CodexCursor()
            _codex_cursors[str(rollout_path)] = cursor
        result = _codex_advance(rollout_path, cursor)
    _store(cache_id, key, result)
    return result


def _codex_advance(path: Path, cursor: _CodexCursor) -> ProviderObservation:
    """Advance ``cursor`` over new bytes and project. Caller holds ``_lock``.

    Three cases:

    * **Rotation / truncation** (inode change or size below the consumed
      offset): a different file at the same path — reset and rescan from 0.
    * **Same-size in-place rewrite** (B2 r1): mtime advanced but size did NOT
      grow past the consumed offset. The cursor is already at EOF, so an
      append-only reader would read nothing and reproject stale values. Reset
      and rescan the whole bound file.
    * **First observation** (not seeded): stream consecutive windows (each read
      capped at :data:`CODEX_INCREMENTAL_MAX_BYTES`) all the way to EOF (B1 r1),
      up to the 16 MB first-pass ceiling; beyond the ceiling, head + tail only.
    * **Ordinary incremental poll**: read only ``[offset, EOF)`` capped at 1 MB,
      carrying any remainder forward to the next poll.
    """
    try:
        st = path.stat()
    except OSError:
        return _codex_project(cursor)

    rotated = cursor.inode is not None and (cursor.inode != st.st_ino or st.st_size < cursor.offset)
    # B2: a same-size (or grow-less) in-place rewrite advances mtime without
    # moving EOF past what we already consumed. Detect it by mtime change with
    # no append growth, and force a full rescan rather than reading zero bytes.
    rewritten = (
        not rotated
        and cursor.seeded
        and cursor.mtime_ns is not None
        and st.st_mtime_ns != cursor.mtime_ns
        and st.st_size <= cursor.offset
    )
    if rotated or rewritten:
        cursor.offset = 0
        cursor.model = cursor.effort = cursor.event_time_ns = None
        cursor.over_cap = False
        cursor.seeded = False
    cursor.inode = st.st_ino

    if not cursor.seeded:
        cursor.seeded = True
        cursor.offset = 0
        if st.st_size > CODEX_FIRST_PASS_MAX_BYTES:
            # Oversized: head + tail windows only (ruling).
            cursor.over_cap = True
            _codex_read_range(path, cursor, 0, CODEX_HEAD_BYTES)
            tail_start = max(CODEX_HEAD_BYTES, st.st_size - CODEX_TAIL_BYTES)
            _codex_read_range(path, cursor, tail_start, st.st_size)
            cursor.offset = st.st_size
            cursor.size = st.st_size
            cursor.mtime_ns = st.st_mtime_ns
            return _codex_project(cursor)
        # First full pass (B1): stream consecutive <=1 MB windows to EOF, not a
        # single window. Each _codex_read_range consumes up to its last newline;
        # the loop guards against a window that consumes nothing (a record
        # straddling the cap boundary) by advancing past it to keep moving.
        _codex_scan_forward_to_eof(path, cursor, st.st_size)
        cursor.size = st.st_size
        cursor.mtime_ns = st.st_mtime_ns
        return _codex_project(cursor)

    # Ordinary incremental poll: read only [offset, min(EOF, offset+cap)).
    if st.st_size > cursor.offset:
        end = min(st.st_size, cursor.offset + CODEX_INCREMENTAL_MAX_BYTES)
        consumed = _codex_read_range(path, cursor, cursor.offset, end)
        cursor.offset += consumed
    cursor.size = st.st_size
    cursor.mtime_ns = st.st_mtime_ns
    return _codex_project(cursor)


def _codex_scan_forward_to_eof(path: Path, cursor: _CodexCursor, eof: int) -> None:
    """Stream consecutive <=1 MB windows from ``cursor.offset`` to ``eof`` (B1).

    Completes the promised first-pass scan instead of stopping after one window.
    Each window consumes up to its last newline; a window that consumes nothing
    (a single record longer than the cap, already skipped inside
    ``_codex_read_range`` when it returns the full cap) still advances the
    offset, so the loop always terminates.
    """
    guard = 0
    max_windows = (CODEX_FIRST_PASS_MAX_BYTES // CODEX_INCREMENTAL_MAX_BYTES) + 2
    while cursor.offset < eof and guard < max_windows:
        guard += 1
        end = min(eof, cursor.offset + CODEX_INCREMENTAL_MAX_BYTES)
        consumed = _codex_read_range(path, cursor, cursor.offset, end)
        if consumed <= 0:
            # No complete line in this window and it is not a full-cap oversized
            # record (that path returns the cap): the tail of the window is a
            # partial line. Nothing more to do on the first pass — stop; the
            # ordinary incremental poll reads it whole once it is newline-
            # terminated.
            break
        cursor.offset += consumed


def _codex_read_range(path: Path, cursor: _CodexCursor, start: int, end: int) -> int:
    """Read ``[start, end)``, scan complete lines into cursor. Returns bytes consumed.

    Consumes only up to the last newline so a record caught mid-write is read
    whole next time; returns the count of bytes actually consumed (which may be
    less than ``end - start`` when the window ends mid-line).
    """
    if end <= start:
        return 0
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            chunk = handle.read(end - start)
    except OSError:
        return 0
    newline = chunk.rfind(b"\n")
    if newline < 0:
        # No complete line in this window. If the window is the full cap, skip
        # it to keep the scan moving (an oversized single record); else wait.
        if len(chunk) >= CODEX_INCREMENTAL_MAX_BYTES:
            return len(chunk)
        return 0
    _codex_scan_chunk(cursor, chunk[: newline + 1], start)
    return newline + 1


def _codex_project(cursor: _CodexCursor) -> ProviderObservation:
    if cursor.model is None and cursor.effort is None:
        validity = "no turn_context within scan budget" if cursor.over_cap else "no observation yet"
        return ProviderObservation.unknown(validity=validity)
    marker = Marker.LIVE if cursor.kind is ObservationKind.SELECTED else Marker.OBSERVED
    src = "codex_rollout"

    def _obs(value: Optional[str]) -> Observation:
        if value is None:
            return Observation.unknown()
        return Observation(
            value=value,
            marker=marker,
            kind=cursor.kind,
            event_time_ns=cursor.event_time_ns,
            source=src,
        )

    return ProviderObservation(model=_obs(cursor.model), effort=_obs(cursor.effort))


# --- pi adapter (tail → [R]) ------------------------------------------------


def observe_pi(
    session_path: Path,
    *,
    generation: Any = None,
) -> ProviderObservation:
    """Observe pi model/effort from the last assistant entry in the session JSONL.

    pi appends an assistant record per response carrying ``message.model`` (and
    ``message.responseModel``) and, on a thinking model, ``message.thinkingLevel``.
    The last assistant record sits near EOF, so a bounded 64 KB tail resolves it
    (contrast codex). Marker ``[R]`` kind RESPONSE. A touched/appended-unrelated
    file never advances the event age (the record's OWN timestamp is the event
    time) and never relabels to ``[L]`` (AC2).
    """
    key = _stat_key(session_path, generation)
    cache_id = f"pi:{session_path}"
    if key is None:
        return ProviderObservation.unknown(validity="no session file")
    cached = _cached(cache_id, key)
    if cached is not None:
        return cached

    try:
        chunk = _read_tail(session_path, TAIL_BUDGET_BYTES)
    except OSError:
        return ProviderObservation.unknown(validity="session unreadable")

    model: Optional[str] = None
    effort: Optional[str] = None
    event_ns: Optional[int] = None
    for record in _iter_json_lines(chunk):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        m = message.get("model") or message.get("responseModel")
        if isinstance(m, str):
            model = m
        level = message.get("thinkingLevel")
        effort = level if isinstance(level, str) and level else None
        ts = record.get("timestamp") or message.get("timestamp")
        event_ns = _pi_ts_ns(ts)
        # Do not break: keep scanning so the LAST assistant record in the tail
        # wins (records are newest-last within the tail window).

    if model is None and effort is None:
        result = ProviderObservation.unknown(validity="no assistant record in tail")
    else:
        result = ProviderObservation(
            model=_pi_obs(model, event_ns),
            effort=(
                _pi_obs(effort, event_ns)
                if effort is not None
                else Observation.unknown(validity="thinkingLevel absent")
            ),
        )
    _store(cache_id, key, result)
    return result


def _pi_obs(value: Optional[str], event_ns: Optional[int]) -> Observation:
    if value is None:
        return Observation.unknown()
    return Observation(
        value=value,
        marker=Marker.OBSERVED,
        kind=ObservationKind.RESPONSE,
        event_time_ns=event_ns,
        source="pi_session",
    )


def _pi_ts_ns(timestamp: Any) -> Optional[int]:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1e9)
    except (ValueError, OverflowError):
        return None


# --- kiro adapter (checkpoint → model [R], effort [C]) ----------------------


def observe_kiro(
    session_path: Path,
    *,
    generation: Any = None,
) -> ProviderObservation:
    """Observe kiro MODEL from the session checkpoint (D5); effort is ``[C]``.

    Model comes from ``session_state.rts_model_state.model_info.model_id`` (the
    observed ``auto`` renders as the alias it is — D5). There is no runtime
    effort key, so effort is left UNKNOWN here and the projector renders the
    CONFIGURED value ``[C]`` with detail "runtime effort unavailable from this
    provider" (AC3). Never infer effort from model/tokens; never scrape the pane.

    The whole session JSON is read (it is small — a checkpoint, not a
    transcript), bounded by the tail budget as a backstop; kind CHECKPOINT.
    """
    key = _stat_key(session_path, generation)
    cache_id = f"kiro:{session_path}"
    if key is None:
        return ProviderObservation.unknown(validity="no session file")
    cached = _cached(cache_id, key)
    if cached is not None:
        return cached

    try:
        with open(session_path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            # A checkpoint is small; cap at a generous multiple of the tail
            # budget so a pathological file cannot blow the read budget.
            data = handle.read(min(size, TAIL_BUDGET_BYTES * 16))
        record = json.loads(data.decode("utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ProviderObservation.unknown(validity="session unreadable")

    model_id: Optional[str] = None
    if isinstance(record, dict):
        ss = record.get("session_state")
        if isinstance(ss, dict):
            rts = ss.get("rts_model_state")
            if isinstance(rts, dict):
                info = rts.get("model_info")
                if isinstance(info, dict):
                    mid = info.get("model_id") or info.get("model_name")
                    if isinstance(mid, str):
                        model_id = mid

    model_obs = (
        Observation(
            value=model_id,
            marker=Marker.OBSERVED,
            kind=ObservationKind.CHECKPOINT,
            source="kiro_session",
        )
        if model_id is not None
        else Observation.unknown(validity="model checkpoint absent")
    )
    # Effort: UNKNOWN here; projector supplies the CONFIGURED [C] value (AC3).
    result = ProviderObservation(
        model=model_obs,
        effort=Observation.unknown(validity="runtime effort unavailable from this provider"),
    )
    _store(cache_id, key, result)
    return result


# --- dispatch ---------------------------------------------------------------

#: Maps CAO provider ids to their adapters. Unknown providers → unknown.
_ADAPTERS: dict[str, Callable[..., ProviderObservation]] = {
    "claude_code": observe_claude,
    "codex": observe_codex,
    "pi_cli": observe_pi,
    "kiro_cli": observe_kiro,
}


def observe_provider(
    provider: str,
    path: Path,
    *,
    generation: Any = None,
    **kwargs: Any,
) -> ProviderObservation:
    """Dispatch to the adapter for ``provider`` over the bound ``path`` (D2).

    Never raises — an unknown provider or a failing adapter returns an unknown
    observation, so one bad source never blocks the fleet (D6/AC5). Identity
    binding is the CALLER's: it resolves ``path`` from the terminal's recorded
    session/rollout binding and passes it in; this function never scans a
    directory for the "newest file".
    """
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        return ProviderObservation.unknown(validity="provider not observable")
    try:
        return adapter(path, generation=generation, **kwargs)
    except Exception:
        return ProviderObservation.unknown(validity="observation failed")
