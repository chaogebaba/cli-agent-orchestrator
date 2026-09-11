"""F970 (#819) — publish the teed turn stream to cao-server's relay.

Step 1 gave the runner the send SSE; this is the wire that carries it out of
the worker subprocess so a human or another agent can watch the turn live
instead of waiting ~35 s for a finished document. The server side is
``services/chatgpt_turn_stream`` plus the two routes in ``api/main``.

Three properties are deliberate:

* **The relay never slows or fails a turn.** Events are queued to a bounded
  deque and flushed by a daemon thread; a full queue DROPS (and counts) rather
  than blocking the browser driver, and every HTTP error is swallowed after a
  debug log. A watcher is a convenience — the turn's correctness lives in the
  conversation GET and the published report, neither of which touches this.
* **Batching, not per-token requests.** A thinking turn emits hundreds of token
  events; each flush sends what accumulated over ``flush_interval`` in one POST.
* **Identity is the worker's own.** The POST carries the worker's
  ``X-CAO-Terminal-Token``, which is what the ingest route binds on, so a turn
  stream cannot be forged under a worker's name by anything that merely holds a
  write scope.

``post`` is injectable so the tests drive the whole queue/flush/terminal path
with no server and no network.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Bounded outbound queue. A slow or dead server must cost the turn nothing.
MAX_QUEUED_EVENTS = 512

#: How long the flusher accumulates before a POST.
DEFAULT_FLUSH_INTERVAL_S = 0.4

#: Must match the ingest route's batch cap.
MAX_BATCH = 200

#: Terminal kinds — publishing one closes every follower's stream.
KIND_TURN_STARTED = "turn_started"
KIND_TURN_FINISHED = "turn_finished"
KIND_TURN_FAILED = "turn_failed"


def _default_post(url: str, token: str, body: Dict[str, Any], timeout: float) -> None:
    import requests

    headers = {"X-CAO-Terminal-Token": token} if token else {}
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()


class TurnRelay:
    """Best-effort, batched publisher for one turn's progress events."""

    def __init__(
        self,
        *,
        endpoint: str,
        terminal_id: str,
        token: str,
        turn_id: str,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        post: Optional[Callable[[str, str, Dict[str, Any], float], None]] = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.url = f"{endpoint.rstrip('/')}/terminals/{terminal_id}/chatgpt/turns/{turn_id}/events"
        self.terminal_id = terminal_id
        self.token = token
        self.turn_id = turn_id
        self.flush_interval_s = flush_interval_s
        self.timeout_s = timeout_s
        self._post = post or _default_post
        self._queue: Deque[Dict[str, Any]] = deque(maxlen=MAX_QUEUED_EVENTS)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0
        self.posted = 0
        self.post_failures = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "TurnRelay":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name=f"cao-turn-relay-{self.turn_id[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop the flusher after draining what is queued."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
        self._thread = None
        self.flush_once()

    # ── producing ────────────────────────────────────────────────────────

    def enqueue(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Queue one event. Never raises; drops (and counts) when full."""
        if not kind:
            return
        with self._lock:
            if len(self._queue) >= MAX_QUEUED_EVENTS:
                self.dropped += 1
                return
            self._queue.append({"kind": kind, "payload": payload or {}})

    def publish_stream_event(self, event: Any) -> None:
        """Sink for ``Transport.arm_sse_tee(on_event=...)``.

        Accepts a ``sse_stream.StreamEvent`` (or anything with ``to_payload``)
        and relays it under its own kind, so a follower sees ``event: token`` /
        ``event: quota`` / ``event: status`` frames rather than one opaque kind.
        """
        try:
            payload = event.to_payload() if hasattr(event, "to_payload") else dict(event)
            kind = str(payload.pop("kind", "") or "stream")
        except Exception:  # pragma: no cover - a malformed event must not raise
            return
        self.enqueue(kind, payload)

    def started(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.enqueue(KIND_TURN_STARTED, payload)

    def finished(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.enqueue(KIND_TURN_FINISHED, self._with_counters(payload))

    def failed(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.enqueue(KIND_TURN_FAILED, self._with_counters(payload))

    def _with_counters(self, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Terminal events carry the relay's own drop count, so a follower can
        tell "the model said nothing" from "the relay dropped it"."""
        out = dict(payload or {})
        out["relay"] = {
            "dropped": self.dropped,
            "posted": self.posted,
            "post_failures": self.post_failures,
        }
        return out

    # ── flushing ─────────────────────────────────────────────────────────

    def _drain(self) -> List[Dict[str, Any]]:
        with self._lock:
            batch = list(self._queue)[:MAX_BATCH]
            for _ in range(len(batch)):
                self._queue.popleft()
        return batch

    def flush_once(self) -> int:
        """POST one batch; returns how many events were sent (0 when idle)."""
        batch = self._drain()
        if not batch:
            return 0
        try:
            self._post(self.url, self.token, {"events": batch}, self.timeout_s)
            self.posted += len(batch)
            return len(batch)
        except Exception as exc:
            self.post_failures += 1
            logger.debug("chatgpt turn relay POST failed: %s", type(exc).__name__)
            return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.flush_interval_s)
            while self.flush_once() >= MAX_BATCH:
                pass
        # Drain whatever arrived during shutdown.
        while self.flush_once():
            pass


def relay_from_env(turn_id: str, **kwargs: Any) -> Optional[TurnRelay]:
    """Build a started relay from the worker's pane env, or None.

    Returns None when the process is not a CAO worker (no endpoint/terminal/
    token) or when ``CAO_CHATGPT_TURN_RELAY=0`` disables it — in which case the
    turn runs exactly as it did before F970.
    """
    if os.environ.get("CAO_CHATGPT_TURN_RELAY", "1") in ("0", "false", "no"):
        return None
    endpoint = os.environ.get("CAO_ENDPOINT", "http://127.0.0.1:8990")
    terminal_id = os.environ.get("CAO_TERMINAL_ID", "")
    token = os.environ.get("CAO_TERMINAL_TOKEN", "")
    if not endpoint or not terminal_id or not token:
        return None
    return TurnRelay(
        endpoint=endpoint, terminal_id=terminal_id, token=token, turn_id=turn_id, **kwargs
    ).start()
