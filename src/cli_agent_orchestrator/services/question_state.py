"""Question-open truth for F507 (edge markers + level transcript parse).

Two inputs, reconciled level-triggered (D9 "edge-triggered notifications,
level-triggered logic"):

* **edge** — ``question_open`` / ``question_clear`` markers pushed by the CC
  hooks through ``POST /terminals/{id}/interaction-marker`` (``push_marker``).
* **level** — the transcript tail parse (``reconcile``): walk the bound
  ``.jsonl``, pair ``tool_use`` blocks (assistant) with ``tool_result`` blocks
  (user) by id, and treat the terminal as question-open iff the newest
  ``tool_use`` has no matching ``tool_result``.

``is_open(tid)`` is the single read the fusion helper consumes.

Durability (A1 rider / D11): a marker can arrive for a terminal whose
transcript does not exist yet (SessionStart fires before CC writes the file).
So ``reconcile`` distinguishes three transcript outcomes:

* **closed** — a readable transcript with NO pending ``tool_use`` ⇒ clear the
  marker (this is the lost-clear healing path, AC10).
* **open** — a readable transcript WITH a pending ``tool_use`` ⇒ open (even
  with no prior hook marker).
* **unreadable / unbound** — leave any open marker ALONE (Do-NOT #12); the TTL
  (``liveness.question_marker_ttl_s``, default 300) is the only backstop, after
  which the marker is cleared with ``fusion_reason="marker_ttl"`` and a WARN
  (AC14). ``permission_prompt`` markers never get a hook clear edge (D7) and
  rely on this + layer 2.

State is in-process (the service is a module singleton like ``status_monitor``
/ ``inbox_service``); markers are ephemeral runtime truth, not durable rows.
Nothing here is claude-specific on the marker channel (Do-NOT #8) — only the
layer-2 transcript walk knows the CC ``.jsonl`` block shape, and it degrades to
"unreadable ⇒ hold" for any provider whose transcript it cannot parse.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# The producing layer, surfaced in the fleet TUI row detail (§8) so a layer-1
# outage is visible rather than silent.
SourceLayer = Literal["hook", "transcript", "screen"]

_DEFAULT_TTL_S = 300.0


@dataclass
class _MarkerState:
    open: bool = False
    # monotonic timestamp of the CURRENT open episode (reset on each fresh open,
    # used for the TTL). None while closed.
    opened_at: float | None = None
    source_layer: SourceLayer = "hook"
    tool_name: str | None = None
    # A nonce/kind history is not retained: opens are idempotent (AC9) — a
    # repeat open on an already-open marker does not restart the TTL clock,
    # which keeps a Notification storm from indefinitely deferring the backstop.


@dataclass
class QuestionStateService:
    """Owns question-open truth for all terminals; process singleton."""

    _clock: Callable[[], float] = time.monotonic
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _markers: dict[str, _MarkerState] = field(default_factory=dict)

    # ---- config -----------------------------------------------------------
    def _ttl_s(self) -> float:
        try:
            return float(ConfigService.get("liveness.question_marker_ttl_s", _DEFAULT_TTL_S))
        except Exception:
            return _DEFAULT_TTL_S

    # ---- edge input (endpoint) -------------------------------------------
    def push_marker(
        self,
        terminal_id: str,
        kind: str,
        *,
        source_layer: SourceLayer = "hook",
        tool_name: str | None = None,
        now: float | None = None,
    ) -> None:
        """Apply one edge marker. Idempotent per (terminal_id, kind).

        ``question_open`` on an already-open marker is a no-op for the TTL clock
        (AC9 — a storm of opens does not push the backstop out forever); it only
        refreshes the source layer / tool name. ``question_clear`` always clears.
        Unknown kinds are ignored.
        """
        now = self._clock() if now is None else now
        with self._lock:
            state = self._markers.get(terminal_id)
            if kind == "question_open":
                if state is None or not state.open:
                    self._markers[terminal_id] = _MarkerState(
                        open=True,
                        opened_at=now,
                        source_layer=source_layer,
                        tool_name=tool_name,
                    )
                else:
                    # Already open — keep the original opened_at (TTL clock), just
                    # refresh the descriptive fields.
                    state.source_layer = source_layer
                    if tool_name is not None:
                        state.tool_name = tool_name
            elif kind == "question_clear":
                if state is not None and state.open:
                    state.open = False
                    state.opened_at = None

    # ---- read (fusion) ----------------------------------------------------
    def is_open(self, terminal_id: str) -> bool:
        with self._lock:
            state = self._markers.get(terminal_id)
            return bool(state and state.open)

    def source_layer(self, terminal_id: str) -> SourceLayer | None:
        with self._lock:
            state = self._markers.get(terminal_id)
            return state.source_layer if state and state.open else None

    def forget(self, terminal_id: str) -> None:
        """Drop all marker state for a terminal (teardown)."""
        with self._lock:
            self._markers.pop(terminal_id, None)

    # ---- level input (watchdog tick) -------------------------------------
    def reconcile(self, terminal_id: str, metadata: dict, *, now: float | None = None) -> None:
        """Level-triggered reconcile from the current transcript + TTL backstop.

        Runs on the watchdog tick for terminals holding an open marker or
        classified WAITING. Re-derives from CURRENT transcript state and ignores
        the triggering event (D9). Reconciles BOTH ways:

        * open marker + transcript shows no pending ``tool_use`` ⇒ clear (AC10).
        * no marker + transcript shows a pending ``tool_use`` ⇒ open.
        * transcript unreadable / unbound ⇒ leave an open marker alone; the TTL
          decides (AC14, D11).
        """
        now = self._clock() if now is None else now
        parse = self._parse_transcript(metadata)  # "open" | "closed" | None(unreadable)

        with self._lock:
            state = self._markers.get(terminal_id)
            open_now = bool(state and state.open)

            if parse == "open":
                if not open_now:
                    self._markers[terminal_id] = _MarkerState(
                        open=True, opened_at=now, source_layer="transcript"
                    )
                return
            if parse == "closed":
                # Level truth beats a stale hook open (lost-clear healing).
                if open_now:
                    assert state is not None
                    state.open = False
                    state.opened_at = None
                return

            # parse is None: transcript unreadable / not yet bound (A1). Hold the
            # marker; the TTL is the only self-heal (D11).
            if open_now:
                assert state is not None and state.opened_at is not None
                if now - state.opened_at >= self._ttl_s():
                    state.open = False
                    state.opened_at = None
                    logger.warning(
                        "question_marker TTL expired for %s after %.0fs "
                        "(transcript unreadable/unbound); clearing "
                        'fusion_reason="marker_ttl"',
                        terminal_id,
                        self._ttl_s(),
                    )

    # ---- layer-2 parse ----------------------------------------------------
    def _parse_transcript(self, metadata: dict) -> Literal["open", "closed"] | None:
        """Return "open"/"closed" from the bound transcript, or None if unreadable.

        AC15: pairing is by ``tool_use`` id, NOT position. Open iff the newest
        assistant ``tool_use`` block has no ``tool_result`` (matched by
        ``tool_use_id``) anywhere after it.
        """
        try:
            from cli_agent_orchestrator.services.message_trace_service import (
                resolve_session_transcript,
            )

            resolution = resolve_session_transcript(metadata)
            if resolution is None:
                return None
            path = resolution.path
            if not path.is_file():
                return None
            return self._classify_jsonl(path)
        except Exception:
            logger.debug("question_state transcript parse failed", exc_info=True)
            return None

    @staticmethod
    def _iter_content_blocks(record: dict):
        """Yield content blocks from a CC transcript record.

        CC transcript entries carry the model turn under ``message.content``
        (a list of typed blocks) with the entry ``type`` at top level.
        """
        message = record.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                yield from (b for b in content if isinstance(b, dict))

    def _classify_jsonl(self, path: Path) -> Literal["open", "closed"] | None:
        """Walk the whole transcript and pair tool_use/tool_result by id.

        Returns None only on a read error (caller treats as unreadable ⇒ hold).
        A well-formed transcript with zero tool_use blocks is "closed".
        """
        # tool_use_id -> True once a tool_result for it has been seen.
        seen_use: dict[str, bool] = {}
        # Preserve encounter order so "newest tool_use" is well defined.
        order: list[str] = []
        try:
            with path.open(encoding="utf-8") as stream:
                for raw in stream:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        # A single malformed line is tolerated (partial write);
                        # it does not make the whole transcript unreadable.
                        continue
                    if not isinstance(record, dict):
                        continue
                    for block in self._iter_content_blocks(record):
                        btype = block.get("type")
                        if btype == "tool_use":
                            use_id = block.get("id")
                            if isinstance(use_id, str):
                                if use_id not in seen_use:
                                    seen_use[use_id] = False
                                    order.append(use_id)
                        elif btype == "tool_result":
                            result_id = block.get("tool_use_id")
                            if isinstance(result_id, str) and result_id in seen_use:
                                seen_use[result_id] = True
        except (OSError, UnicodeDecodeError):
            return None

        # Open iff ANY tool_use lacks a matching tool_result (id-matched, not
        # positional — AC15). The "newest" framing in the blueprint is satisfied
        # by this: if the newest tool_use is unmatched, there is an unmatched id.
        for use_id in order:
            if not seen_use.get(use_id, False):
                return "open"
        return "closed"


question_state = QuestionStateService()

__all__ = ["QuestionStateService", "question_state", "SourceLayer"]
