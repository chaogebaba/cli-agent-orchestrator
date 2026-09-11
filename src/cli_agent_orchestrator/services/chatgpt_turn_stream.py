"""F970 (#819) — per-turn relay of the ChatGPT-web send SSE, with seq + replay.

The runner (`chatgpt_web_runner`) now consumes the app's own
``text/event-stream`` send response instead of discarding it (step 1). That
stream lives inside a worker subprocess driving a headful browser, which is the
one place nobody can watch. This store is the relay point: the worker POSTs its
allow-listed progress events to cao-server, and any number of readers follow
them over CAO's own SSE surface, live, with replay.

**Why a new store rather than the fleet bus.** ``sse_bus`` is live pub/sub with
no replay and no per-subject addressing, and ``event_log_service`` is one global
500-row ring for fleet governance events — a chatty token stream would evict the
fleet timeline within seconds. Turn streams are per-subject, high-rate and
short-lived, so they get their own bounded per-turn ring. Turn LIFECYCLE events
(start, quota, end) are additionally mirrored onto the fleet bus by the API
layer, which is the cheap half of "watch it on the fleet stream".

**Ordering and replay are the point.** Every event carries a per-turn monotonic
``seq``, emitted as the SSE ``id:``, so a native ``EventSource`` reconnect
resumes exactly after the last delivered event (``Last-Event-ID``), and a
programmatic reader can pass ``?after_seq=``. A reader that fell behind the ring
is told so with an explicit ``gap`` frame rather than being handed a silently
discontiguous stream — the same rule the workflow-run stream follows.

**Content boundary.** Payloads are whatever the worker publishes, and the worker
publishes the ``sse_stream`` allow-list: token text, status/model fields, and
the ``limits_progress`` quota. This module additionally refuses payload keys
that are credential-shaped, so a future publisher bug cannot turn the relay into
a token exfiltration path.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

#: Per-turn ring capacity. A long thinking turn emits a few hundred token
#: events; 2000 keeps a whole turn addressable while bounding one turn's memory.
TURN_RING_CAPACITY = 2000

#: How many turns are retained at once (LRU by last write). A worker runs one
#: turn at a time and the cap is 14 workers, so 64 is generous.
MAX_TURNS = 64

#: Turns untouched for this long are swept on the next write/read.
TURN_TTL = timedelta(hours=2)

#: Kinds that END a turn: after one of these is delivered, a follower's stream
#: closes instead of hanging on a turn that can never produce another event.
TERMINAL_KINDS: frozenset[str] = frozenset({"turn_finished", "turn_failed"})

#: Kinds mirrored to the fleet bus (never token text — that is the whole point
#: of keeping the two surfaces separate).
LIFECYCLE_KINDS: frozenset[str] = frozenset(
    {"turn_started", "quota", "turn_finished", "turn_failed"}
)

#: A payload key naming any of these is dropped. Same fail-closed posture as
#: ``sse_stream``: the relay is not a place to discover a new token field.
_FORBIDDEN_KEY_RE = re.compile(
    r"token|authorization|cookie|sentinel|turnstile|credential|secret|bearer", re.IGNORECASE
)

#: A turn id is an opaque, URL-safe handle minted by the runner (the run id).
TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TurnEvent:
    """One relayed event. ``seq`` is per-turn and monotonic from 1."""

    seq: int
    turn_id: str
    kind: str
    ts: str
    terminal_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "ts": self.ts,
            "terminal_id": self.terminal_id,
            **self.payload,
        }


@dataclass(frozen=True)
class Gap:
    """A declared hole: the reader's cursor is older than the ring retains."""

    after_seq: int
    before_seq: int
    missing_count: int
    reason: str = "ring_evicted"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "after_seq": self.after_seq,
            "before_seq": self.before_seq,
            "missing_count": self.missing_count,
            "reason": self.reason,
        }


