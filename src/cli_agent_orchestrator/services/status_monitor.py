"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import asyncio
import hashlib
import logging
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, NotRequired, Optional, Tuple, TypedDict

from cli_agent_orchestrator.constants import (
    CAO_PYTE_STATUS,
    PYTE_QUIESCENCE_DELAY_S,
    STATE_BUFFER_MAX,
)
from cli_agent_orchestrator.kernel.receiver_state import (
    FreshnessProof,
    PassOutcome,
    ReceiverState,
    ReceiverStateStore,
    pass_outcome_for_source,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


class PaneIdentityProofFailure(RuntimeError):
    """Internal control flow for a fail-closed admission identity proof."""


class EmptyProbeCapture(RuntimeError):
    """The backend returned no usable rows for the final admission frame."""


ScreenProbeResult = Literal[
    "waiting_user_answer", "error", "processing", "completed", "idle", "unknown"
]
ScreenProbeSignalClass = Literal["waiting", "error", "progress", "completion", "chrome", "none"]
ScreenProbeFrameSource = Literal["incremental", "fresh_capture"]


class ScreenProbeGeometry(TypedDict):
    columns: int
    rows: int


ScreenProbeLawSignal = TypedDict(
    "ScreenProbeLawSignal",
    {
        "class": ScreenProbeSignalClass,
        "provider_signal": str | None,
        "row_index": int | None,
    },
)


class ScreenProbeMeta(TypedDict):
    probed_at: str
    geometry: ScreenProbeGeometry
    frame_rows_hash: str
    frame_source: ScreenProbeFrameSource
    result_status: ScreenProbeResult
    law_signal: ScreenProbeLawSignal
    identity_proof_failure: NotRequired[str]
    temporal_demotion: NotRequired["ScreenProbeTemporalDemotion"]
    transient_api_error: NotRequired[bool]
    idle_reason: NotRequired[str]
    injection_hazard: NotRequired[str]
    probe_failure: NotRequired[
        Literal["empty_capture", "malformed_meta", "provider_hook_exception"]
    ]


class ScreenProbeTemporalDemotion(TypedDict):
    frames: int
    multiset_sha256: str


def _frame_rows_hash(rows: List[str]) -> str:
    """SHA-256 over an unambiguous length-delimited UTF-8 row sequence."""
    digest = hashlib.sha256()
    for row in rows:
        encoded = row.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _corroborable_rows(result: object) -> tuple[str, ...]:
    signals = getattr(result, "signals", ())
    return tuple(
        signal.row_bytes
        for signal in signals
        if signal.signal_class == "progress"
        and signal.temporal_policy == "corroborable"
        and isinstance(signal.row_bytes, str)
    )


def _row_multiset_hash(rows: tuple[str, ...]) -> str:
    return _frame_rows_hash(sorted(rows, key=lambda row: row.encode("utf-8")))


# Statuses that represent a stable "ready" state — the agent has finished
# producing output and is waiting for further input. Once latched, the
# StatusMonitor will not regress to PROCESSING until ``notify_input_sent``
# is called (signalling that a new processing cycle is starting).
#
# Why: the event-driven pipeline derives status from a rolling 8KB buffer,
# and TUI redraws (cursor positioning, status-bar refreshes) routinely
# evict the idle/response markers that the per-provider get_status() relies
# on. That makes status flap rapidly between IDLE/COMPLETED and PROCESSING
# in the seconds following completion. Without stickiness, both
# wait_until_status (server-side) and the e2e tests' HTTP polling miss the
# brief "ready" windows and time out (PR #273 codex 60s init timeouts,
# completion-timeout failures).
_STICKY_READY_STATUSES = frozenset(
    {
        TerminalStatus.IDLE,
        TerminalStatus.COMPLETED,
        TerminalStatus.WAITING_USER_ANSWER,
        TerminalStatus.ERROR,
    }
)


@dataclass(frozen=True)
class BoundaryObservation:
    observation_epoch: str
    status: TerminalStatus
    status_gen: Optional[int]
    input_gen: int
    seq: int
    last_non_ready_seq: Optional[int]
    last_ready_seq: Optional[int]


class StatusMonitor:
    """Accumulates terminal output into rolling buffers and detects status changes."""

    def __init__(self):
        # Guards _buffers/_last_status/_allow_processing_revert. State is
        # touched from the asyncio consumer (_process_chunk), FastAPI's
        # threadpool (send_input → notify_input_sent, get_status), inbox
        # delivery worker threads, and cleanup_old_data's thread. Individual
        # dict ops are GIL-atomic, but the latch logic is a read-modify-write
        # sequence (read armed → decide transition → consume arm) that must
        # not interleave with notify_input_sent, or a freshly-armed gate can
        # be consumed by a decision taken against stale state.
        self._lock = threading.RLock()
        self._buffers: Dict[str, str] = {}
        self._last_status: Dict[str, TerminalStatus] = {}
        # Per-terminal flag: when True, the next provider-detected PROCESSING
        # is honored and stickiness reset. Set by notify_input_sent() whenever
        # external input is sent to the terminal (paste-bombed by send_input
        # or backend.send_keys via provider init). Without this, latched
        # IDLE/COMPLETED would freeze the terminal forever even when the
        # agent is genuinely processing new work.
        self._allow_processing_revert: Dict[str, bool] = {}
        self._input_gen: Dict[str, int] = {}
        self._processing_gen: Dict[str, int] = {}
        self._status_gen: Dict[str, int] = {}
        self._observation_epoch: Dict[str, str] = {}
        self._observation_seq: Dict[str, int] = {}
        self._last_non_ready_seq: Dict[str, int] = {}
        self._last_ready_seq: Dict[str, int] = {}
        # --- pyte rendered-screen detection state (only used when CAO_PYTE_STATUS
        # is on AND the provider opts in via supports_screen_detection) ---
        # Per-terminal pyte Screen+Stream that composites the raw byte stream
        # into a rendered viewport. Detection runs against the composited screen
        # on two edges only — rising (output resumed) and quiescence (output
        # stopped for PYTE_QUIESCENCE_DELAY_S) — never mid-burst, which is what
        # keeps status flap-free.
        self._screens: Dict[str, Tuple[object, object]] = {}
        self._bursting: Dict[str, bool] = {}
        # Monotonic per-terminal chunk generation. Quiescence detection runs in
        # a worker thread; if newer chunks arrive before it applies, its result
        # is stale and must not overwrite the newer screen/buffer state.
        self._chunk_seq: Dict[str, int] = {}
        # Advances exclusively when bytes enter through _process_chunk. Unlike
        # _chunk_seq, input notifications and reset bookkeeping never touch it.
        self._fifo_frame_seq: Dict[str, int] = {}
        # Pending quiescence-detect timer handle per terminal (loop.call_later).
        self._quiesce_handle: Dict[str, asyncio.TimerHandle] = {}
        # The event loop that owns the quiescence timers. Captured when the
        # first timer is scheduled (on the loop thread). clear_terminal /
        # reset_buffer can run OFF that thread (cleanup_old_data is dispatched
        # via asyncio.to_thread), and TimerHandle.cancel() is not thread-safe,
        # so the cancel is marshaled back onto this loop. See
        # _cancel_quiesce_handle.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Strong references to in-flight quiescence-detection tasks. asyncio only
        # keeps a WEAK reference to tasks created via loop.create_task, so without
        # this a detection task can be garbage-collected mid-run and silently drop
        # a status transition. Tasks remove themselves on completion.
        self._detect_tasks: set = set()
        self._screen_size_deferred_warned: set[str] = set()
        self._receiver_state_store = ReceiverStateStore()
        self._receiver_publish_last_logged: Dict[str, float] = {}

    @property
    def receiver_state_store(self) -> ReceiverStateStore:
        """Return this monitor's process-local observation store."""

        return self._receiver_state_store

    def _publish_observation(
        self,
        terminal_id: str,
        *,
        latched_status: TerminalStatus,
        pass_outcome: PassOutcome,
        frame_source: Literal["incremental", "fresh_capture"],
        metadata: dict[str, Any] | None = None,
        freshness_proof: FreshnessProof | None = None,
    ) -> None:
        """Build and publish one receiver observation. Caller holds ``_lock``."""

        if metadata is None:
            raise LookupError(f"terminal metadata unavailable for {terminal_id}")
        self._receiver_state_store.publish_observation(
            ReceiverState(
                terminal_id=terminal_id,
                lifecycle_generation=int(metadata["lifecycle_generation"]),
                window_identity=str(metadata["tmux_window"]),
                observation_epoch=self._epoch_locked(terminal_id),
                observation_sequence=self._observation_seq.get(terminal_id, 0),
                provider=str(metadata["provider"]),
                frame_source=frame_source,
                captured_at_mono=time.monotonic(),
                frame_hash=None,
                latched_status=latched_status,
                pass_outcome=pass_outcome,
                freshness_proof=freshness_proof or FreshnessProof("not_probed"),
            )
        )

    def _log_receiver_publish_failure(self, terminal_id: str) -> None:
        """Rate-limit hook-failure tracebacks without changing pass behavior."""

        now_mono = time.monotonic()
        last_logged = self._receiver_publish_last_logged.get(terminal_id)
        if last_logged is not None and now_mono - last_logged < 60.0:
            return
        self._receiver_publish_last_logged[terminal_id] = now_mono
        logger.exception("Failed to publish receiver observation for %s", terminal_id)

    def _bump_chunk_seq_locked(self, terminal_id: str) -> int:
        """Advance the terminal generation. Caller holds _lock."""
        chunk_seq = self._chunk_seq.get(terminal_id, 0) + 1
        self._chunk_seq[terminal_id] = chunk_seq
        return chunk_seq

    def _epoch_locked(self, terminal_id: str) -> str:
        return self._observation_epoch.setdefault(terminal_id, str(uuid.uuid4()))

    def _new_epoch_locked(self, terminal_id: str) -> None:
        self._observation_epoch[terminal_id] = str(uuid.uuid4())
        self._observation_seq[terminal_id] = 0
        self._last_non_ready_seq.pop(terminal_id, None)
        self._last_ready_seq.pop(terminal_id, None)

    def _observe_locked(self, terminal_id: str, status: TerminalStatus) -> int:
        self._epoch_locked(terminal_id)
        seq = self._observation_seq.get(terminal_id, 0) + 1
        self._observation_seq[terminal_id] = seq
        if status == TerminalStatus.PROCESSING:
            self._last_non_ready_seq[terminal_id] = seq
        elif status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
            self._last_ready_seq[terminal_id] = seq
        return seq

    async def run(self) -> None:
        """Subscribe to output events and detect status changes.

        ``_process_chunk`` runs provider status detection which, for tmux-backed
        providers, shells out to the ``tmux`` binary via libtmux (a blocking
        ``subprocess`` fork/exec — e.g. kiro's ``get_pane_current_command`` in
        Check 3). Running that inline on the event loop meant every output chunk
        from every worker forked tmux ON the loop; with a few concurrent workers
        streaming, that fork storm froze the whole server (no /health, assign
        POSTs stranded until the MCP client's ~120s timeout). Offload
        ``_process_chunk`` to a worker thread so the loop stays free.

        Chunks are processed one at a time (each ``to_thread`` is awaited before
        the next ``queue.get()``), so per-terminal ordering and the latch's
        read-modify-write sequence are preserved exactly as before.
        """
        # Capture the loop up front, on the loop thread, so the debounce timers
        # scheduled from the worker thread can be marshaled back onto it.
        self._loop = asyncio.get_running_loop()
        queue = bus.subscribe("terminal.*.output")
        logger.info("StatusMonitor started")

        while True:
            try:
                event = await queue.get()
                terminal_id = terminal_id_from_topic(event["topic"])
                await asyncio.to_thread(self._process_chunk, terminal_id, event["data"]["data"])
            except Exception as e:
                logger.exception(f"Error in StatusMonitor: {e}")

    def _process_chunk(self, terminal_id: str, chunk: str) -> None:
        """Append chunk to the rolling buffer and (re)detect status.

        Two detection paths share one latch/publish backend (_apply_detection):
        - RAW (default, every provider): regex over the rolling 8KB byte
          buffer, run on every chunk. Unchanged legacy behavior.
        - SCREEN (pyte): when CAO_PYTE_STATUS is on AND the provider opts in
          via supports_screen_detection, the chunk is fed to a per-terminal
          pyte screen and detection runs only on the rising edge (output
          resumed) and at quiescence (output stopped) — see
          _schedule_screen_detection.
        """
        provider = provider_manager.get_provider(terminal_id)
        use_screen = (
            CAO_PYTE_STATUS
            and provider is not None
            and getattr(provider, "supports_screen_detection", False)
        )

        # Resolve the pyte screen size BEFORE taking the lock: the lookup
        # shells out to tmux (fork/exec — see run()'s fork-storm note) and
        # only happens once per terminal lifetime (screen absent). If metadata
        # is not visible yet during terminal creation, screen creation is
        # deferred and retried on the next chunk; exact first-screen sizing is
        # load-bearing for TUI compositing.
        screen_size = None
        if use_screen and terminal_id not in self._screens:
            screen_size = self._resolve_screen_size(terminal_id)

        with self._lock:
            buffer = self._buffers.get(terminal_id, "") + chunk
            if len(buffer) > STATE_BUFFER_MAX:
                buffer = buffer[-STATE_BUFFER_MAX:]
            self._buffers[terminal_id] = buffer
            chunk_seq = self._bump_chunk_seq_locked(terminal_id)
            self._fifo_frame_seq[terminal_id] = self._fifo_frame_seq.get(terminal_id, 0) + 1
            if use_screen:
                screen_ready = self._feed_screen_locked(terminal_id, chunk, screen_size)
            else:
                screen_ready = False

        if not use_screen:
            # Debounced raw detection: same rising-edge + quiescence pattern as
            # the pyte path.  Detects immediately on the first chunk after quiet
            # (catches PROCESSING transition), then waits for output to settle
            # before re-detecting (catches IDLE/COMPLETED without running costly
            # regex on every single chunk during bursts).
            self._schedule_raw_detection(terminal_id, buffer, chunk_seq)
            return

        if screen_ready:
            self._schedule_screen_detection(terminal_id, provider, chunk_seq)

    def _apply_detection(
        self,
        terminal_id: str,
        detected: TerminalStatus,
        *,
        trusted_busy: bool = False,
        expected_seq: Optional[int] = None,
        pass_source: Literal["inline", "forced"] = "inline",
    ) -> None:
        """Apply the sticky-latch rules to a freshly detected status and publish
        on change. Shared by the raw and pyte detection paths.

        Stickiness: once a ready status is latched, refuse downgrades unless
        notify_input_sent() armed a revert. Two kinds of downgrade are blocked:
        1. ready → PROCESSING/UNKNOWN — buffer-eviction / mid-redraw flap.
        2. COMPLETED → IDLE — the response marker evicts before the user marker.
        The arm is consumed only by a genuine PROCESSING transition or an
        init-style non-ready → ready upgrade, never by a ready → ready flap
        (which would block the input's real PROCESSING and let InboxService
        paste into a busy agent).
        """
        screen_spinner_override: Optional[TerminalStatus] = None
        publish_external = False
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            observation_metadata = get_terminal_metadata(terminal_id)
        except Exception:
            observation_metadata = None
        with self._lock:
            pass_outcome: PassOutcome = "aborted"
            try:
                if expected_seq is not None and self._chunk_seq.get(terminal_id, 0) != expected_seq:
                    pass_outcome = "stale_seq"
                else:
                    last = self._last_status.get(terminal_id)
                    self._observe_locked(terminal_id, detected)

                    # UNKNOWN is "no signal", not a state: never let it overwrite a known
                    # status. Mid-turn the screen can momentarily show neither a spinner
                    # nor the prompt (e.g. while a tool runs), which the detector reports
                    # as UNKNOWN; downgrading a known PROCESSING to UNKNOWN there is a
                    # spurious transition (observed live as processing->unknown->completed).
                    #
                    # Do NOT narrow this to "suppress only when not armed" (to let an
                    # armed new turn clear a stale ready status). It does not actually
                    # close that window — the rising-edge frame right after a paste still
                    # composites the PREVIOUS turn's COMPLETED box, so get_status() reports
                    # ready whether or not UNKNOWN is let through — and it opens a worse
                    # one: an armed ready->UNKNOWN->ready re-render (torn paste frame, then
                    # the prior turn repainted before the new spinner draws) makes the
                    # bounce back to COMPLETED a non-ready->ready upgrade that CONSUMES the
                    # revert arm. The genuine PROCESSING that follows is then latch-blocked
                    # and the terminal reads ready for the entire busy turn — exactly what
                    # InboxService must never paste into. See
                    # test_armed_unknown_then_ready_rerender_keeps_processing. The initial
                    # UNKNOWN (last is None, nothing detected yet) is still allowed through.
                    if detected == TerminalStatus.UNKNOWN and last is not None:
                        pass_outcome = "unknown_suppressed"
                    else:
                        armed = self._allow_processing_revert.get(terminal_id, False)
                        sticky_rejected = False
                        if not armed:
                            if last in _STICKY_READY_STATUSES and detected in (
                                TerminalStatus.PROCESSING,
                                TerminalStatus.UNKNOWN,
                            ):
                                if trusted_busy and detected == TerminalStatus.PROCESSING:
                                    screen_spinner_override = last
                                else:
                                    sticky_rejected = True
                            if last == TerminalStatus.COMPLETED and detected == TerminalStatus.IDLE:
                                sticky_rejected = True

                        if sticky_rejected:
                            pass_outcome = "sticky_rejected"
                        elif detected == last:
                            if detected in _STICKY_READY_STATUSES:
                                self._status_gen[terminal_id] = self._processing_gen.get(
                                    terminal_id, 0
                                )
                                logger.info(
                                    "Terminal %s accepted %s generation: input_gen=%s "
                                    "processing_gen=%s status_gen=%s",
                                    terminal_id,
                                    detected.value,
                                    self._input_gen.get(terminal_id, 0),
                                    self._processing_gen.get(terminal_id, 0),
                                    self._status_gen.get(terminal_id, 0),
                                )
                            pass_outcome = pass_outcome_for_source(pass_source, "no_change")
                        else:
                            self._last_status[terminal_id] = detected
                            if detected == TerminalStatus.PROCESSING:
                                self._processing_gen[terminal_id] = self._input_gen.get(
                                    terminal_id, 0
                                )
                                self._allow_processing_revert[terminal_id] = False
                                logger.info(
                                    "Terminal %s accepted processing generation: input_gen=%s "
                                    "processing_gen=%s status_gen=%s",
                                    terminal_id,
                                    self._input_gen.get(terminal_id, 0),
                                    self._processing_gen.get(terminal_id, 0),
                                    self._status_gen.get(terminal_id, 0),
                                )
                            elif detected in _STICKY_READY_STATUSES:
                                self._status_gen[terminal_id] = self._processing_gen.get(
                                    terminal_id, 0
                                )
                                if last not in _STICKY_READY_STATUSES:
                                    self._allow_processing_revert[terminal_id] = False
                                logger.info(
                                    "Terminal %s accepted %s generation: input_gen=%s "
                                    "processing_gen=%s status_gen=%s",
                                    terminal_id,
                                    detected.value,
                                    self._input_gen.get(terminal_id, 0),
                                    self._processing_gen.get(terminal_id, 0),
                                    self._status_gen.get(terminal_id, 0),
                                )
                            pass_outcome = pass_outcome_for_source(pass_source, "accepted")
                            publish_external = True
            finally:
                try:
                    self._publish_observation(
                        terminal_id,
                        latched_status=self._last_status.get(terminal_id, TerminalStatus.UNKNOWN),
                        pass_outcome=pass_outcome,
                        frame_source="incremental",
                        metadata=observation_metadata,
                    )
                except Exception:
                    try:
                        self._log_receiver_publish_failure(terminal_id)
                    except Exception:
                        pass

        # Publish outside the lock — subscribers must never be able to
        # re-enter StatusMonitor while the latch state is mid-update.
        if publish_external:
            bus.publish(f"terminal.{terminal_id}.status", {"status": detected.value})
            __import__(f"{__package__}.auto_responder", fromlist=["auto_responder"]).auto_responder.record_published_status(terminal_id, detected)  # fmt: skip
            if screen_spinner_override is not None:
                logger.info("screen spinner override: %s→processing", screen_spinner_override.value)
            logger.info(f"Terminal {terminal_id} status changed: {detected.value}")

    # ----- pyte rendered-screen detection (edge-debounced) -------------------

    def _resolve_screen_size(self, terminal_id: str) -> Optional[Tuple[int, int]]:
        """Resolve (cols, rows) of the terminal's REAL pane for pyte sizing.

        Exact sizing is load-bearing, not cosmetic: the TUI app addresses rows
        and scrolls against the real pane height. A pyte screen with a
        different height never scrolls in step (an LF at the app's bottom row
        49 does not scroll a 50-row pyte screen), so the composited display
        degrades into a palimpsest of stale rows. Observed live: codex's
        spinner missing from the display while the ghost '› …' hint still
        matched the idle prompt — get_status latched COMPLETED through a whole
        busy turn and the stalled-callback watchdog false-fired.

        Must be called OFF the lock (shells out to tmux). None means the caller
        defers screen creation and retries on a later chunk. A pane resized
        mid-session is not tracked; the screen keeps its creation-time size
        until reset_buffer/clear_terminal drops it.
        """
        try:
            from cli_agent_orchestrator.backends.registry import get_backend
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            metadata = get_terminal_metadata(terminal_id)
            if not metadata:
                return None
            return get_backend().get_pane_size(metadata["tmux_session"], metadata["tmux_window"])
        except Exception:
            logger.exception("Failed to resolve pane size for %s", terminal_id)
            return None

    def _feed_screen_locked(
        self,
        terminal_id: str,
        chunk: str,
        screen_size: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Feed a chunk into the terminal's pyte screen. Caller holds the lock.

        Lazily creates the Screen+Stream so pyte is only imported/used when the
        screen path is active for this terminal. ``screen_size`` is the real
        pane's (cols, rows) resolved off-lock by the caller; used only on
        creation. If it is unavailable, creation is deferred so pyte never
        freezes a terminal at a fallback size. When the deferred first screen is
        eventually created, replay the rolling buffer so bytes received before
        metadata commit are not lost.
        """
        scr = self._screens.get(terminal_id)
        if scr is None:
            if screen_size is None:
                if terminal_id not in self._screen_size_deferred_warned:
                    self._screen_size_deferred_warned.add(terminal_id)
                    logger.warning(
                        "pyte screen creation deferred for %s: screen size unresolved",
                        terminal_id,
                    )
                return False
            import pyte

            cols, rows = screen_size
            screen = pyte.Screen(cols, rows)
            stream = pyte.Stream(screen)
            scr = (screen, stream)
            self._screens[terminal_id] = scr
            logger.info("pyte screen created for %s at %sx%s", terminal_id, cols, rows)
            chunk = self._buffers.get(terminal_id, "") or chunk
        scr[1].feed(chunk)
        return True

    def _detect_screen(self, terminal_id: str, provider) -> TerminalStatus:
        """Detect status from the terminal's composited pyte screen."""
        detected, _trusted_busy = self._detect_screen_with_trust(terminal_id, provider)
        return detected

    def _detect_screen_with_trust(self, terminal_id: str, provider) -> Tuple[TerminalStatus, bool]:
        """Detect screen status plus whether PROCESSING is a trusted screen read."""
        fallback_buffer: Optional[str] = None
        with self._lock:
            scr = self._screens.get(terminal_id)
            buffer = self._buffers.get(terminal_id, "")
            try:
                lines: List[str] = list(scr[0].display) if scr is not None else []
            except Exception:
                # pyte can transiently hold zero-length cell data while rendering
                # complex TUI redraws. Fall back to raw-buffer detection instead of
                # letting the quiescence callback tear down status monitoring.
                logger.exception(
                    "Error rendering screen status for %s; falling back to raw buffer",
                    terminal_id,
                )
                fallback_buffer = buffer
                lines = []
        if fallback_buffer is not None:
            if provider is None:
                return TerminalStatus.UNKNOWN, False
            try:
                return provider.get_status(fallback_buffer), False
            except Exception:
                logger.exception("Error detecting fallback status for %s", terminal_id)
                return TerminalStatus.UNKNOWN, False
        if not lines or provider is None:
            return TerminalStatus.UNKNOWN, False

        # Auto-responder: inspect the same composited screen for whitelisted
        # blocking dialogs (whitelist-only auto-answer, or WAITING_USER_ANSWER
        # + supervisor push for anything unrecognized). Capability-gated inside
        # on_screen (supports_screen_detection + CAO_AUTO_ANSWER kill switch),
        # so this is a no-op for providers/servers that don't opt in. A
        # non-None return overrides normal detection for this tick.
        try:
            from cli_agent_orchestrator.services.auto_responder import auto_responder

            override = auto_responder.on_screen(terminal_id, provider, lines)
            if override is not None:
                return override, False
        except Exception:
            logger.exception("Error in auto-responder for %s", terminal_id)

        try:
            return provider.get_status_from_screen(lines), True
        except Exception:
            # Full traceback: screen detectors are new and can trip on
            # unexpected TUI frames; the stack makes such regressions debuggable.
            logger.exception(f"Error detecting screen status for {terminal_id}")
            return TerminalStatus.UNKNOWN, False

    def _schedule_screen_detection(
        self, terminal_id: str, provider, chunk_seq: Optional[int] = None
    ) -> None:
        """Edge-debounce detection on the pyte screen.

        Rising edge (first chunk after quiet) → detect immediately (catches the
        PROCESSING transition the instant work resumes). Quiescence (no new
        chunk for PYTE_QUIESCENCE_DELAY_S) → detect again (the TUI repaint has
        settled, so the screen shows the true end state). Mid-burst detection
        also runs while cached status is ready/armed to catch small spinner
        repaints when debounce state is stuck bursting; only a detected
        PROCESSING result is applied, so torn mid-burst ready frames never latch.
        """
        loop = self._loop or self._running_loop()
        if loop is None:
            # No event loop (unit tests / offline replay): detect immediately
            # on the current screen — deterministic, no timing.
            detected, trusted_busy = self._detect_screen_with_trust(terminal_id, provider)
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=chunk_seq,
            )
            return

        with self._lock:
            if chunk_seq is None:
                chunk_seq = self._chunk_seq.get(terminal_id, 0)
            was_bursting = self._bursting.get(terminal_id, False)
            self._bursting[terminal_id] = True
            handle = self._quiesce_handle.pop(terminal_id, None)
            armed = self._allow_processing_revert.get(terminal_id, False)
            last_status = self._last_status.get(terminal_id)
        self._cancel_quiesce_handle(handle)

        if not was_bursting:
            detected, trusted_busy = self._detect_screen_with_trust(terminal_id, provider)
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=chunk_seq,
            )
        elif armed or last_status in _STICKY_READY_STATUSES or last_status is None:
            detected, trusted_busy = self._detect_screen_with_trust(terminal_id, provider)
            if detected == TerminalStatus.PROCESSING:
                self._apply_detection(
                    terminal_id,
                    detected,
                    trusted_busy=trusted_busy,
                    expected_seq=chunk_seq,
                )

        self._arm_quiesce_timer(loop, terminal_id, self._on_screen_quiescent, provider, chunk_seq)

    def _on_screen_quiescent(
        self, terminal_id: str, provider, expected_seq: Optional[int] = None
    ) -> None:
        """Quiescence timer fired: output stopped, so the screen has settled.

        Fires on the loop; offload the (potentially blocking) screen detection
        to a worker thread so the loop stays free.
        """
        with self._lock:
            if expected_seq is not None and self._chunk_seq.get(terminal_id, 0) != expected_seq:
                return
            self._bursting[terminal_id] = False
            self._quiesce_handle.pop(terminal_id, None)

        async def _detect_and_apply() -> None:
            detected, trusted_busy = await asyncio.to_thread(
                self._detect_screen_with_trust, terminal_id, provider
            )
            with self._lock:
                if expected_seq is not None and self._chunk_seq.get(terminal_id, 0) != expected_seq:
                    return
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=expected_seq,
            )

        loop = self._loop or self._running_loop()
        if loop is None:
            detected, trusted_busy = self._detect_screen_with_trust(terminal_id, provider)
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=expected_seq,
            )
        else:
            self._spawn_tracked(loop, _detect_and_apply())

    def _schedule_raw_detection(
        self, terminal_id: str, buffer: str, chunk_seq: Optional[int] = None
    ) -> None:
        """Edge-debounce detection on the raw rolling buffer.

        Detects on every chunk while the terminal is in a ready/armed state
        (to catch the IDLE→PROCESSING transition immediately). Once PROCESSING
        is observed, switches to quiescence-only detection (the busy→ready
        transition only matters after output settles). This prevents queue
        overflow during sustained output while ensuring InboxService never
        pastes into a busy terminal.

        Runs on a StatusMonitor worker thread (``run`` dispatches
        ``_process_chunk`` via ``asyncio.to_thread``), so the blocking
        ``_detect_status`` (which shells out to tmux) executes off the event
        loop. The quiescence timer is loop-affine, so it is armed on the
        captured loop via ``call_soon_threadsafe`` rather than the current
        thread's (nonexistent) loop.
        """
        loop = self._loop or self._running_loop()
        if loop is None:
            # No loop ever captured (unit tests / offline replay): detect
            # inline and skip the debounce timer.
            self._apply_detection(terminal_id, self._detect_status(terminal_id, buffer))
            return

        with self._lock:
            if chunk_seq is None:
                chunk_seq = self._chunk_seq.get(terminal_id, 0)
            was_bursting = self._bursting.get(terminal_id, False)
            self._bursting[terminal_id] = True
            handle = self._quiesce_handle.pop(terminal_id, None)
            last_status = self._last_status.get(terminal_id)
        self._cancel_quiesce_handle(handle)

        # While terminal is ready/armed, detect on every chunk so the
        # IDLE→PROCESSING transition is never missed (prevents stale-IDLE
        # delivery by InboxService). Once PROCESSING is observed, debounce.
        if not was_bursting or last_status in _STICKY_READY_STATUSES or last_status is None:
            detected = self._detect_status(terminal_id, buffer)
            self._apply_detection(terminal_id, detected, expected_seq=chunk_seq)

        self._arm_quiesce_timer(loop, terminal_id, self._on_raw_quiescent, chunk_seq)

    def _arm_quiesce_timer(self, loop, terminal_id: str, callback, *cb_args) -> None:
        """Schedule the quiescence timer on ``loop`` from any thread.

        ``loop.call_later`` is not thread-safe and this may run on a worker
        thread, so marshal the scheduling onto the loop with
        ``call_soon_threadsafe``. The resulting TimerHandle is stored from
        inside the marshaled closure (still on the loop thread) so cancel
        marshaling in ``_cancel_quiesce_handle`` stays correct. ``cb_args``
        are extra positional args passed to ``callback`` after ``terminal_id``.
        """

        def _arm() -> None:
            # Runs on the loop thread (via call_soon_threadsafe), so it is safe
            # to cancel a prior TimerHandle directly here. Cancel any existing
            # timer for this terminal BEFORE arming the new one: if several
            # chunks arrive in quick succession their _arm closures are queued
            # together, and without this the later closure would overwrite
            # _quiesce_handle while leaving the earlier timer live — two timers
            # then fire, and a stale one firing mid-burst causes early/duplicate
            # quiescence detections and status flaps. One outstanding timer per
            # terminal, always the latest.
            with self._lock:
                prior = self._quiesce_handle.get(terminal_id)
                if prior is not None:
                    prior.cancel()
                handle = loop.call_later(PYTE_QUIESCENCE_DELAY_S, callback, terminal_id, *cb_args)
                self._quiesce_handle[terminal_id] = handle

        try:
            loop.call_soon_threadsafe(_arm)
        except RuntimeError:
            # Loop closed during shutdown — quiescence re-detect is moot.
            pass

    def _on_raw_quiescent(self, terminal_id: str, expected_seq: Optional[int] = None) -> None:
        """Quiescence timer fired for raw path: re-detect from current buffer.

        Fires on the event loop (via call_later), so the blocking
        ``_detect_status`` is offloaded to a worker thread to keep the loop
        free — a tmux ``get_pane_current_command`` here would otherwise fork
        on the loop.
        """
        with self._lock:
            if expected_seq is not None and self._chunk_seq.get(terminal_id, 0) != expected_seq:
                return
            self._bursting[terminal_id] = False
            self._quiesce_handle.pop(terminal_id, None)
            buffer = self._buffers.get(terminal_id, "")

        async def _detect_and_apply() -> None:
            detected = await asyncio.to_thread(self._detect_status, terminal_id, buffer)
            with self._lock:
                if expected_seq is not None and self._chunk_seq.get(terminal_id, 0) != expected_seq:
                    return
            self._apply_detection(terminal_id, detected, expected_seq=expected_seq)

        loop = self._loop or self._running_loop()
        if loop is None:
            self._apply_detection(
                terminal_id,
                self._detect_status(terminal_id, buffer),
                expected_seq=expected_seq,
            )
        else:
            self._spawn_tracked(loop, _detect_and_apply())

    def _spawn_tracked(self, loop, coro) -> None:
        """Create a task on ``loop`` and hold a strong reference until it
        finishes, so asyncio's weak task references can't GC it mid-run."""
        task = loop.create_task(coro)
        self._detect_tasks.add(task)
        task.add_done_callback(self._detect_tasks.discard)

    @staticmethod
    def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _cancel_quiesce_handle(self, handle: Optional[asyncio.TimerHandle]) -> None:
        """Cancel a quiescence timer safely from any thread.

        The timer is an asyncio.TimerHandle owned by ``self._loop``.
        TimerHandle.cancel() mutates loop-internal scheduling state and is NOT
        thread-safe, yet clear_terminal/reset_buffer can run off the loop thread
        (cleanup_old_data is dispatched via asyncio.to_thread). Marshal the
        cancel onto the owning loop with call_soon_threadsafe unless we are
        already on it.
        """
        if handle is None:
            return
        loop = self._loop
        if loop is None:
            handle.cancel()  # no loop ever captured (unit/offline path) — safe
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            handle.cancel()
        else:
            try:
                loop.call_soon_threadsafe(handle.cancel)
            except RuntimeError:
                pass  # loop already closed during shutdown — the timer is moot

    def notify_input_sent(self, terminal_id: str) -> None:
        """Arm the next PROCESSING transition.

        Call before any send_keys / paste that initiates a new processing
        cycle (terminal_service.send_input, provider.initialize warm-up
        and CLI-launch keystrokes). Without this, a previously-latched
        IDLE/COMPLETED would block the genuine PROCESSING transition.
        """
        with self._lock:
            self._input_gen[terminal_id] = self._input_gen.get(terminal_id, 0) + 1
            self._bump_chunk_seq_locked(terminal_id)
            self._allow_processing_revert[terminal_id] = True
            logger.info(
                "Terminal %s input sent generation: input_gen=%s processing_gen=%s "
                "status_gen=%s",
                terminal_id,
                self._input_gen[terminal_id],
                self._processing_gen.get(terminal_id, 0),
                self._status_gen.get(terminal_id, 0),
            )

    def get_input_gen(self, terminal_id: str) -> int:
        """Return the current input-event generation for a terminal."""
        with self._lock:
            return self._input_gen.get(terminal_id, 0)

    def get_status_gen(self, terminal_id: str) -> Optional[int]:
        """Return the ready-status generation, or None for event-inbox terminals."""
        from cli_agent_orchestrator.backends.registry import get_backend

        if get_backend().supports_event_inbox():
            return None
        with self._lock:
            return self._status_gen.get(terminal_id, 0)

    def get_boundary_observation(self, terminal_id: str) -> BoundaryObservation:
        """Return one status/cycle snapshot sampled under the monitor lock."""
        with self._lock:
            status = self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)
            return BoundaryObservation(
                observation_epoch=self._epoch_locked(terminal_id),
                status=status,
                status_gen=self._status_gen.get(terminal_id, 0),
                input_gen=self._input_gen.get(terminal_id, 0),
                seq=self._observation_seq.get(terminal_id, 0),
                last_non_ready_seq=self._last_non_ready_seq.get(terminal_id),
                last_ready_seq=self._last_ready_seq.get(terminal_id),
            )

    def mark_injection_completed(self, terminal_id: str) -> BoundaryObservation:
        """Anchor a successful backend submit in the observation sequence."""
        with self._lock:
            status = self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)
            seq = self._observe_locked(terminal_id, status)
            return BoundaryObservation(
                observation_epoch=self._epoch_locked(terminal_id),
                status=status,
                status_gen=self._status_gen.get(terminal_id, 0),
                input_gen=self._input_gen.get(terminal_id, 0),
                seq=seq,
                last_non_ready_seq=self._last_non_ready_seq.get(terminal_id),
                last_ready_seq=self._last_ready_seq.get(terminal_id),
            )

    def clear_rolling_buffer(self, terminal_id: str) -> None:
        """Clear ONLY the rolling byte buffer for a terminal — preserves
        ``_last_status`` and ``_allow_processing_revert``.

        Used by send_input to drop stale pre-task content (e.g. kiro-cli 2.11's
        "ask a question" idle placeholder) so it can't combine with the
        input_received flag to trigger a false COMPLETED before the agent has
        rendered its processing indicator. Unlike ``reset_buffer``, this does
        NOT wipe the sticky-latch state, so the arm set by ``notify_input_sent``
        survives and the subsequent IDLE→PROCESSING transition is honored.
        """
        with self._lock:
            self._buffers[terminal_id] = ""

    def _detect_status(self, terminal_id: str, buffer: str) -> TerminalStatus:
        """Detect status: provider-specific patterns or UNKNOWN if no provider."""
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return TerminalStatus.UNKNOWN

        try:
            return provider.get_status(buffer)
        except Exception as e:
            logger.error(f"Error detecting status for {terminal_id}: {e}")
            return TerminalStatus.UNKNOWN

    def clear_terminal(self, terminal_id: str) -> None:
        """Free buffer and status for a deleted terminal."""
        with self._lock:
            self._buffers.pop(terminal_id, None)
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            self._input_gen.pop(terminal_id, None)
            self._processing_gen.pop(terminal_id, None)
            self._status_gen.pop(terminal_id, None)
            self._observation_epoch.pop(terminal_id, None)
            self._observation_seq.pop(terminal_id, None)
            self._last_non_ready_seq.pop(terminal_id, None)
            self._last_ready_seq.pop(terminal_id, None)
            self._fifo_frame_seq.pop(terminal_id, None)
            self._screens.pop(terminal_id, None)
            self._screen_size_deferred_warned.discard(terminal_id)
            self._bursting.pop(terminal_id, None)
            self._bump_chunk_seq_locked(terminal_id)
            handle = self._quiesce_handle.pop(terminal_id, None)
            self._receiver_state_store.invalidate_terminal(terminal_id)
        self._cancel_quiesce_handle(handle)

    def reset_buffer(self, terminal_id: str) -> None:
        """Clear the rolling buffer + last-known status WITHOUT forgetting the
        terminal.

        Used when a provider relaunches a different CLI mode on the SAME
        ``terminal_id`` (e.g. Kiro's TUI -> ``--legacy-ui`` fallback). Without
        this, the retry re-derives status from a buffer still full of stale bytes
        from the failed first attempt and can spuriously time out.
        """
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            metadata = get_terminal_metadata(terminal_id)
            receiver_key = (
                (
                    terminal_id,
                    int(metadata["lifecycle_generation"]),
                    str(metadata["tmux_window"]),
                )
                if metadata is not None
                else None
            )
        except Exception:
            receiver_key = None
        with self._lock:
            if receiver_key is not None:
                self._receiver_state_store.invalidate(receiver_key)
            self._buffers[terminal_id] = ""
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            self._input_gen.pop(terminal_id, None)
            self._processing_gen.pop(terminal_id, None)
            self._status_gen.pop(terminal_id, None)
            self._new_epoch_locked(terminal_id)
            # Drop the rendered screen too so the relaunched CLI mode is
            # detected against a fresh viewport, not the failed attempt's.
            self._screens.pop(terminal_id, None)
            self._screen_size_deferred_warned.discard(terminal_id)
            self._bursting.pop(terminal_id, None)
            self._bump_chunk_seq_locked(terminal_id)
            handle = self._quiesce_handle.pop(terminal_id, None)
        self._cancel_quiesce_handle(handle)

    def get_raw_status(self, terminal_id: str, provider_override=None) -> TerminalStatus:
        """Return provider/backend status without the durable recovery overlay.

        Pipe-pane backends (tmux) return the last status pushed by the FIFO →
        EventBus → _process_chunk pipeline. Event-inbox backends (herdr) don't
        feed that pipeline (no FIFO reader is started for them), so _last_status
        would stay UNKNOWN forever; for those we derive status on demand from the
        provider, whose get_status() consults backend.get_native_status(). Direct
        raw reads are internal to rebind; external callers go through get_status(),
        which applies the durable recovery projection before delegating here.
        """
        from cli_agent_orchestrator.backends.registry import get_backend

        if get_backend().supports_event_inbox():
            try:
                provider = provider_override or provider_manager.get_provider(terminal_id)
            except Exception:
                provider = None
            if provider is not None:
                with self._lock:
                    buffer = self._buffers.get(terminal_id, "")
                try:
                    # The native (herdr) path ignores the buffer arg; pass the
                    # rolling buffer (empty for herdr) so the rare
                    # get_native_status()==None fallback still gets what we have.
                    # provider.get_status may shell out to the herdr CLI — call
                    # it outside the lock.
                    return provider.get_status(buffer)
                except Exception as e:
                    logger.error(f"Error deriving native status for {terminal_id}: {e}")
                    return TerminalStatus.UNKNOWN

        with self._lock:
            cached = self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)
            # When cached status is PROCESSING, the debounced detection may be
            # stuck: TUI providers (kiro-cli) can send escape sequences
            # continuously after becoming idle, preventing the 200ms quiescence
            # timer from ever firing. Do a fresh detection from the current
            # buffer so poll-based callers (wait_until_status) catch the
            # PROCESSING→ready transition without waiting for stream silence.
            if cached == TerminalStatus.PROCESSING:
                buffer = self._buffers.get(terminal_id, "")
            else:
                buffer = ""

        if cached == TerminalStatus.PROCESSING and buffer:
            if provider_override is None:
                fresh = self._detect_status(terminal_id, buffer)
            else:
                try:
                    fresh = provider_override.get_status(buffer)
                except Exception:
                    fresh = TerminalStatus.UNKNOWN
            logger.debug(
                f"get_status [{terminal_id}]: cached=PROCESSING, "
                f"fresh={fresh.value}, buffer_len={len(buffer)}"
            )
            if fresh != TerminalStatus.PROCESSING and fresh != TerminalStatus.UNKNOWN:
                self._apply_detection(terminal_id, fresh)
                return fresh
        return cached

    def get_status(self, terminal_id: str) -> TerminalStatus:
        """Return externally projected health, quarantining recovery states."""
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            metadata = get_terminal_metadata(terminal_id)
            if metadata and metadata.get("recovery_state") not in (None, "rebound"):
                return TerminalStatus.ERROR
        except Exception:
            pass
        return self.get_raw_status(terminal_id)

    def get_buffer(self, terminal_id: str) -> str:
        """Get accumulated output buffer for a terminal."""
        with self._lock:
            return self._buffers.get(terminal_id, "")

    def get_fifo_frame_gen(self, terminal_id: str) -> int:
        """Counter advanced exclusively by frames entering via _process_chunk."""
        with self._lock:
            return self._fifo_frame_seq.get(terminal_id, 0)

    def force_status(self, terminal_id: str, status: TerminalStatus) -> None:
        """Force-publish a status, going through the normal latch/publish path.

        Used by the auto-responder to surface WAITING_USER_ANSWER when a
        retry-exhausted rule leaves a dialog unresolved outside the regular
        detection tick (its verify/retry loop runs on a background thread,
        off the event loop, so it can't just return an override like
        ``_detect_screen`` callers do).
        """
        self._apply_detection(terminal_id, status, pass_source="forced")

    def probe_screen_status(self, terminal_id: str) -> Tuple[TerminalStatus, ScreenProbeMeta]:
        """Classify a frame with temporal progress corroboration before admission."""
        from cli_agent_orchestrator.providers.screen_classification import (
            ScreenClassification,
            ScreenClassificationResult,
            ScreenSignal,
            screen_classification_result,
        )

        def processing_result(
            signals: tuple[ScreenSignal, ...] = (),
        ) -> ScreenClassificationResult:
            return ScreenClassificationResult(
                ScreenClassification(TerminalStatus.PROCESSING, "progress", None, None),
                signals,
            )

        try:
            provider = provider_manager.get_provider(terminal_id)
        except Exception:
            provider = None
        with self._lock:
            screen_state = self._screens.get(terminal_id)
            if screen_state is None:
                rows: List[str] = []
                columns = 0
                row_count = 0
            else:
                screen = screen_state[0]
                try:
                    rows = list(getattr(screen, "display", []))
                    columns = int(getattr(screen, "columns", 0))
                    row_count = int(getattr(screen, "lines", len(rows)))
                except Exception:
                    logger.exception("Error snapshotting screen probe for %s", terminal_id)
                    rows = []
                    columns = 0
                    row_count = 0

        def classify(frame_rows: List[str]) -> ScreenClassificationResult:
            if not frame_rows or provider is None:
                return screen_classification_result([])
            try:
                return provider.classify_screen(frame_rows)
            except Exception:
                logger.exception("Error classifying admission screen for %s", terminal_id)
                return screen_classification_result([])

        classification = classify(rows)

        frame_source: ScreenProbeFrameSource = "incremental"
        identity_proof_failure: str | None = None
        probe_failure: (
            Literal["empty_capture", "malformed_meta", "provider_hook_exception"] | None
        ) = None

        backend = None
        metadata = None

        def load_route() -> tuple[Any, dict[str, Any]]:
            nonlocal backend, metadata
            if backend is None or metadata is None:
                from cli_agent_orchestrator.backends.registry import get_backend
                from cli_agent_orchestrator.clients.database import get_terminal_metadata

                metadata = get_terminal_metadata(terminal_id)
                if not metadata:
                    raise ValueError(f"No terminal metadata for {terminal_id}")
                backend = get_backend()
            return backend, metadata

        def capture() -> tuple[List[str], int, int, ScreenClassificationResult]:
            route_backend, route_metadata = load_route()
            captured = route_backend.capture_viewport(
                route_metadata["tmux_session"], route_metadata["tmux_window"]
            )
            pane_size = route_backend.get_pane_size(
                route_metadata["tmux_session"], route_metadata["tmux_window"]
            )
            captured_rows = captured.splitlines()
            if not captured_rows or not any(row.strip() for row in captured_rows):
                raise EmptyProbeCapture("Fresh viewport capture was empty")
            if (
                isinstance(pane_size, tuple)
                and len(pane_size) == 2
                and all(isinstance(value, int) for value in pane_size)
            ):
                captured_columns, captured_row_count = pane_size
            else:
                captured_columns = max((len(row) for row in captured_rows), default=0)
                captured_row_count = len(captured_rows)
            return (
                captured_rows,
                captured_columns,
                captured_row_count,
                classify(captured_rows),
            )

        def prove_identity() -> None:
            nonlocal identity_proof_failure
            route_backend, route_metadata = load_route()
            if getattr(route_backend, "supports_identity_readback", False) is not True:
                result = route_backend.read_native_identity(
                    terminal_id,
                    route_metadata["tmux_session"],
                    route_metadata["tmux_window"],
                    route_metadata.get("provider", "unknown"),
                )
                verdict = getattr(result, "verdict", None)
                if verdict not in {"match", "mismatch", "unavailable"}:
                    logger.warning(
                        "pane_identity_proof_unsupported terminal=%s backend=%s",
                        terminal_id,
                        type(route_backend).__name__,
                    )
                    return
                if verdict != "match":
                    identity_proof_failure = f"native_identity_{verdict}"
                    raise PaneIdentityProofFailure(identity_proof_failure)
                return
            from cli_agent_orchestrator.services.pane_identity_service import (
                pane_identity_failure,
            )

            identity_proof_failure = pane_identity_failure(
                terminal_id, route_metadata, route_backend
            )
            if identity_proof_failure is not None:
                logger.critical(
                    "pane_identity_proof_failed terminal=%s session=%s window=%s "
                    "reason=%s stage=admission",
                    terminal_id,
                    route_metadata["tmux_session"],
                    route_metadata["tmux_window"],
                    identity_proof_failure,
                )
                raise PaneIdentityProofFailure(identity_proof_failure)

        if provider is not None and classification.status in {
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
        }:
            frame_source = "fresh_capture"
            try:
                prove_identity()
                rows, columns, row_count, classification = capture()
            except PaneIdentityProofFailure:
                rows, columns, row_count = [], 0, 0
                classification = screen_classification_result([])
            except EmptyProbeCapture:
                probe_failure = "empty_capture"
                rows, columns, row_count = [], 0, 0
                classification = screen_classification_result([])
            except Exception:
                logger.exception("Error refreshing admission screen for %s", terminal_id)
                probe_failure = "empty_capture"
                rows, columns, row_count = [], 0, 0
                classification = screen_classification_result([])

        temporal_demotion: ScreenProbeTemporalDemotion | None = None
        deciding_is_corroborable = any(
            signal.signal_class == classification.signal_class
            and signal.provider_signal == classification.provider_signal
            and signal.row_index == classification.row_index
            and signal.temporal_policy == "corroborable"
            for signal in classification.signals
        )

        if (
            provider is not None
            and classification.status == TerminalStatus.PROCESSING
            and deciding_is_corroborable
        ):
            previous: tuple[str, ...] = ()
            corroboration_frames = 0
            try:
                # The incremental frame only admits temporal corroboration. The
                # first sample in the temporal sequence must come from the live
                # viewport, and it must still be corroborable progress.
                fresh_rows, fresh_columns, fresh_row_count, fresh_result = capture()
                rows, columns, row_count = fresh_rows, fresh_columns, fresh_row_count
                frame_source = "fresh_capture"
                previous = _corroborable_rows(fresh_result)
                fresh_deciding_is_corroborable = any(
                    signal.signal_class == fresh_result.signal_class
                    and signal.provider_signal == fresh_result.provider_signal
                    and signal.row_index == fresh_result.row_index
                    and signal.temporal_policy == "corroborable"
                    for signal in fresh_result.signals
                )
                fresh_sample_is_busy = (
                    fresh_result.status == TerminalStatus.PROCESSING
                    and fresh_deciding_is_corroborable
                )
                if not fresh_sample_is_busy:
                    classification = processing_result(fresh_result.signals)
                    previous = ()
                else:
                    classification = fresh_result
                    for _ in range(2):
                        time.sleep(1.2)
                        fresh_rows, fresh_columns, fresh_row_count, fresh_result = capture()
                        corroboration_frames += 1
                        rows, columns, row_count = fresh_rows, fresh_columns, fresh_row_count
                        frame_source = "fresh_capture"
                        current = _corroborable_rows(fresh_result)
                        if Counter(current) != Counter(previous):
                            classification = processing_result(fresh_result.signals)
                            break
                        classification = fresh_result
                        previous = current
                    else:
                        temporal_demotion = {
                            "frames": corroboration_frames,
                            "multiset_sha256": _row_multiset_hash(previous),
                        }
            except EmptyProbeCapture:
                probe_failure = "empty_capture"
                classification = processing_result()
            except Exception:
                logger.exception("Error corroborating admission screen for %s", terminal_id)
                probe_failure = "empty_capture"
                classification = processing_result()

            if temporal_demotion is not None:
                try:
                    prove_identity()
                    fresh_rows, fresh_columns, fresh_row_count, final_result = capture()
                    rows, columns, row_count = fresh_rows, fresh_columns, fresh_row_count
                    frame_source = "fresh_capture"
                    final_rows = _corroborable_rows(final_result)
                    if Counter(final_rows) != Counter(previous):
                        classification = processing_result(final_result.signals)
                    else:
                        remaining = list(final_result.signals)
                        demotions = Counter(previous)
                        kept = []
                        for signal in remaining:
                            if (
                                signal.signal_class == "progress"
                                and signal.temporal_policy == "corroborable"
                                and isinstance(signal.row_bytes, str)
                                and demotions[signal.row_bytes] > 0
                            ):
                                demotions[signal.row_bytes] -= 1
                            else:
                                kept.append(signal)
                        classification = screen_classification_result(kept)
                        if classification.status not in {
                            TerminalStatus.IDLE,
                            TerminalStatus.COMPLETED,
                        }:
                            classification = processing_result(tuple(kept))
                except PaneIdentityProofFailure:
                    rows, columns, row_count = [], 0, 0
                    classification = screen_classification_result([])
                except EmptyProbeCapture:
                    probe_failure = "empty_capture"
                    rows, columns, row_count = [], 0, 0
                    classification = screen_classification_result([])
                except Exception:
                    logger.exception("Error finalizing admission screen for %s", terminal_id)
                    classification = processing_result()

        if provider is not None and frame_source == "incremental" and probe_failure is None:
            frame_source = "fresh_capture"
            try:
                prove_identity()
                rows, columns, row_count, classification = capture()
            except PaneIdentityProofFailure:
                rows, columns, row_count = [], 0, 0
                classification = screen_classification_result([])
            except EmptyProbeCapture:
                probe_failure = "empty_capture"
                rows, columns, row_count = [], 0, 0
                classification = screen_classification_result([])
            except Exception:
                logger.exception("Error refreshing final admission screen for %s", terminal_id)
                probe_failure = "empty_capture"
                rows, columns, row_count = [], 0, 0
                classification = screen_classification_result([])

        if (
            classification.status == TerminalStatus.UNKNOWN
            and probe_failure is None
            and identity_proof_failure is None
        ):
            probe_failure = "malformed_meta"

        status_values: Dict[TerminalStatus, ScreenProbeResult] = {
            TerminalStatus.WAITING_USER_ANSWER: "waiting_user_answer",
            TerminalStatus.ERROR: "error",
            TerminalStatus.PROCESSING: "processing",
            TerminalStatus.COMPLETED: "completed",
            TerminalStatus.IDLE: "idle",
            TerminalStatus.UNKNOWN: "unknown",
        }
        assert classification.status != TerminalStatus.RENDER_UNCERTAIN
        result_status = status_values[classification.status]
        meta: ScreenProbeMeta = {
            "probed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "geometry": {"columns": columns, "rows": row_count},
            "frame_rows_hash": _frame_rows_hash(rows),
            "frame_source": frame_source,
            "result_status": result_status,
            "law_signal": {
                "class": classification.signal_class,
                "provider_signal": classification.provider_signal,
                "row_index": classification.row_index,
            },
        }
        if identity_proof_failure is not None:
            meta["identity_proof_failure"] = identity_proof_failure
        if probe_failure is not None:
            meta["probe_failure"] = probe_failure
        if temporal_demotion is not None:
            meta["temporal_demotion"] = temporal_demotion
        if provider is not None:
            try:
                injection_hazard = provider.classify_injection_hazard(rows)
                if injection_hazard is not None:
                    meta["injection_hazard"] = injection_hazard
            except Exception:
                meta["probe_failure"] = "provider_hook_exception"
                logger.exception("Error classifying injection hazard for %s", terminal_id)
            try:
                if provider.transient_error_detected(rows, classification):
                    meta["transient_api_error"] = True
            except Exception:
                logger.exception("Error evaluating transient-error signal for %s", terminal_id)
            try:
                idle_reason = provider.classify_idle_reason(rows, classification)
                if idle_reason is not None:
                    meta["idle_reason"] = idle_reason
            except Exception:
                logger.exception("Error classifying idle reason for %s", terminal_id)
        freshness_kind = "identity_ok"
        freshness_detail = None
        if identity_proof_failure is not None:
            freshness_kind = "identity_failed"
            freshness_detail = identity_proof_failure
        elif probe_failure is not None:
            freshness_kind = "probe_failed"
            freshness_detail = probe_failure
        try:
            if metadata is None:
                from cli_agent_orchestrator.clients.database import get_terminal_metadata

                metadata = get_terminal_metadata(terminal_id)
            with self._lock:
                self._publish_observation(
                    terminal_id,
                    latched_status=classification.status,
                    pass_outcome="probe",
                    frame_source="fresh_capture",
                    metadata=metadata,
                    freshness_proof=FreshnessProof(freshness_kind, freshness_detail),
                )
        except Exception:
            try:
                self._log_receiver_publish_failure(terminal_id)
            except Exception:
                pass
        return classification.status, meta

    def get_rendered_screen(self, terminal_id: str) -> Optional[List[str]]:
        """Return the current pyte-composited screen for a terminal if present."""
        with self._lock:
            scr = self._screens.get(terminal_id)
            if scr is None:
                return None
            try:
                return list(scr[0].display)
            except Exception:
                logger.exception("Error rendering screen for %s", terminal_id)
                return None


# Module-level singleton
status_monitor = StatusMonitor()