def sanitize_payload(payload: Any) -> Dict[str, Any]:
    """Drop credential-shaped keys, recursively. Non-dict input becomes ``{}``."""
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or _FORBIDDEN_KEY_RE.search(key):
            continue
        if isinstance(value, dict):
            out[key] = sanitize_payload(value)
        elif isinstance(value, list):
            out[key] = [sanitize_payload(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


class _Turn:
    def __init__(self, turn_id: str, terminal_id: str) -> None:
        self.turn_id = turn_id
        self.terminal_id = terminal_id
        self.events: Deque[TurnEvent] = deque(maxlen=TURN_RING_CAPACITY)
        self.next_seq = 1
        self.ended = False
        self.created_at = _utcnow()
        self.updated_at = self.created_at


class TurnStreamStore:
    """Thread-safe, bounded, per-turn event rings with seq + replay.

    Writes come from the worker's HTTP POST (any thread); reads come from the
    SSE follow loop on the event loop. One lock guards everything; critical
    sections are O(1) appends and short list copies.
    """

    def __init__(self) -> None:
        self._turns: "OrderedDict[str, _Turn]" = OrderedDict()
        self._lock = threading.Lock()

    # ── writes ───────────────────────────────────────────────────────────

    def append(
        self,
        turn_id: str,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        terminal_id: str = "",
    ) -> TurnEvent:
        """Append one event and return it (with its assigned ``seq``)."""
        clean = sanitize_payload(payload or {})
        with self._lock:
            self._sweep_locked()
            turn = self._turns.get(turn_id)
            if turn is None:
                turn = _Turn(turn_id, terminal_id)
                self._turns[turn_id] = turn
                if len(self._turns) > MAX_TURNS:
                    self._turns.popitem(last=False)
            elif terminal_id and not turn.terminal_id:
                turn.terminal_id = terminal_id
            event = TurnEvent(
                seq=turn.next_seq,
                turn_id=turn_id,
                kind=kind,
                ts=_utcnow().isoformat(),
                terminal_id=turn.terminal_id,
                payload=clean,
            )
            turn.next_seq += 1
            turn.events.append(event)
            turn.updated_at = _utcnow()
            if kind in TERMINAL_KINDS:
                turn.ended = True
            self._turns.move_to_end(turn_id)
            return event

    # ── reads ────────────────────────────────────────────────────────────

    def read_after(
        self, turn_id: str, after_seq: Optional[int]
    ) -> Tuple[List[TurnEvent], Optional[Gap]]:
        """Return events with ``seq > after_seq``, plus a declared gap if the
        cursor is older than the ring still holds."""
        cursor = after_seq or 0
        with self._lock:
            self._sweep_locked()
            turn = self._turns.get(turn_id)
            if turn is None:
                return [], None
            events = [e for e in turn.events if e.seq > cursor]
            gap: Optional[Gap] = None
            if turn.events:
                oldest = turn.events[0].seq
                if cursor + 1 < oldest:
                    gap = Gap(
                        after_seq=cursor,
                        before_seq=oldest,
                        missing_count=oldest - cursor - 1,
                    )
            return events, gap

    def is_ended(self, turn_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(turn_id)
            return bool(turn and turn.ended)

    def exists(self, turn_id: str) -> bool:
        with self._lock:
            return turn_id in self._turns

    def snapshot(self, turn_id: str) -> Dict[str, Any]:
        """A JSON summary of one turn (for the non-streaming read)."""
        events, gap = self.read_after(turn_id, None)
        with self._lock:
            turn = self._turns.get(turn_id)
            ended = bool(turn and turn.ended)
            terminal_id = turn.terminal_id if turn else ""
            known = turn is not None
        return {
            "turn_id": turn_id,
            "known": known,
            "ended": ended,
            "terminal_id": terminal_id,
            "events": [e.to_dict() for e in events],
            "gap": gap.to_dict() if gap else None,
        }

    def turn_ids(self) -> List[str]:
        with self._lock:
            return list(self._turns.keys())

    # ── housekeeping ─────────────────────────────────────────────────────

    def _sweep_locked(self) -> None:
        cutoff = _utcnow() - TURN_TTL
        stale = [tid for tid, t in self._turns.items() if t.updated_at < cutoff]
        for tid in stale:
            self._turns.pop(tid, None)


_store: Optional[TurnStreamStore] = None
_store_lock = threading.Lock()


def get_turn_streams() -> TurnStreamStore:
    """Process-wide singleton store (lazily created)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = TurnStreamStore()
    return _store


def reset_turn_streams() -> None:
    """Drop the singleton (tests)."""
    global _store
    with _store_lock:
        _store = None


def sse_frame(event: TurnEvent) -> str:
    """Serialize one event as a named SSE frame carrying its seq as ``id:``."""
    import json

    return f"event: {event.kind}\ndata: {json.dumps(event.to_dict())}\nid: {event.seq}\n\n"


def gap_frame(gap: Gap) -> str:
    """Serialize a declared gap. It owns no seq — the surrounding events do."""
    import json

    return f"event: gap\ndata: {json.dumps(gap.to_dict())}\n\n"
