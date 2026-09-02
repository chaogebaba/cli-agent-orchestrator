"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import asyncio
import copy
import hashlib
import logging
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, NotRequired, Optional, Tuple, TypedDict

from cli_agent_orchestrator.backends.herdr_backend import map_native_status
from cli_agent_orchestrator.constants import (
    CAO_PYTE_STATUS,
    PYTE_QUIESCENCE_DELAY_S,
)
from cli_agent_orchestrator.kernel.receiver_state import (
    FreshnessProof,
    FreshToken,
    NativeEvidence,
    PassOutcome,
    ProbeEvidence,
    ReceiverState,
    ReceiverStateStore,
    pass_outcome_for_source,
)
from cli_agent_orchestrator.models.native_publish import (
    DispatchTxn,
    NativePublishRequest,
    SettlementFence,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.event import terminal_id_from_topic
from cli_agent_orchestrator.utils.terminal_render import ScreenRenderCache

logger = logging.getLogger(__name__)

# F516 commit 1: injectable monotonic clock seam (precedent: question_state.py
# ``_clock``). The D4 detection-retry backoff (commit 4) reads through this seam
# so the deterministic 1/2/4/8s schedule is observable in the loop-less replay
# harness without wall-clock time. Production reads the real monotonic clock.
_clock: Callable[[], float] = time.monotonic

# F360 (#215, diff gate SHOULD): consecutive "Terminal ... not found in
# database" misses from provider_manager.get_provider() tolerated before a
# ghost terminal id is dropped from the watch state. 3 (not 2) so a live
# terminal whose creation commit is delayed by lock contention — while
# buffered output events still arrive — is not dropped mid-creation.
GHOST_DROP_MISSES = 3


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


@dataclass(frozen=True)
class IdentityProof:
    terminal_id: str
    method: Literal["pane_readback", "native"]
    proven_at_mono: float
    failure: str | None


@dataclass(frozen=True)
class ProbeResult:
    status: TerminalStatus
    meta: ScreenProbeMeta
    fresh_token: FreshToken


@dataclass(frozen=True)
class SettlementEntry:
    handle: asyncio.TimerHandle
    fenced_request: NativePublishRequest
    native_event_gen: int
    dispatch_gen: int


PROOF_MAX_AGE_S = 2.0


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
# Why: the event-driven pipeline derives status from a rolling state buffer,
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

# F579 D17: consecutive non-PROCESSING status publishes after which the
# children-ledger publish-time reconcile drops all entries (D3's K). A lost
# SubagentStop cannot pin a seat at delegating beyond this many ticks.
_CHILDREN_RECONCILE_K_TICKS = 3


@dataclass(frozen=True)
class BoundaryObservation:
    observation_epoch: str
    status: TerminalStatus
    status_gen: Optional[int]
    input_gen: int
    seq: int
    last_non_ready_seq: Optional[int]
    last_ready_seq: Optional[int]
    # F506: appended AFTER the seven existing fields and both DEFAULTED so the
    # deliberately-unfused mark_injection_completed construction, the keyword
    # get_boundary_observation site, and the ~30 positional seven-arg test
    # constructions all compile unchanged and read as "no fusion applied"
    # (r7 R7-S1; r8 R8-N1 — appending is load-bearing, a defaulted field before
    # the required seven is a Python error and positional fixtures would shift).
    #
    # fusion_reason is an EVIDENCE TAG (rules 1/3 stamp it on unchanged
    # statuses); fusion_changed is True iff the fused status differs from the
    # status passed in — the reason alone cannot recover this (rules 1/2 share
    # "question_marker", rule 3a stamps "pane_delta" on no-ops, R5-S2). §8's
    # fleet-TUI asterisk keys on fusion_changed. fusion_changed is pass-relative
    # and does NOT re-derive on a second pass, which is why AC21 scopes
    # idempotence to status and reason only.
    fusion_reason: Optional[str] = None
    fusion_changed: bool = False


# Stale-PROCESSING self-heal (#558). get_status()'s cheap re-check re-derives from the SAME
# rolling buffer the FIFO pipeline feeds — and the moment a process goes genuinely idle it also
# stops emitting output, so that buffer stops changing. If its final content never happened to
# parse as a ready state (the idle marker rotated out of the bounded window, or a truncated
# escape corrupted the tail), re-running detection on the unchanging buffer returns
# PROCESSING/UNKNOWN forever: the pane already shows the finished response while every poller
# sees PROCESSING. The #397 pipe-liveness watchdog cannot see this either — the FIFO delivered
# its bytes, so the pipe looks healthy. Observed live: a queued message sat undelivered for
# ~10 minutes until a manual tmux resize forced a redraw. The self-heal reads the pane directly
# via get_backend().get_history() (a real ``tmux capture-pane``, NOT the FIFO-fed buffer — tmux
# always holds the current rendered state regardless of output volume) and re-detects from that.
# This constant rate-limits those reads: get_status() is a hot path (every wait_until_status
# poll, every UI refresh, fleet-wide) and a capture-pane is a real subprocess fork — unbounded,
# it would recreate the fork-storm class run()'s own docstring documents.
STALE_PROCESSING_CAPTURE_INTERVAL_S = 3.0

# The interval above bounds how OFTEN the fallback re-runs; this gates WHETHER it runs at all.
# Without it, every genuinely busy terminal would eat a capture-pane subprocess every ~3s for
# the entire duration of every ordinary turn, on top of the fifo watchdog's own probing. The
# wedge's actual signature is "the rolling buffer stopped changing", so require exactly that:
# only attempt a capture once no new chunk has been appended for this long. A terminal
# mid-burst never reaches the fallback at all.
STALE_PROCESSING_BUFFER_QUIET_S = 3.0

# A ready verdict from a single capture is never honored on its own — it must repeat on the
# next eligible read (see _fresh_capture_pane_status). This bounds how far apart those two
# reads may be: a candidate older than this is dropped and confirmation starts over. Without
# the bound, a candidate recorded during one turn could sit in the map indefinitely and be
# "confirmed" by one lone mid-repaint frame much later — exactly the single-frame latch the
# two-read confirm exists to prevent.
STALE_PROCESSING_CONFIRM_TTL_S = 2 * STALE_PROCESSING_CAPTURE_INTERVAL_S


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
        # Monotonic per-terminal byte-buffer generation.  A provider that
        # remembers positions across get_status() calls needs an explicit reset
        # boundary when send_input discards the old rolling buffer; content
        # overlap alone cannot distinguish a fresh, byte-identical turn from a
        # stale screen redraw.
        self._buffer_epochs: Dict[str, int] = {}
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
        # Per-terminal monotonic timestamp of the last stale-PROCESSING capture-pane
        # attempt — the STALE_PROCESSING_CAPTURE_INTERVAL_S rate limit. Absence is None,
        # deliberately NOT 0.0: time.monotonic()'s reference point is arbitrary, so a 0.0
        # sentinel is indistinguishable from a genuine reading and could rate-limit the
        # very first check before it ever runs.
        self._last_stale_capture_check: Dict[str, Optional[float]] = {}
        # Per-terminal monotonic timestamp of the last time _process_chunk actually
        # appended a chunk (i.e. the buffer changed) — the STALE_PROCESSING_BUFFER_QUIET_S
        # quiet gate reads this. Same None-vs-0.0 sentinel rule as above.
        self._buffer_changed_at: Dict[str, Optional[float]] = {}
        # Per-terminal (status, monotonic, generation) candidate from a
        # stale-PROCESSING capture, awaiting a second confirming read before being
        # honored (see _fresh_capture_pane_status). Cleared on confirm, on an
        # intervening PROCESSING/UNKNOWN read, on expiry past
        # STALE_PROCESSING_CONFIRM_TTL_S, by a real chunk landing in
        # _process_chunk, and by notify_input_sent — new input or new output means
        # whatever the pane showed before no longer describes the terminal. The
        # third element is the generation sampled BEFORE the candidate's pane
        # read: every mutation of this map is rejected unless that generation is
        # still current, so a read that straddled a turn/output boundary can never
        # seed (or confirm) a candidate — see _fresh_capture_pane_status.
        self._pending_stale_capture: Dict[str, Tuple[TerminalStatus, float, int]] = {}
        # Per-terminal turn/output generation. Bumped under the lock by
        # notify_input_sent (a new turn began) and by _process_chunk (real output
        # arrived). A capture-pane verdict is only applied if the generation it was
        # sampled under is still current at apply time: checking _last_status alone
        # cannot see a new turn, because notify_input_sent deliberately leaves
        # _last_status == PROCESSING while arming the revert — a stale ready verdict
        # applied across that boundary would consume the arm and latch-block the new
        # turn's genuine PROCESSING.
        self._capture_generation: Dict[str, int] = {}
        # --- pyte rendered-screen detection state (only used when CAO_PYTE_STATUS
        # is on AND the provider opts in via supports_screen_detection) ---
        # Per-terminal pyte Screen+Stream that composites the raw byte stream
        # into a rendered viewport. Detection runs against the composited screen
        # on two edges only — rising (output resumed) and quiescence (output
        # stopped for PYTE_QUIESCENCE_DELAY_S) — never mid-burst, which is what
        # keeps status flap-free.
        self._screens: Dict[str, Tuple[object, object]] = {}
        # Perf: incremental renderer per terminal (only pyte-dirty rows are
        # re-rendered). Always read screens through _screen_rows_locked().
        self._screen_render: Dict[str, ScreenRenderCache] = {}
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
        # F516 D4: per-terminal geometric backoff step for detection retries
        # (1→2→4→8s cap, max 6 per silence episode). Reset edge is the chunk
        # path _schedule_screen_detection; purged in clear_terminal.
        self._retry_backoff_step: Dict[str, int] = {}
        # D15: status-stream loss recovery state. A status publish snapshots
        # the event-bus drop level; the independent pane-sampler tick can then
        # force a re-derivation when that level changes.
        self._drop_seq_seen: Dict[str, int] = {}
        self._last_publish_monotonic: Dict[str, float] = {}
        self._last_level_resync_monotonic: Dict[str, float] = {}
        # F579 D17: per-terminal count of CONSECUTIVE non-PROCESSING status
        # publishes. Feeds the children-ledger publish-time reconcile conjunct
        # (a lost SubagentStop cannot pin a seat at delegating past K ticks).
        self._non_processing_streak: Dict[str, int] = {}
        self._status_fusion_reason: Dict[str, str] = {}
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
        self._dispatch_mutexes: Dict[str, threading.Lock] = {}
        self._dispatch_next_gen: Dict[str, int] = {}
        self._active_dispatch_epoch: Dict[str, int] = {}
        self._dispatch_states: Dict[tuple[str, int], tuple[dict, int | None]] = {}
        self._dispatch_provider_overrides: Dict[str, object] = {}
        self._dispatch_providers: Dict[tuple[str, int], object] = {}
        self._dispatch_consumed: set[tuple[str, int]] = set()
        self._native_event_gen_accessor = None
        self._settlements: Dict[tuple[str, str], object] = {}
        self._settlement_tasks: set[asyncio.Task] = set()
        self._latest_native_request: Dict[str, NativePublishRequest] = {}
        self._dispatch_had_native: set[tuple[str, int]] = set()
        # --- unregister / quarantine state (B5, f100) ---
        self._consecutive_errors: Dict[str, int] = {}
        self._quarantined: set[str] = set()
        # --- ghost-terminal tolerance (F360, issue #215) ---
        # Consecutive "Terminal ... not found in database" misses per terminal
        # id from provider_manager.get_provider(). The first misses are
        # tolerated (the row may not be committed yet during creation, even
        # under lock contention); after GHOST_DROP_MISSES consecutive misses
        # the ghost id is dropped from the watch state with a single warning
        # instead of raising on every output chunk forever.
        self._provider_not_found_count: Dict[str, int] = {}
        self._dropped_not_found: set[str] = set()

        # F611 (#467): condition detection state. `_last_condition` is the live
        # fleet-field projection (label or None), read by terminal_service at
        # egress exactly like fused status — SEPARATE from `_last_status`, never
        # feeds fuse_status (D1/AC2). `_condition_delivery` is the ONE fan-out
        # seam (D4); its sinks are wired lazily to avoid import cycles.
        self._last_condition: Dict[str, Optional[str]] = {}
        self._condition_delivery: Optional[Any] = None

    @property
    def receiver_state_store(self) -> ReceiverStateStore:
        """Return this monitor's process-local observation store."""

        return self._receiver_state_store

    def set_native_event_gen_accessor(self, accessor) -> None:
        self._native_event_gen_accessor = accessor

    def bind_dispatch_provider(self, terminal_id: str, provider: object | None) -> None:
        """Bind the provider already resolved by the guarded send seam."""
        if provider is not None:
            with self._lock:
                self._dispatch_provider_overrides[terminal_id] = provider

    def begin_dispatch(self, terminal_id: str) -> DispatchTxn:
        mutex = self._dispatch_mutexes.setdefault(terminal_id, threading.Lock())
        mutex.acquire()
        try:
            begun = time.monotonic()
            with self._lock:
                provider = self._dispatch_provider_overrides.pop(terminal_id, None)
                if provider is None:
                    try:
                        provider = provider_manager.get_provider(terminal_id)
                    except Exception:
                        provider = None
                next_gen = self._dispatch_next_gen.get(terminal_id, 0) + 1
                self._dispatch_next_gen[terminal_id] = next_gen
                prior = self._active_dispatch_epoch.get(terminal_id, 0)
                snapshot = {}
                if provider is not None:
                    # Some source-compatible provider fixtures bypass
                    # BaseProvider.__init__; give them the shared flush state
                    # before invoking the inherited transaction helpers.
                    if not hasattr(provider, "_flush_lock"):
                        provider._flush_lock = threading.RLock()
                    for field, default in (
                        ("_task_dispatched", False),
                        ("_last_dispatch_time", 0.0),
                        ("_done_first_detected", 0.0),
                        ("_idle_first_detected", 0.0),
                    ):
                        if not hasattr(provider, field):
                            setattr(provider, field, default)
                    with provider._flush_lock:
                        snapshot = provider._arm_dispatch_locked(begun)
                self._active_dispatch_epoch[terminal_id] = next_gen
                self._dispatch_states[(terminal_id, next_gen)] = (snapshot, prior)
                if provider is not None:
                    self._dispatch_providers[(terminal_id, next_gen)] = provider
            return DispatchTxn(terminal_id, next_gen, begun)
        except BaseException:
            mutex.release()
            raise

    def _finish_dispatch(self, txn: DispatchTxn) -> bool:
        key = (txn.terminal_id, txn.dispatch_gen)
        with self._lock:
            if (
                key in self._dispatch_consumed
                or self._active_dispatch_epoch.get(txn.terminal_id) != txn.dispatch_gen
            ):
                return False
            self._dispatch_consumed.add(key)
            self._dispatch_states.pop(key, None)
            return True

    def commit_dispatch(self, txn: DispatchTxn) -> None:
        consumed = False
        try:
            provider = self._dispatch_providers.get((txn.terminal_id, txn.dispatch_gen))
            if not self._finish_dispatch(txn):
                return
            consumed = True
            with self._lock:
                if provider is not None:
                    with provider._flush_lock:
                        provider._commit_dispatch_locked(time.monotonic())
                self._active_dispatch_epoch[txn.terminal_id] = txn.dispatch_gen
                if (txn.terminal_id, txn.dispatch_gen) in self._dispatch_had_native:
                    self._repair_dispatch_locked(txn.terminal_id, txn.dispatch_gen)
        finally:
            if consumed:
                self._dispatch_providers.pop((txn.terminal_id, txn.dispatch_gen), None)
                self._dispatch_mutexes[txn.terminal_id].release()

    def abort_dispatch(self, txn: DispatchTxn) -> None:
        consumed = False
        try:
            key = (txn.terminal_id, txn.dispatch_gen)
            with self._lock:
                if (
                    key in self._dispatch_consumed
                    or self._active_dispatch_epoch.get(txn.terminal_id) != txn.dispatch_gen
                ):
                    return
                snapshot, prior = self._dispatch_states.pop(key, ({}, None))
                self._dispatch_consumed.add(key)
                consumed = True
                provider = self._dispatch_providers.pop(key, None)
                if provider is not None:
                    with provider._flush_lock:
                        provider._restore_dispatch_locked(snapshot)
                self._active_dispatch_epoch[txn.terminal_id] = int(prior or 0)
                if key in self._dispatch_had_native:
                    self._repair_dispatch_locked(txn.terminal_id, int(prior or 0))
        finally:
            if consumed:
                self._dispatch_mutexes[txn.terminal_id].release()

    def active_dispatch_epoch(self, terminal_id: str) -> int | None:
        with self._lock:
            return self._active_dispatch_epoch.get(terminal_id)

    def _receiver_key_locked(self, terminal_id: str):
        from cli_agent_orchestrator.clients.database import get_terminal_metadata

        metadata = get_terminal_metadata(terminal_id)
        if metadata is None:
            return None
        return (terminal_id, int(metadata["lifecycle_generation"]), str(metadata["tmux_window"]))

    def _cancel_settlements_locked(self, terminal_id: str) -> None:
        for key in [key for key in self._settlements if key[0] == terminal_id]:
            entry = self._settlements.pop(key)
            entry.handle.cancel()

    def _repair_dispatch_locked(self, terminal_id: str, dispatch_gen: int) -> None:
        request = self._latest_native_request.get(terminal_id)
        if request is None:
            return
        self._cancel_settlements_locked(terminal_id)
        key = self._receiver_key_locked(terminal_id)
        if key is not None:
            self._receiver_state_store.invalidate(key)
        anchor = time.monotonic()
        proof = self.prove_terminal_identity(terminal_id, depth="marker")
        self.publish_native_observation(
            request,
            proof,
            anchor,
            SettlementFence(request.generation, dispatch_gen),
        )

    def _arm_settlement_locked(self, request: NativePublishRequest, deadline_mono: float) -> None:
        loop = self._loop or self._running_loop()
        if loop is None:
            return
        key = (request.terminal_id, request.pane_id)
        dispatch_gen = self._active_dispatch_epoch.get(request.terminal_id, 0)

        def install() -> None:
            with self._lock:
                old = self._settlements.pop(key, None)
                if old is not None:
                    old.handle.cancel()
                delay = max(0.0, deadline_mono + 0.25 - time.monotonic())
                handle = loop.call_later(
                    delay,
                    self._settlement_timer_fired,
                    key,
                    request,
                    request.generation,
                    dispatch_gen,
                )
                self._settlements[key] = SettlementEntry(
                    handle, request, request.generation, dispatch_gen
                )

        if self._running_loop() is loop:
            install()
        else:
            loop.call_soon_threadsafe(install)

    def _settlement_timer_fired(
        self,
        key: tuple[str, str],
        request: NativePublishRequest,
        native_event_gen: int,
        dispatch_gen: int,
    ) -> None:
        loop = self._loop or self._running_loop()
        if loop is None:
            return

        async def settle() -> None:
            try:
                await asyncio.to_thread(
                    self._run_settlement,
                    request,
                    native_event_gen,
                    dispatch_gen,
                )
            finally:
                with self._lock:
                    entry = self._settlements.get(key)
                    if (
                        entry is not None
                        and entry.native_event_gen == native_event_gen
                        and entry.dispatch_gen == dispatch_gen
                    ):
                        self._settlements.pop(key, None)

        task = loop.create_task(settle())
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)

    def _run_settlement(
        self, request: NativePublishRequest, native_event_gen: int, dispatch_gen: int
    ) -> None:
        current_gen = (
            self._native_event_gen_accessor(request.terminal_id, request.pane_id)
            if self._native_event_gen_accessor is not None
            else request.generation
        )
        with self._lock:
            if (
                current_gen != native_event_gen
                or self._active_dispatch_epoch.get(request.terminal_id, 0) != dispatch_gen
            ):
                return
        anchor = time.monotonic()
        proof = self.prove_terminal_identity(request.terminal_id, depth="marker")
        self.publish_native_observation(
            request,
            proof,
            anchor,
            SettlementFence(native_event_gen, dispatch_gen),
        )

    def publish_native_observation(
        self,
        request: NativePublishRequest,
        proof: IdentityProof,
        anchor_mono: float,
        settlement_fence: SettlementFence | None = None,
    ) -> None:
        if proof.terminal_id != request.terminal_id:
            raise ValueError("identity proof terminal mismatch")
        proof_age = proof.proven_at_mono - anchor_mono
        if proof_age < 0 or proof_age > PROOF_MAX_AGE_S:
            raise ValueError("native identity proof outside event age bound")
        with self._lock:
            if settlement_fence is not None:
                current_gen = (
                    self._native_event_gen_accessor(request.terminal_id, request.pane_id)
                    if self._native_event_gen_accessor is not None
                    else request.generation
                )
                if (
                    current_gen != settlement_fence.native_event_gen
                    or self._active_dispatch_epoch.get(request.terminal_id, 0)
                    != settlement_fence.dispatch_gen
                ):
                    return
            provider = provider_manager.get_provider(request.terminal_id)
            if provider is None:
                return
            with provider._flush_lock:
                mapped = map_native_status(request.agent_status)
                resolution = provider.resolve_native_status(mapped)
                status = resolution.status or TerminalStatus.UNKNOWN
                self._latest_native_request[request.terminal_id] = request
                dispatch_epoch = self._active_dispatch_epoch.get(request.terminal_id, 0)
                if dispatch_epoch:
                    self._dispatch_had_native.add((request.terminal_id, dispatch_epoch))
                try:
                    from cli_agent_orchestrator.clients.database import get_terminal_metadata

                    metadata = get_terminal_metadata(request.terminal_id)
                    if metadata is None:
                        return
                    self._publish_observation(
                        request.terminal_id,
                        latched_status=status,
                        pass_outcome="native",
                        frame_source="native",
                        metadata=metadata,
                        freshness_proof=FreshnessProof(
                            "identity_ok" if proof.failure is None else "identity_failed",
                            proof.failure,
                        ),
                        captured_at_mono=request.received_at_mono,
                        origin="native",
                        native_evidence=NativeEvidence(
                            request.agent_status,
                            status,
                            request.generation,
                            request.received_at_mono,
                        ),
                        slot="incremental",
                    )
                    if resolution.settlement_deadline_mono is not None:
                        self._arm_settlement_locked(request, resolution.settlement_deadline_mono)
                except Exception:
                    self._log_receiver_publish_failure(request.terminal_id)

    def publish_native_poll(
        self, terminal_id: str, pane_id: str, fetch, fetched_at_mono: float, proof: IdentityProof
    ) -> FreshToken:
        if proof.terminal_id != terminal_id:
            raise ValueError("identity proof terminal mismatch")
        if (
            fetched_at_mono - proof.proven_at_mono < 0
            or fetched_at_mono - proof.proven_at_mono > PROOF_MAX_AGE_S
        ):
            raise ValueError("native poll identity proof outside capture age bound")
        with self._lock:
            token = self._receiver_state_store.mint_token(
                terminal_id, self._epoch_locked(terminal_id)
            )
            if fetch.failure_cause is not None or fetch.status is None:
                return token
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                return token
            with provider._flush_lock:
                resolution = provider.resolve_native_status(fetch.status)
            status = resolution.status or TerminalStatus.UNKNOWN
            try:
                from cli_agent_orchestrator.clients.database import get_terminal_metadata

                metadata = get_terminal_metadata(terminal_id)
                if metadata is None:
                    return token
                epoch = self._epoch_locked(terminal_id)
                seq = self._observation_seq.get(terminal_id, 0)
                evidence = NativeEvidence(
                    fetch.agent_status or "",
                    status,
                    int(
                        self._native_event_gen_accessor(terminal_id, pane_id)
                        if self._native_event_gen_accessor
                        else 0
                    ),
                    fetched_at_mono,
                )
                request = NativePublishRequest(
                    terminal_id,
                    pane_id,
                    evidence.native_event_gen,
                    fetch.agent_status or "",
                    fetched_at_mono,
                )
                self._latest_native_request[terminal_id] = request
                dispatch_epoch = self._active_dispatch_epoch.get(terminal_id, 0)
                if dispatch_epoch:
                    self._dispatch_had_native.add((terminal_id, dispatch_epoch))
                kwargs = dict(
                    terminal_id=terminal_id,
                    lifecycle_generation=int(metadata["lifecycle_generation"]),
                    window_identity=str(metadata["tmux_window"]),
                    observation_epoch=epoch,
                    observation_sequence=seq,
                    provider=str(metadata["provider"]),
                    frame_source="native",
                    captured_at_mono=fetched_at_mono,
                    frame_hash=None,
                    latched_status=status,
                    pass_outcome="native",
                    freshness_proof=FreshnessProof(
                        "identity_ok" if proof.failure is None else "identity_failed", proof.failure
                    ),
                    origin="native_poll",
                    native_evidence=evidence,
                )
                fresh = ReceiverState(**kwargs)
                incremental = ReceiverState(**kwargs)
                self._receiver_state_store.publish_native_pair(fresh, incremental, token)
                if resolution.settlement_deadline_mono is not None:
                    self._arm_settlement_locked(request, resolution.settlement_deadline_mono)
            except Exception:
                self._log_receiver_publish_failure(terminal_id)
            return token

    def _publish_observation(
        self,
        terminal_id: str,
        *,
        latched_status: TerminalStatus,
        pass_outcome: PassOutcome,
        frame_source: Literal["incremental", "fresh_capture", "native"],
        metadata: dict[str, Any] | None = None,
        freshness_proof: FreshnessProof | None = None,
        captured_at_mono: float | None = None,
        raw_classification: object | None = None,
        probe_evidence: ProbeEvidence | None = None,
        origin: Literal["incremental", "probe", "forced", "native", "native_poll"] | None = None,
        fresh_token: FreshToken | None = None,
        native_evidence: NativeEvidence | None = None,
        slot: Literal["incremental", "fresh"] | None = None,
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
                captured_at_mono=(
                    time.monotonic() if captured_at_mono is None else captured_at_mono
                ),
                frame_hash=None,
                latched_status=latched_status,
                pass_outcome=pass_outcome,
                freshness_proof=freshness_proof or FreshnessProof("not_probed"),
                origin=(
                    origin
                    if origin is not None
                    else "forced" if pass_outcome == "forced" else "incremental"
                ),
                raw_classification=raw_classification,
                probe_evidence=probe_evidence,
                native_evidence=native_evidence,
            ),
            fresh_token=fresh_token,
            slot=slot,
        )

    @staticmethod
    def _signal_emitting(provider: object) -> bool:
        from cli_agent_orchestrator.providers.base import BaseProvider

        return (
            getattr(type(provider), "emit_screen_signals", BaseProvider.emit_screen_signals)
            is not BaseProvider.emit_screen_signals
        )

    def _prior_signals(
        self, terminal_id: str, metadata: dict[str, Any], *, prefer_fresh: bool
    ) -> tuple[object, ...]:
        prior = self._receiver_state_store.prior_classification(
            (
                terminal_id,
                int(metadata["lifecycle_generation"]),
                str(metadata["tmux_window"]),
            ),
            prefer_fresh=prefer_fresh,
        )
        return () if prior is None else prior.signals

    def _classify_frame(
        self,
        terminal_id: str,
        provider: object,
        rows: List[str],
        metadata: dict[str, Any],
        *,
        prefer_fresh: bool,
    ) -> tuple[TerminalStatus, object | None, object]:
        from cli_agent_orchestrator.providers.screen_classification import (
            ScreenClassification,
            ScreenClassificationResult,
            screen_classification_result,
        )

        if self._signal_emitting(provider):
            signals = provider.emit_screen_signals(rows)
            result = screen_classification_result(
                signals,
                self._prior_signals(terminal_id, metadata, prefer_fresh=prefer_fresh),
                provider.capabilities.liveness_anchor,
            )
            return result.status, result, result
        status = provider.get_status_from_screen(rows)
        if status == TerminalStatus.RENDER_UNCERTAIN:
            status = TerminalStatus.UNKNOWN
        hook_result = ScreenClassificationResult(
            ScreenClassification(status, "none", None, None), ()
        )
        return status, None, hook_result

    def prove_terminal_identity(
        self, terminal_id: str, depth: Literal["marker", "live"] = "live"
    ) -> IdentityProof:
        """Return a closed identity proof for the terminal's current route."""

        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import get_terminal_metadata

        try:
            metadata = get_terminal_metadata(terminal_id)
            if not metadata:
                return IdentityProof(
                    terminal_id, "pane_readback", time.monotonic(), "metadata_missing"
                )
            backend = get_backend()
            if getattr(backend, "supports_identity_readback", False) is not True:
                if depth == "marker":
                    from cli_agent_orchestrator.services.herdr_inbox_registry import (
                        get_herdr_inbox_service,
                    )

                    service = get_herdr_inbox_service()
                    marker = (
                        service.read_identity_marker(terminal_id) if service is not None else None
                    )
                    failure = (
                        None if marker is not None else "native_identity_failure:marker_unavailable"
                    )
                    return IdentityProof(terminal_id, "native", time.monotonic(), failure)
                result = backend.read_native_identity(
                    terminal_id,
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    metadata.get("provider", "unknown"),
                )
                verdict = getattr(result, "verdict", None)
                failure = (
                    None
                    if verdict == "match" or verdict not in {"mismatch", "unavailable"}
                    else f"native_identity_{verdict}"
                )
                return IdentityProof(terminal_id, "native", time.monotonic(), failure)
            from cli_agent_orchestrator.services.pane_identity_service import (
                pane_identity_failure,
            )

            failure = pane_identity_failure(terminal_id, metadata, backend)
            return IdentityProof(terminal_id, "pane_readback", time.monotonic(), failure)
        except Exception as exc:
            return IdentityProof(
                terminal_id,
                "pane_readback",
                time.monotonic(),
                f"identity_exception:{type(exc).__name__}",
            )

    def publish_fresh_observation(
        self,
        terminal_id: str,
        frame: List[str],
        frame_captured_at_mono: float,
        classification: object,
        frame_source: ScreenProbeFrameSource,
        proof: IdentityProof,
    ) -> FreshToken:
        """Publish one proven capture while containing only publication faults."""

        if proof.terminal_id != terminal_id:
            raise ValueError("identity proof terminal mismatch")
        proof_age = frame_captured_at_mono - proof.proven_at_mono
        if proof_age < 0 or proof_age > PROOF_MAX_AGE_S:
            raise ValueError("identity proof outside capture age bound")
        with self._lock:
            epoch = self._epoch_locked(terminal_id)
        token = self._receiver_state_store.mint_token(terminal_id, epoch)
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            metadata = get_terminal_metadata(terminal_id)
            if metadata is None:
                raise LookupError(f"terminal metadata unavailable for {terminal_id}")
            raw = (
                classification
                if self._signal_emitting(provider_manager.get_provider(terminal_id))
                else None
            )
            status = classification.status
            with self._lock:
                self._publish_observation(
                    terminal_id,
                    latched_status=status,
                    pass_outcome="probe",
                    frame_source=frame_source,
                    metadata=metadata,
                    freshness_proof=FreshnessProof(
                        "identity_ok" if proof.failure is None else "identity_failed",
                        proof.failure,
                    ),
                    captured_at_mono=frame_captured_at_mono,
                    raw_classification=raw,
                    origin="probe",
                    fresh_token=token,
                )
        except Exception:
            try:
                self._log_receiver_publish_failure(terminal_id)
            except Exception:
                pass
        return token

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
        - RAW (default, every provider): regex over the rolling state buffer
          (``state_buffer_max`` bytes, server setting), run on every chunk.
          Unchanged legacy behavior.
        - SCREEN (pyte): when CAO_PYTE_STATUS is on AND the provider opts in
          via supports_screen_detection, the chunk is fed to a per-terminal
          pyte screen and detection runs only on the rising edge (output
          resumed) and at quiescence (output stopped) — see
          _schedule_screen_detection.
        """
        # F360 (#215): a ghost terminal id (DB row deleted, e.g. by a failed
        # create that unwound its registration, while buffered output events
        # still arrive) makes get_provider raise ValueError on every chunk.
        # Tolerate the first misses (creation window, incl. lock-contention
        # commit delay), then drop the ghost from the watch state with one
        # warning instead of erroring forever.
        if terminal_id in self._dropped_not_found:
            return
        try:
            provider = provider_manager.get_provider(terminal_id)
        except ValueError as exc:
            if "not found in database" not in str(exc):
                raise
            misses = self._provider_not_found_count.get(terminal_id, 0) + 1
            self._provider_not_found_count[terminal_id] = misses
            if misses >= GHOST_DROP_MISSES:
                self._dropped_not_found.add(terminal_id)
                logger.warning(
                    "StatusMonitor: terminal %s not found in database %d times; "
                    "dropping ghost terminal from watch state (issue #215)",
                    terminal_id,
                    misses,
                )
                self.clear_terminal(terminal_id)
            else:
                logger.debug(
                    "StatusMonitor: terminal %s not found in database yet " "(miss %d); tolerating",
                    terminal_id,
                    misses,
                )
            return
        if terminal_id in self._provider_not_found_count:
            self._provider_not_found_count.pop(terminal_id, None)
        use_screen = (
            CAO_PYTE_STATUS
            and provider is not None
            and getattr(provider, "supports_screen_detection", False)
        )
        state_buffer_max = get_server_settings()["state_buffer_max"]

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
            if len(buffer) > state_buffer_max:
                buffer = buffer[-state_buffer_max:]
            self._buffers[terminal_id] = buffer
            chunk_seq = self._bump_chunk_seq_locked(terminal_id)
            self._fifo_frame_seq[terminal_id] = self._fifo_frame_seq.get(terminal_id, 0) + 1
            # Real new output just landed — the stale-PROCESSING quiet gate keys off this
            # (see STALE_PROCESSING_BUFFER_QUIET_S). It also advances the capture
            # generation and kills any pending capture candidate: output arriving
            # means the terminal is demonstrably alive, so a ready verdict sampled
            # before this chunk no longer describes it and must not be confirmable
            # by a later read.
            self._buffer_changed_at[terminal_id] = time.monotonic()
            self._capture_generation[terminal_id] = self._capture_generation.get(terminal_id, 0) + 1
            self._pending_stale_capture.pop(terminal_id, None)
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
        raw_classification: object | None = None,
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
                            if pass_source == "inline":
                                self._status_fusion_reason.pop(terminal_id, None)
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
                    evidence_kwargs = (
                        {"raw_classification": raw_classification}
                        if pass_source != "forced" and raw_classification is not None
                        else {}
                    )
                    self._publish_observation(
                        terminal_id,
                        latched_status=self._last_status.get(terminal_id, TerminalStatus.UNKNOWN),
                        pass_outcome=pass_outcome,
                        frame_source="incremental",
                        metadata=observation_metadata,
                        **evidence_kwargs,
                    )
                except Exception:
                    try:
                        self._log_receiver_publish_failure(terminal_id)
                    except Exception:
                        pass

        # Publish outside the lock — subscribers must never be able to
        # re-enter StatusMonitor while the latch state is mid-update.
        if publish_external:
            with self._lock:
                self._drop_seq_seen[terminal_id] = bus.get_drop_seq(terminal_id)
                self._last_publish_monotonic[terminal_id] = _clock()
                fusion_reason = self._status_fusion_reason.get(terminal_id)
            payload = {"status": detected.value}
            if fusion_reason is not None:
                payload["fusion_reason"] = fusion_reason
            bus.publish(f"terminal.{terminal_id}.status", payload)
            __import__(f"{__package__}.auto_responder", fromlist=["auto_responder"]).auto_responder.record_published_status(terminal_id, detected)  # fmt: skip
            # F579 D17: publish-time children-ledger reconcile. Track the
            # consecutive non-PROCESSING streak and drop stranded ledger entries
            # once a lost SubagentStop has held the seat non-PROCESSING for K
            # ticks; also runs the age-out on every publish (the firing-15 fix).
            try:
                if detected == TerminalStatus.PROCESSING:
                    self._non_processing_streak[terminal_id] = 0
                else:
                    self._non_processing_streak[terminal_id] = (
                        self._non_processing_streak.get(terminal_id, 0) + 1
                    )
                from cli_agent_orchestrator.clients.database import (
                    reconcile_children_on_publish,
                )

                reconcile_children_on_publish(
                    terminal_id,
                    detected.value,
                    self._non_processing_streak.get(terminal_id, 0),
                    _CHILDREN_RECONCILE_K_TICKS,
                )
            except Exception:
                logger.debug("children reconcile on publish failed", exc_info=True)
            if screen_spinner_override is not None:
                logger.info("screen spinner override: %s→processing", screen_spinner_override.value)
            logger.info(f"Terminal {terminal_id} status changed: {detected.value}")

            # F611 (#467): a published status transition is EXACTLY the "one
            # event per terminal transition" seam (§3/D4). Classify the current
            # pane for a provider condition and fan the ONE result to the three
            # surfaces (fleet field / supervisor inbox / CLI). SEPARATE from the
            # status publish above — never touches fuse_status (D1/AC2). Runs
            # off the lock; any failure is swallowed inside the helper.
            try:
                with self._lock:
                    cond_buffer = self._buffers.get(terminal_id, "")
                cond_provider = None
                try:
                    cond_provider = provider_manager.get_provider(terminal_id)
                except Exception:
                    cond_provider = None
                self._classify_and_deliver_condition(terminal_id, cond_provider, cond_buffer)
            except Exception:
                logger.debug("condition detection at transition failed", exc_info=True)

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

    def _screen_rows_locked(self, terminal_id: str, screen: object) -> List[str]:
        """``list(screen.display)`` via the per-terminal incremental cache. Caller holds _lock."""
        cache = self._screen_render.get(terminal_id)
        if cache is None:
            cache = ScreenRenderCache()
            self._screen_render[terminal_id] = cache
        return cache.rows(screen)

    def _detect_screen(self, terminal_id: str, provider) -> TerminalStatus:
        """Detect status from the terminal's composited pyte screen."""
        detected, _trusted_busy, _raw = self._detect_screen_with_trust(terminal_id, provider)
        return detected

    def _detect_screen_with_trust(
        self, terminal_id: str, provider
    ) -> tuple[TerminalStatus, bool, object | None]:
        """Detect screen status plus whether PROCESSING is a trusted screen read."""
        fallback_buffer: Optional[str] = None
        with self._lock:
            scr = self._screens.get(terminal_id)
            buffer = self._buffers.get(terminal_id, "")
            try:
                lines: List[str] = (
                    self._screen_rows_locked(terminal_id, scr[0]) if scr is not None else []
                )
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
                return TerminalStatus.UNKNOWN, False, None
            try:
                return provider.get_status(fallback_buffer), False, None
            except Exception:
                logger.exception("Error detecting fallback status for %s", terminal_id)
                return TerminalStatus.UNKNOWN, False, None
        if not lines or provider is None:
            return TerminalStatus.UNKNOWN, False, None

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
                return override, False, None
        except Exception:
            logger.exception("Error in auto-responder for %s", terminal_id)

        try:
            legacy_status = provider.get_status_from_screen(lines)
            raw = None
            if self._signal_emitting(provider):
                try:
                    from cli_agent_orchestrator.clients.database import get_terminal_metadata

                    metadata = get_terminal_metadata(terminal_id)
                    if metadata is not None:
                        _status, raw, _hooks = self._classify_frame(
                            terminal_id,
                            provider,
                            lines,
                            metadata,
                            prefer_fresh=False,
                        )
                except Exception:
                    logger.exception(
                        "Error producing incremental receiver evidence for %s", terminal_id
                    )
            return legacy_status, True, raw
        except Exception:
            # Full traceback: screen detectors are new and can trip on
            # unexpected TUI frames; the stack makes such regressions debuggable.
            logger.exception(f"Error detecting screen status for {terminal_id}")
            return TerminalStatus.UNKNOWN, False, None

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
            detected, trusted_busy, raw = self._detect_screen_with_trust(terminal_id, provider)
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=chunk_seq,
                raw_classification=raw,
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
            # F516 D4 reset edge: a real chunk snaps the detection-retry backoff
            # back to zero (Decepticon #662 snap-back on first output).
            self._retry_backoff_step.pop(terminal_id, None)
        self._cancel_quiesce_handle(handle)

        if not was_bursting:
            detected, trusted_busy, raw = self._detect_screen_with_trust(terminal_id, provider)
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=chunk_seq,
                raw_classification=raw,
            )
        elif armed or last_status in _STICKY_READY_STATUSES or last_status is None:
            detected, trusted_busy, raw = self._detect_screen_with_trust(terminal_id, provider)
            if detected == TerminalStatus.PROCESSING:
                self._apply_detection(
                    terminal_id,
                    detected,
                    trusted_busy=trusted_busy,
                    expected_seq=chunk_seq,
                    raw_classification=raw,
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
            detected, trusted_busy, raw = await asyncio.to_thread(
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
                raw_classification=raw,
            )

        loop = self._loop or self._running_loop()
        if loop is None:
            detected, trusted_busy, raw = self._detect_screen_with_trust(terminal_id, provider)
            self._apply_detection(
                terminal_id,
                detected,
                trusted_busy=trusted_busy,
                expected_seq=expected_seq,
                raw_classification=raw,
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

    def _arm_quiesce_timer(
        self, loop, terminal_id: str, callback, *cb_args, delay: float | None = None
    ) -> None:
        """Schedule the quiescence timer on ``loop`` from any thread.

        ``loop.call_later`` is not thread-safe and this may run on a worker
        thread, so marshal the scheduling onto the loop with
        ``call_soon_threadsafe``. The resulting TimerHandle is stored from
        inside the marshaled closure (still on the loop thread) so cancel
        marshaling in ``_cancel_quiesce_handle`` stays correct. ``cb_args``
        are extra positional args passed to ``callback`` after ``terminal_id``.

        F516 D4: ``delay`` parameterizes the timer interval on this shared helper
        (default ``PYTE_QUIESCENCE_DELAY_S`` preserves every existing caller);
        ``schedule_detection_retry`` passes the geometric backoff delay through
        the SAME single ``_quiesce_handle`` slot with cancel-prior semantics.
        """
        fire_delay = PYTE_QUIESCENCE_DELAY_S if delay is None else delay

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
                handle = loop.call_later(fire_delay, callback, terminal_id, *cb_args)
                self._quiesce_handle[terminal_id] = handle

        try:
            loop.call_soon_threadsafe(_arm)
        except RuntimeError:
            # Loop closed during shutdown — quiescence re-detect is moot.
            pass

    def schedule_detection_retry(self, terminal_id: str, delay_s: float | None = None) -> None:
        """F516 D4: re-arm a screen-detection tick after a vetoed/unmatched eval.

        The per-silence-episode eval already exists; this adds the RE-TRIGGER
        (r2-B5). It occupies the SAME single ``_quiesce_handle`` slot with
        cancel-prior semantics (a real chunk re-arms the slot at
        ``PYTE_QUIESCENCE_DELAY_S`` and, via the chunk-path reset edge, snaps the
        backoff to zero). The delay follows a geometric backoff (1→2→4→8s cap,
        max 6 requests per silence episode) unless ``delay_s`` is explicit. It
        snapshots ``_chunk_seq`` INTERNALLY, resolves the detection provider
        ITSELF via ``provider_manager.get_provider``, and is a NO-OP when the
        provider is gone or no loop is captured — NEVER an immediate detect (an
        immediate fallback would burn all six backoff steps at once in the
        loop-less replay harness).

        Lock-order law (F522 #377): arms a timer under the monitor lock only and
        is called as a LEAF — no responder-internal lock may be held across it,
        and no status_monitor→auto_responder→status_monitor re-entry.
        """
        try:
            loop = self._loop or self._running_loop()
            if loop is None:
                return  # loop-less: NO-OP, never an immediate detect
            with self._lock:
                if delay_s is None:
                    step = self._retry_backoff_step.get(terminal_id, 0)
                    if step >= 6:
                        return  # max 6 retries per silence episode
                    delay_s = float(min(2**step, 8))
                    self._retry_backoff_step[terminal_id] = step + 1
                chunk_seq = self._chunk_seq.get(terminal_id, 0)
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                return  # provider gone: NO-OP (r3-S5)
            if CAO_PYTE_STATUS and getattr(provider, "supports_screen_detection", False):
                self._arm_quiesce_timer(
                    loop,
                    terminal_id,
                    self._on_screen_quiescent,
                    provider,
                    chunk_seq,
                    delay=delay_s,
                )
            else:
                self._arm_quiesce_timer(
                    loop,
                    terminal_id,
                    self._on_raw_quiescent,
                    chunk_seq,
                    delay=delay_s,
                )
        except Exception:
            # Precedent status_monitor.py:1292-1299 — a retry-request failure must
            # never raise into or alter the caller's on_screen path.
            logger.debug(
                "status_monitor: schedule_detection_retry failed for %s",
                terminal_id,
                exc_info=True,
            )

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

    def notify_input_sent(self, terminal_id: str, *, assume_processing: bool = False) -> None:
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
            # A new turn is starting: whatever ready state a stale-PROCESSING capture saw
            # before this input no longer describes the terminal. Left armed, that candidate
            # could be "confirmed" by a single post-input read and latch ready against the
            # new turn's genuine PROCESSING. The generation bump additionally invalidates
            # any capture verdict already CONFIRMED but not yet applied — an in-flight
            # get_status() that sampled the pane before this input must not stamp its
            # stale verdict over the new turn (and consume the revert arm just set).
            self._pending_stale_capture.pop(terminal_id, None)
            self._capture_generation[terminal_id] = self._capture_generation.get(terminal_id, 0) + 1
            logger.info(
                "Terminal %s input sent generation: input_gen=%s processing_gen=%s "
                "status_gen=%s",
                terminal_id,
                self._input_gen[terminal_id],
                self._processing_gen.get(terminal_id, 0),
                self._status_gen.get(terminal_id, 0),
            )
            if assume_processing:
                self._apply_detection(terminal_id, TerminalStatus.PROCESSING)

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

    def get_condition(self, terminal_id: str) -> Optional[str]:
        """F611 (#467): return the live condition fleet label, or None.

        Read at egress by ``terminal_service.get_terminal`` to populate
        ``Terminal.condition`` — a SEPARATE projection from status fusion. Pure
        read; never invokes detection or captures. ``None`` = no condition.
        """
        with self._lock:
            return self._last_condition.get(terminal_id)

    def _get_condition_delivery(self) -> Any:
        """Lazily build the ONE condition-delivery seam with production sinks.

        Wired here (not at import) to avoid an import cycle: the inbox sink pulls
        in ``inbox_service``/``api`` layers that import back into providers. The
        three sinks PERFORM the §3 fan-out — fleet field, one supervisor inbox
        push, and the bus/CLI projection — from ONE event (D4).
        """
        if self._condition_delivery is not None:
            return self._condition_delivery
        from cli_agent_orchestrator.providers.condition import ConditionDelivery

        self._condition_delivery = ConditionDelivery(
            fleet_sink=self._condition_fleet_sink,
            inbox_sink=self._condition_inbox_sink,
            cli_sink=self._condition_cli_sink,
        )
        return self._condition_delivery

    def _condition_fleet_sink(self, terminal_id: str, label: Optional[str]) -> None:
        """§3 surface 1: set the live fleet ``condition`` field (or clear it)."""
        with self._lock:
            if label is None:
                self._last_condition.pop(terminal_id, None)
            else:
                self._last_condition[terminal_id] = label

    def _condition_inbox_sink(self, terminal_id: str, cond: Any) -> None:
        """§3 surface 2: ONE structured supervisor inbox push on the transition.

        Routes the typed event to the terminal's recorded caller (the same
        direct-terminal enqueue the POST endpoint uses). Best-effort: a missing
        caller or enqueue failure never breaks the status transition.
        """
        caller_id = None
        try:
            from cli_agent_orchestrator.clients.database import get_terminal_metadata

            meta = get_terminal_metadata(terminal_id)
            if meta:
                caller_id = meta.get("caller_id")
        except Exception:
            caller_id = None
        if not caller_id:
            return  # no supervisor to notify; fleet field + CLI still carry it
        from cli_agent_orchestrator.clients.database import create_inbox_message
        from cli_agent_orchestrator.services.inbox_service import request_delivery

        create_inbox_message(terminal_id, caller_id, cond.render_event(terminal_id))
        try:
            request_delivery(caller_id)
        except Exception:
            pass  # deliver-on-next-idle is the fallback

    def _condition_cli_sink(self, terminal_id: str, cond: Any, label: str) -> None:
        """§3 surface 3: project the condition on the status bus for ``cao``.

        Publishes a distinct ``terminal.{id}.condition`` frame so the fleet TUI /
        ``cao fleet`` render the typed condition without opening the seat. This
        NEVER touches the ``terminal.{id}.status`` frame or fuse_status (D1).
        """
        try:
            from cli_agent_orchestrator.services.event_bus import bus

            bus.publish(
                f"terminal.{terminal_id}.condition",
                {
                    "condition": label,
                    "kind": cond.kind.value,
                    "subtype": cond.subtype,
                    "confidence": cond.confidence.value,
                },
            )
        except Exception:
            pass

    def _classify_and_deliver_condition(self, terminal_id: str, provider: Any, buffer: str) -> None:
        """F611 (#467): detection + ONE-event delivery at a status transition.

        Called from the published-transition seam in ``_apply_detection``. Runs
        the provider's ``classify_condition`` on the current pane buffer and
        hands the result to the ONE delivery seam, de-duped per dispatch epoch
        (D4). SEPARATE from status/fusion (D1); a failure here never disturbs the
        status publish that triggered it.
        """
        if provider is None:
            return
        classify = getattr(provider, "classify_condition", None)
        if classify is None:
            return
        try:
            cond = classify(buffer)
        except Exception:
            logger.debug("condition classify failed for %s", terminal_id, exc_info=True)
            return
        with self._lock:
            epoch = self._buffer_epochs.get(terminal_id, 0)
        try:
            self._get_condition_delivery().deliver(terminal_id, cond, epoch=epoch)
        except Exception:
            logger.debug("condition delivery failed for %s", terminal_id, exc_info=True)

    def get_published_status(self, terminal_id: str) -> Optional[TerminalStatus]:
        """Return the pre-fusion latched status, or None if never published.

        F506 (r8 R8-S2): the SET-edge published-status read for the pane-hold
        bound. This is the ONE public accessor that does NOT fuse — post-change
        every other public accessor (get_boundary_observation/get_raw_status/
        get_status) does, and the sampler must not call those: they fuse, and
        fusing inside the sampler would re-enter observe(). Returns the raw
        ``self._last_status`` entry under the lock.
        """
        with self._lock:
            return self._last_status.get(terminal_id)

    def fuse_status(
        self, terminal_id: str, status: Optional[TerminalStatus]
    ) -> Tuple[Optional[TerminalStatus], Optional[str]]:
        """Compose liveness/marker evidence onto one status read (F506, D2).

        PUBLIC and read-time: wraps every status egress consumed for admission,
        display, or parity. Acquires ``self._lock`` itself (RLock — safe from
        call sites already holding it, and several sit outside it). IDEMPOTENT
        BY RE-DERIVATION: a second pass recomputes the same status and the same
        reason given unchanged inputs (AC21). Pure on the read path — it only
        READS ``question_state`` and ``pane_liveness.peek`` (never captures, D2).

        Precondition (stated ONCE): the rules apply only when ``status is not
        None``; ``None ⇒ (None, None)`` — the none_behavior="none" pass-through
        (R4-S1). Ordered rules, first match wins:

        1. status is WAITING_USER_ANSWER ⇒ (status, "question_marker" if a marker
           is open else None). Additive-only on WAITING: may RAISE into it, never
           LOWER out of it — keeps the fusion from fighting the 16 producers and
           specifically auto_responder.force_status (D5).
        2. a question marker is open ⇒ (WAITING_USER_ANSWER, "question_marker").
        3a. status in {IDLE, COMPLETED, PROCESSING}, a usable sample exists,
            unchanged_count < K (eligibility first — a stable pane is never
            tagged, AC4). Then, in order (F568 D12d, R3-B3/B5): children_count>0
            ⇒ (published, "pane_delta_delegating"); busy_marker is False ⇒
            (published, "pane_delta_vetoed"); busy_marker in (None, True) and NOT
            pane_hold_expired ⇒ (PROCESSING, "pane_delta"). The "usable sample"
            guard IS the whole no-evidence rule (R3-B1). PROCESSING⇒PROCESSING is
            a status no-op that reproduces the reason.
        3b. same preconditions but pane_hold_expired ⇒ status UNCHANGED,
            "pane_delta_expired" (fusion_changed stays False — the caller sets
            it from the status delta). The expiry is a distinct outcome (R5-S3).
            With a children/marker veto the clock was cleared, so expiry cannot
            co-occur (AC-8).
        4. else ⇒ (status, None).
        """
        if status is None:
            return None, None

        with self._lock:
            try:
                from cli_agent_orchestrator.services.question_state import question_state

                marker_open = question_state.is_open(terminal_id)
            except Exception:
                marker_open = False

            # Rule 1: additive-only on an already-WAITING status.
            if status is TerminalStatus.WAITING_USER_ANSWER:
                return status, ("question_marker" if marker_open else None)

            # Rule 2: a marker raises any other status into WAITING.
            if marker_open:
                return TerminalStatus.WAITING_USER_ANSWER, "question_marker"

            # Rules 3a/3b: pane-delta downgrade, bounded by the hold clock.
            if status in (
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
                TerminalStatus.PROCESSING,
            ):
                try:
                    from cli_agent_orchestrator.services.pane_liveness import pane_liveness

                    observation = pane_liveness.peek(terminal_id)
                except Exception:
                    observation = None
                if observation is not None:
                    k = self._stable_samples()
                    # Eligibility first (R3-B3/B5): a usable sample AND an
                    # unstable pane, else fall through to rule 4 (reason None) —
                    # a stable pane is never tagged with any D12d reason (AC4).
                    if observation.unchanged_count < k:
                        # F568 D12d (2) ledger veto: children in flight ⇒ admit
                        # the published status tagged "pane_delta_delegating".
                        # No hold episode opens (nothing is withheld — this is
                        # how D12b's "bound never expires while children > 0" is
                        # realised). Checked BEFORE the marker so a spinner never
                        # overrides delegating (S2).
                        if observation.children_count > 0:
                            return status, "pane_delta_delegating"
                        # F568 D12d (3) marker veto: the seat's own TUI spinner
                        # is absent ⇒ admit the published status tagged
                        # "pane_delta_vetoed". _rule3a_would_downgrade returned
                        # False, so the hold clock was cleared (expiry cannot
                        # co-occur, AC-8).
                        if observation.busy_marker is False:
                            return status, "pane_delta_vetoed"
                        # F568 D12d (4) busy_marker in (None, True): existing
                        # outcome — PROCESSING/"pane_delta", or the expired admit.
                        if not observation.pane_hold_expired:
                            return TerminalStatus.PROCESSING, "pane_delta"
                        # 3b: the bound expired — admit the published status but
                        # tag it so the withhold is explicable (AC22 second arm).
                        return status, "pane_delta_expired"

            # Rule 4.
            return status, None

    @staticmethod
    def _stable_samples() -> int:
        from cli_agent_orchestrator.services.config_service import ConfigService

        try:
            return int(ConfigService.get("liveness.stable_samples", 3))
        except Exception:
            return 3

    def get_boundary_observation(self, terminal_id: str) -> BoundaryObservation:
        """Return one status/cycle snapshot sampled under the monitor lock."""
        with self._lock:
            published = self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)
            # F506: fuse at read time (D2). fuse_status re-acquires the RLock
            # (safe) and only READS question_state/pane_liveness — no capture.
            fused_status, fusion_reason = self.fuse_status(terminal_id, published)
            fusion_reason = self._status_fusion_reason.get(terminal_id) or fusion_reason
            if fused_status is None:
                fused_status = published
            # fusion_changed is set HERE from the status delta (R6-S5): the
            # fuse_status tuple contract is unchanged; this getter owns the flag.
            fusion_changed = fused_status is not published
            return BoundaryObservation(
                observation_epoch=self._epoch_locked(terminal_id),
                status=fused_status,
                status_gen=self._status_gen.get(terminal_id, 0),
                input_gen=self._input_gen.get(terminal_id, 0),
                seq=self._observation_seq.get(terminal_id, 0),
                last_non_ready_seq=self._last_non_ready_seq.get(terminal_id),
                last_ready_seq=self._last_ready_seq.get(terminal_id),
                fusion_reason=fusion_reason,
                fusion_changed=fusion_changed,
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

    def clear_rolling_buffer(self, terminal_id: str, provider=None) -> None:
        """Clear ONLY the rolling byte buffer for a terminal — preserves
        ``_last_status`` and ``_allow_processing_revert``.

        Used by send_input to drop stale pre-task content (e.g. kiro-cli 2.11's
        "ask a question" idle placeholder) so it can't combine with the
        input_received flag to trigger a false COMPLETED before the agent has
        rendered its processing indicator. Unlike ``reset_buffer``, this does
        NOT wipe the sticky-latch state, so the arm set by ``notify_input_sent``
        survives and the subsequent IDLE→PROCESSING transition is honored.

        When the active provider is supplied, it is synchronously notified of
        the new monotonically increasing byte-buffer epoch while this monitor's
        lock is held.  That makes the boundary atomic with respect to the
        output-consumer thread, which otherwise could parse the fresh first
        chunk against state from the discarded buffer.
        """
        with self._lock:
            self._buffers[terminal_id] = ""
            epoch = self._buffer_epochs.get(terminal_id, 0) + 1
            self._buffer_epochs[terminal_id] = epoch
            if provider is not None:
                provider.notify_status_buffer_reset(epoch)

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

    @staticmethod
    def _resync_interval_s() -> float:
        from cli_agent_orchestrator.services.config_service import ConfigService

        try:
            return float(ConfigService.get("liveness.resync_interval_s", 60.0))
        except Exception:
            return 60.0

    def resync_from_pane_tail(
        self, terminal_id: str, filtered_tail: str, *, now: float | None = None
    ) -> bool:
        """D15: re-derive status from the pane sampler's retained level.

        Returns whether a forced pass ran. This method never captures the pane;
        the caller supplies the sample already produced by pane_liveness.
        """
        now = _clock() if now is None else now
        drop_seq = bus.get_drop_seq(terminal_id)
        with self._lock:
            published = self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)
            seen_drop_seq = self._drop_seq_seen.get(terminal_id, 0)
            published_at = self._last_publish_monotonic.get(terminal_id, now)
            last_resync = self._last_level_resync_monotonic.get(terminal_id)
            interval_s = self._resync_interval_s()
            dropped = drop_seq != seen_drop_seq
            periodic = (
                published == TerminalStatus.PROCESSING
                and now - published_at >= interval_s
                and (last_resync is None or now - last_resync >= interval_s)
            )
            if not dropped and not periodic:
                return False
            self._last_level_resync_monotonic[terminal_id] = now
            self._drop_seq_seen[terminal_id] = drop_seq
            # Consume the signalled drop level here rather than only on the
            # publish that may follow: a forced re-derive that lands on the
            # same status publishes nothing, and re-deriving it on every
            # subsequent tick would turn a one-shot signal into a level.

        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return False
        try:
            detected = provider.get_status(filtered_tail)
        except Exception:
            logger.debug("D15 pane-tail resync failed for %s", terminal_id, exc_info=True)
            return False
        if dropped:
            with self._lock:
                self._status_fusion_reason[terminal_id] = "resync_after_drop"
        else:
            with self._lock:
                self._status_fusion_reason.pop(terminal_id, None)
        self._apply_detection(terminal_id, detected, pass_source="forced")
        return True

    def clear_terminal(self, terminal_id: str) -> None:
        """Free buffer and status for a deleted terminal."""
        with self._lock:
            self._cancel_settlements_locked(terminal_id)
            self._latest_native_request.pop(terminal_id, None)
            self._buffers.pop(terminal_id, None)
            self._buffer_epochs.pop(terminal_id, None)
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
            self._screen_render.pop(terminal_id, None)
            self._screen_size_deferred_warned.discard(terminal_id)
            self._bursting.pop(terminal_id, None)
            self._retry_backoff_step.pop(terminal_id, None)
            self._drop_seq_seen.pop(terminal_id, None)
            self._last_publish_monotonic.pop(terminal_id, None)
            self._last_level_resync_monotonic.pop(terminal_id, None)
            self._status_fusion_reason.pop(terminal_id, None)
            self._bump_chunk_seq_locked(terminal_id)
            self._last_stale_capture_check.pop(terminal_id, None)
            self._buffer_changed_at.pop(terminal_id, None)
            self._pending_stale_capture.pop(terminal_id, None)
            self._capture_generation.pop(terminal_id, None)
            handle = self._quiesce_handle.pop(terminal_id, None)
            self._receiver_state_store.invalidate_terminal(terminal_id)
        self._cancel_quiesce_handle(handle)

    def unregister(self, terminal_id: str) -> None:
        """Unregister a terminal from monitoring (called on delete).

        Clears all monitoring state and removes the terminal from quarantine
        and error tracking.
        """
        with self._lock:
            self._consecutive_errors.pop(terminal_id, None)
            self._quarantined.discard(terminal_id)
            # F360 (#215): ghost-id tracking goes with the rest of the state.
            self._provider_not_found_count.pop(terminal_id, None)
            self._dropped_not_found.discard(terminal_id)
        self.clear_terminal(terminal_id)

    def record_probe_error(self, terminal_id: str) -> bool:
        """Record a consecutive probe error for a terminal.

        Returns True if the terminal was auto-quarantined (3 consecutive errors).
        """
        with self._lock:
            if terminal_id in self._quarantined:
                return True
            count = self._consecutive_errors.get(terminal_id, 0) + 1
            self._consecutive_errors[terminal_id] = count
            if count >= 3:
                self._quarantined.add(terminal_id)
                logger.warning(
                    f"StatusMonitor: terminal {terminal_id} quarantined after "
                    f"{count} consecutive probe errors (not found)"
                )
                return True
            return False

    def reset_probe_errors(self, terminal_id: str) -> None:
        """Reset consecutive error count on a successful probe."""
        with self._lock:
            if terminal_id in self._consecutive_errors:
                self._consecutive_errors[terminal_id] = 0

    def is_quarantined(self, terminal_id: str) -> bool:
        """Return whether a terminal has been quarantined due to probe errors."""
        with self._lock:
            return terminal_id in self._quarantined

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
            self._cancel_settlements_locked(terminal_id)
            self._latest_native_request.pop(terminal_id, None)
            if receiver_key is not None:
                self._receiver_state_store.invalidate(receiver_key)
            self._buffers[terminal_id] = ""
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            self._input_gen.pop(terminal_id, None)
            self._processing_gen.pop(terminal_id, None)
            self._status_gen.pop(terminal_id, None)
            self._drop_seq_seen.pop(terminal_id, None)
            self._last_publish_monotonic.pop(terminal_id, None)
            self._last_level_resync_monotonic.pop(terminal_id, None)
            self._status_fusion_reason.pop(terminal_id, None)
            self._new_epoch_locked(terminal_id)
            # Drop the rendered screen too so the relaunched CLI mode is
            # detected against a fresh viewport, not the failed attempt's.
            self._screens.pop(terminal_id, None)
            self._screen_render.pop(terminal_id, None)
            self._screen_size_deferred_warned.discard(terminal_id)
            self._bursting.pop(terminal_id, None)
            self._bump_chunk_seq_locked(terminal_id)
            self._last_stale_capture_check.pop(terminal_id, None)
            self._buffer_changed_at.pop(terminal_id, None)
            self._pending_stale_capture.pop(terminal_id, None)
            self._capture_generation.pop(terminal_id, None)
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
                    native = provider.get_status(buffer)
                    # F506: fuse the egress (bare status; the reason is not on
                    # this return, AC16/R-S7). fuse_status only reads.
                    fused, _reason = self.fuse_status(terminal_id, native)
                    return fused if fused is not None else native
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
                # F506 (AC16): :1856 still publishes the RAW fresh via
                # _apply_detection above (the latch does not move); the EGRESS
                # is fused (bare status — reason not on this return, R-N2).
                fused, _reason = self.fuse_status(terminal_id, fresh)
                return fused if fused is not None else fresh

        if cached == TerminalStatus.PROCESSING:
            # The cheap re-check above re-derives from the SAME rolling buffer the FIFO
            # pipeline feeds — once the process stops emitting output, that buffer stops
            # changing too, so the re-check can return PROCESSING/UNKNOWN forever while
            # the real pane already shows the finished response (#558; the module
            # constants above carry the full incident rationale). Consult the quiet gate
            # BEFORE anything that could fork: a terminal still streaming chunks is busy,
            # not wedged, and must not cost a subprocess call.
            with self._lock:
                changed_at = self._buffer_changed_at.get(terminal_id)
                # Pin the turn/output generation BEFORE the capture read. The pane
                # is sampled outside the lock; only a verdict whose generation is
                # still current at apply time may be applied (see below).
                generation = self._capture_generation.get(terminal_id, 0)
            buffer_is_quiet = (
                changed_at is not None
                and time.monotonic() - changed_at >= STALE_PROCESSING_BUFFER_QUIET_S
            )
            if buffer_is_quiet:
                fresh_capture = self._fresh_capture_pane_status(terminal_id, generation)
                if fresh_capture is not None:
                    logger.debug(
                        f"get_status [{terminal_id}]: cached=PROCESSING stale-buffer re-check "
                        f"still PROCESSING/UNKNOWN, fresh capture-pane={fresh_capture.value}"
                    )
                    if (
                        fresh_capture != TerminalStatus.PROCESSING
                        and fresh_capture != TerminalStatus.UNKNOWN
                    ):
                        # The capture-pane read above ran OUTSIDE the lock — a real
                        # subprocess call, tens of milliseconds rather than microseconds —
                        # so the world can have moved: real new output can have resumed
                        # PROCESSING, or notify_input_sent can have started a whole new
                        # turn. The latter is invisible to a _last_status check (a new
                        # turn deliberately KEEPS _last_status == PROCESSING while arming
                        # the revert), which is why the generation pinned before the read
                        # is the authority here. Validate and apply in ONE critical
                        # section — a check-then-apply gap would let notify_input_sent
                        # slip between them, and the stale verdict would consume the arm
                        # it just set, latch-blocking the new turn's genuine PROCESSING.
                        with self._lock:
                            current_last_status = self._last_status.get(terminal_id)
                            generation_current = (
                                self._capture_generation.get(terminal_id, 0) == generation
                            )
                            apply_ok = (
                                generation_current
                                and current_last_status == TerminalStatus.PROCESSING
                            )
                        if apply_ok:
                            # Fork deviation from upstream #712: our _apply_detection owns
                            # the whole latch/observation/fusion pass and takes the lock
                            # itself, so there is no _apply_detection_locked to call inside
                            # the validating critical section. The generation pinned before
                            # the capture read still rejects a verdict overtaken by new
                            # input or new output; the residual window is the microseconds
                            # between releasing the lock here and re-taking it inside
                            # _apply_detection, which publishes the change itself.
                            self._apply_detection(terminal_id, fresh_capture)
                            return fresh_capture
                        logger.debug(
                            f"get_status [{terminal_id}]: fresh capture-pane result "
                            "discarded — the terminal moved on (new input or new "
                            "output) while the capture-pane read was in flight"
                        )
                        # The pipeline got there first: hand back ITS status rather than
                        # the `cached` value snapshotted at entry, which is now one step
                        # stale.
                        if current_last_status is not None:
                            return current_last_status
        # F506: fuse the cached egress (bare status, R-S7).
        fused_cached, _reason = self.fuse_status(terminal_id, cached)
        return fused_cached if fused_cached is not None else cached

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

    def _fresh_capture_pane_status(
        self, terminal_id: str, generation: int
    ) -> Optional[TerminalStatus]:
        """Re-detect a stuck-PROCESSING terminal from a fresh pane capture (#558).

        ``generation`` is the turn/output generation the caller pinned BEFORE
        deciding to capture. The pane read below runs unlocked, so
        notify_input_sent or _process_chunk can bump the generation (and clear the
        candidate map) while it is in flight; a verdict from such a read describes
        the pane from BEFORE that boundary. The caller's apply-time check cannot
        see this alone: it only rejects CONFIRMED verdicts, while a straddling
        first read would re-seed the candidate map AFTER the boundary cleared it,
        and the next poll — pinning the new, by-then-stable generation — would
        treat that pre-boundary entry as its matching first read and confirm it.
        So every candidate-map mutation here (seed, confirm-pop, busy-pop) is
        performed only if ``generation`` is still current under the lock.

        Reads the pane directly via ``get_backend().get_history()`` (a real ``tmux
        capture-pane``, not the FIFO-fed rolling buffer) and re-runs provider detection
        against it. tmux always holds the correct, current rendered pane state regardless
        of output volume, so this can see the ready state a stale buffer cannot.

        Detector routing: a capture-pane snapshot is RENDERED content — cursor moves
        resolved, lines in on-screen order — a different input shape from the raw byte
        stream most ``get_status()`` detectors are tuned against (see BaseProvider.
        get_status's input contract). Feeding a rendered frame to a raw-stream detector
        produces systematic misreads, not noise: in a rendered busy kiro frame the
        always-drawn composer placeholder sits physically BELOW the working line and the
        credits line, which its raw-stream ordering checks parse as COMPLETED — and a
        deterministic misread sails straight through the two-read confirm below, because
        both reads see the same bytes. Applied, that false ready sticky-latches, disarms
        _allow_processing_revert, and blocks the agent's genuine PROCESSING for the rest
        of the turn. So the capture is routed through the two existing opt-in predicates:
        ``supports_screen_detection`` providers get their purpose-built
        ``get_status_from_screen()`` (calibrated for exactly this composited-viewport
        shape), ``supports_direct_status_probe`` providers get ``get_status()`` (declared
        safe on rendered snapshots — the same contract terminal_service's deferred-init
        direct probe relies on), and providers with neither flag fail CLOSED: no capture,
        no verdict, the terminal stays PROCESSING until the pipeline resolves it.
        The read is viewport-only (``visible_only=True`` — capture-pane ``-S 0``): a
        ``tail_lines`` read would include scrollback ABOVE the viewport, and detectors
        that match anywhere in their input (kimi/kiro ERROR indicators) would resurrect
        text from finished turns. Only the currently rendered screen is evidence.

        A single capture is still not trusted even on a routed detector: Ink-style TUIs
        repaint by clear-then-rewrite, and a sample caught between those two steps can
        miss the spinner while the previous response box parses ready. The screen path
        never samples mid-burst for exactly this reason (_schedule_screen_detection);
        since this read fires at an arbitrary moment instead, it requires the SAME ready
        status on two consecutive reads — the confirming read arriving within
        STALE_PROCESSING_CONFIRM_TTL_S — before honoring it. The same confirm-don't-trust
        pattern claude_code's wait_until_input_ready uses. The confirm still earns its
        keep despite the quiet gate: the gate watches the FIFO-fed buffer, so a pane that
        repaints without reaching the wedged FIFO can differ between reads.

        Returns ``None`` when skipped (rate-limited, no provider, unroutable detector,
        unconfirmed candidate, or any read/detection failure) — the caller treats that
        identically to "still PROCESSING", never as a signal to change status. Only ever
        called when cached status is already PROCESSING, so every failure path degrades
        to today's behavior, never past it.
        """
        now = time.monotonic()
        with self._lock:
            last_check = self._last_stale_capture_check.get(terminal_id)
            if last_check is not None and now - last_check < STALE_PROCESSING_CAPTURE_INTERVAL_S:
                return None
            self._last_stale_capture_check[terminal_id] = now

        try:
            provider = provider_manager.get_provider(terminal_id)
        except Exception as e:
            # get_provider() raises (not returns None) for a terminal it doesn't
            # recognize (not yet / no longer in the DB) — same defensive shape as
            # get_status()'s own event-inbox branch.
            logger.debug(f"_fresh_capture_pane_status [{terminal_id}]: get_provider failed: {e}")
            return None
        if provider is None:
            return None

        if not getattr(provider, "supports_stale_capture_selfheal", True):
            # Explicit opt-out: this provider's get_status is raw-stream tuned and
            # misreads a rendered frame (see ProviderBase for the full rationale).
            return None

        use_screen = getattr(provider, "supports_screen_detection", False)
        if not use_screen and not getattr(provider, "supports_direct_status_probe", False):
            # Raw-stream-tuned detector with no snapshot-safe alternative (kiro_cli,
            # cursor_cli): a rendered frame cannot be trusted as its input — see the
            # docstring — so don't capture at all. Self-heal is opt-in via either flag,
            # never a guess; these providers stay PROCESSING until the pipeline resolves
            # them.
            return None

        try:
            from cli_agent_orchestrator.backends.registry import get_backend

            # visible_only, NOT tail_lines: capture-pane's -S -N means "N history
            # lines ABOVE the viewport, plus the viewport" — a tail_lines read
            # includes scrollback, and detectors that match anywhere in their input
            # (kiro/kimi ERROR indicators) would resurrect text from finished turns.
            # Only the currently rendered screen is evidence about the current turn.
            fresh_output = get_backend().get_history(
                provider.session_name,
                provider.window_name,
                strip_escapes=True,
                visible_only=True,
            )
        except Exception as e:
            logger.debug(
                f"_fresh_capture_pane_status [{terminal_id}]: capture-pane read failed: {e}"
            )
            return None
        if not fresh_output:
            return None

        try:
            if use_screen:
                detected = provider.get_status_from_screen(fresh_output.splitlines())
            else:
                detected = provider.get_status(fresh_output)
        except Exception as e:
            logger.debug(f"_fresh_capture_pane_status [{terminal_id}]: detection failed: {e}")
            return None

        if detected == TerminalStatus.PROCESSING or detected == TerminalStatus.UNKNOWN:
            # Not a ready candidate — nothing to confirm. Clear any pending one: a busy
            # read BETWEEN two ready reads means the terminal is genuinely still working,
            # so the earlier candidate no longer describes a settled pane. Only if this
            # read didn't straddle a boundary, though — a busy verdict from BEFORE a
            # notify_input_sent/_process_chunk bump says nothing about a candidate
            # legitimately seeded after it.
            with self._lock:
                if self._capture_generation.get(terminal_id, 0) == generation:
                    self._pending_stale_capture.pop(terminal_id, None)
            return detected

        now = time.monotonic()
        with self._lock:
            if self._capture_generation.get(terminal_id, 0) != generation:
                # The pane read straddled a turn/output boundary: the boundary already
                # cleared the candidate map, and this verdict describes the pane from
                # before it. Seeding it anyway would hand the NEXT poll (which pins the
                # new generation before its read) a pre-boundary first read to "confirm"
                # against — the exact single-frame latch the two-read confirm exists to
                # prevent. No mutation, no verdict.
                logger.debug(
                    f"_fresh_capture_pane_status [{terminal_id}]: candidate "
                    f"{detected.value} discarded — the terminal moved on (new input or "
                    "new output) while the capture-pane read was in flight"
                )
                return None
            pending = self._pending_stale_capture.get(terminal_id)
            if (
                pending is not None
                and pending[0] == detected
                and pending[2] == generation
                and now - pending[1] <= STALE_PROCESSING_CONFIRM_TTL_S
            ):
                # Second consecutive matching read, in time and within the same
                # turn/output generation — honor it.
                self._pending_stale_capture.pop(terminal_id, None)
                confirmed = True
            else:
                # First sighting, a different candidate than the pending one, or a
                # candidate that aged out — (re)record with a fresh timestamp and wait
                # for the next read to agree.
                self._pending_stale_capture[terminal_id] = (detected, now, generation)
                confirmed = False
        if not confirmed:
            logger.debug(
                f"_fresh_capture_pane_status [{terminal_id}]: candidate {detected.value} "
                "recorded, awaiting a second confirming read before honoring it"
            )
            return None
        return detected

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

    def _probe_screen_status_stage0b(self, terminal_id: str) -> ProbeResult:
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.providers.screen_classification import (
            ScreenClassification,
            ScreenClassificationResult,
            screen_classification_result,
        )

        try:
            provider = provider_manager.get_provider(terminal_id)
        except Exception:
            provider = None
        metadata = get_terminal_metadata(terminal_id)
        backend = get_backend()

        with self._lock:
            screen_state = self._screens.get(terminal_id)
            if screen_state is None:
                rows: List[str] = []
                columns = 0
                row_count = 0
            else:
                screen = screen_state[0]
                rows = (
                    self._screen_rows_locked(terminal_id, screen)
                    if hasattr(screen, "display")
                    else []
                )
                columns = int(getattr(screen, "columns", 0))
                row_count = int(getattr(screen, "lines", len(rows)))

        raw_classification: object | None = None

        def classify_rows(
            frame_rows: List[str], prior_signals: tuple[object, ...]
        ) -> ScreenClassificationResult:
            nonlocal raw_classification
            if provider is None or not frame_rows:
                raw_classification = None
                return screen_classification_result([])
            if self._signal_emitting(provider):
                result = screen_classification_result(
                    provider.emit_screen_signals(frame_rows),
                    prior_signals,
                    provider.capabilities.liveness_anchor,
                )
                raw_classification = result
                return result
            if hasattr(provider, "get_status_from_screen"):
                status = provider.get_status_from_screen(frame_rows)
                raw_classification = None
                return ScreenClassificationResult(
                    ScreenClassification(status, "none", None, None), ()
                )
            # Source-compatible test/third-party fallback; real providers are
            # structurally classified by the two branches above.
            result = provider.classify_screen(frame_rows)
            raw_classification = result
            return result

        key = None
        if metadata is not None and "lifecycle_generation" in metadata:
            key = (
                terminal_id,
                int(metadata["lifecycle_generation"]),
                str(metadata["tmux_window"]),
            )
        prior = self._receiver_state_store.prior_classification(key) if key is not None else None
        prior_signals: tuple[object, ...] = () if prior is None else prior.signals
        classification = classify_rows(rows, prior_signals)
        initial_status = classification.status
        frame_source: ScreenProbeFrameSource = "incremental"
        identity_failure: str | None = None
        probe_failure: str | None = None
        temporal_demotion: ScreenProbeTemporalDemotion | None = None
        last_proof: IdentityProof | None = None
        captured_at_mono = time.monotonic()

        def capture(corroboration_prior: tuple[object, ...]):
            nonlocal last_proof, identity_failure, captured_at_mono
            if metadata is None:
                raise LookupError(f"terminal metadata unavailable for {terminal_id}")
            last_proof = self.prove_terminal_identity(terminal_id)
            if last_proof.failure is not None:
                identity_failure = last_proof.failure
                raise PaneIdentityProofFailure(identity_failure)
            captured = backend.capture_viewport(metadata["tmux_session"], metadata["tmux_window"])
            captured_at_mono = time.monotonic()
            captured_rows = captured.splitlines()
            if not captured_rows or not any(row.strip() for row in captured_rows):
                raise EmptyProbeCapture("Fresh viewport capture was empty")
            pane_size = backend.get_pane_size(metadata["tmux_session"], metadata["tmux_window"])
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
                classify_rows(captured_rows, corroboration_prior),
            )

        try:
            if provider is not None and classification.status in {
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
            }:
                rows, columns, row_count, classification = capture(prior_signals)
                frame_source = "fresh_capture"

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
                rows, columns, row_count, classification = capture(prior_signals)
                frame_source = "fresh_capture"
                frames = 0
                previous = _corroborable_rows(classification)
                changed = classification.status != TerminalStatus.PROCESSING or not previous
                if changed:
                    classification = ScreenClassificationResult(
                        ScreenClassification(TerminalStatus.PROCESSING, "progress", None, None),
                        classification.signals,
                    )
                else:
                    for _ in range(2):
                        time.sleep(1.2)
                        rows, columns, row_count, classification = capture(classification.signals)
                        frames += 1
                        current = _corroborable_rows(classification)
                        if Counter(current) != Counter(previous):
                            previous = current
                            changed = True
                            if classification.status != TerminalStatus.PROCESSING:
                                classification = ScreenClassificationResult(
                                    ScreenClassification(
                                        TerminalStatus.PROCESSING,
                                        "progress",
                                        None,
                                        None,
                                    ),
                                    classification.signals,
                                )
                            break
                        previous = current
                if not changed and previous:
                    temporal_demotion = {
                        "frames": frames,
                        "multiset_sha256": _row_multiset_hash(previous),
                    }
                    rows, columns, row_count, classification = capture(classification.signals)

            if provider is not None and frame_source == "incremental":
                rows, columns, row_count, classification = capture(prior_signals)
                frame_source = "fresh_capture"
        except PaneIdentityProofFailure:
            rows, columns, row_count = [], 0, 0
            classification = screen_classification_result([])
            raw_classification = None
            frame_source = "fresh_capture"
        except EmptyProbeCapture:
            probe_failure = "empty_capture"
            rows, columns, row_count = [], 0, 0
            classification = (
                ScreenClassificationResult(
                    ScreenClassification(TerminalStatus.PROCESSING, "progress", None, None), ()
                )
                if initial_status == TerminalStatus.PROCESSING
                else screen_classification_result([])
            )
            raw_classification = None
            frame_source = "fresh_capture"
        except Exception:
            logger.exception("Error refreshing admission screen for %s", terminal_id)
            probe_failure = "empty_capture"
            rows, columns, row_count = [], 0, 0
            classification = (
                ScreenClassificationResult(
                    ScreenClassification(TerminalStatus.PROCESSING, "progress", None, None), ()
                )
                if initial_status == TerminalStatus.PROCESSING
                else screen_classification_result([])
            )
            raw_classification = None
            frame_source = "fresh_capture"

        if (
            classification.status == TerminalStatus.UNKNOWN
            and probe_failure is None
            and identity_failure is None
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
        if identity_failure is not None:
            meta["identity_proof_failure"] = identity_failure
        if probe_failure is not None:
            meta["probe_failure"] = probe_failure  # type: ignore[typeddict-item]
        if temporal_demotion is not None:
            meta["temporal_demotion"] = temporal_demotion
        if provider is not None:
            try:
                hazard = provider.classify_injection_hazard(rows)
                if hazard is not None:
                    meta["injection_hazard"] = hazard
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

        with self._lock:
            epoch = self._epoch_locked(terminal_id)
        token = self._receiver_state_store.mint_token(terminal_id, epoch)
        evidence = ProbeEvidence.from_legacy_dict(meta)
        freshness_kind = (
            "identity_failed"
            if identity_failure is not None
            else "probe_failed" if probe_failure is not None else "identity_ok"
        )
        freshness_detail = identity_failure or probe_failure
        try:
            with self._lock:
                self._publish_observation(
                    terminal_id,
                    latched_status=classification.status,
                    pass_outcome="probe",
                    frame_source="fresh_capture",
                    metadata=metadata,
                    freshness_proof=FreshnessProof(freshness_kind, freshness_detail),
                    captured_at_mono=captured_at_mono,
                    raw_classification=raw_classification,
                    probe_evidence=evidence,
                    origin="probe",
                    fresh_token=token,
                )
        except Exception:
            try:
                self._log_receiver_publish_failure(terminal_id)
            except Exception:
                pass
        return ProbeResult(classification.status, copy.deepcopy(meta), token)

    def _probe_screen_status_stage0a_dead(self, terminal_id: str) -> ProbeResult:
        """Frozen Stage-0a implementation retained for source archaeology."""
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
                    rows = (
                        self._screen_rows_locked(terminal_id, screen)
                        if hasattr(screen, "display")
                        else []
                    )
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
                status = provider.get_status_from_screen(frame_rows)
                return ScreenClassificationResult(
                    ScreenClassification(status, "none", None, None), ()
                )
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
        return classification.status, meta  # type: ignore[return-value]

    def probe_screen_status(self, terminal_id: str) -> ProbeResult:
        """Classify, prove, publish, and return one operation-owned probe."""

        return self._probe_screen_status_stage0b(terminal_id)

    def get_rendered_screen(self, terminal_id: str) -> Optional[List[str]]:
        """Return the current pyte-composited screen for a terminal if present."""
        with self._lock:
            scr = self._screens.get(terminal_id)
            if scr is None:
                return None
            try:
                return self._screen_rows_locked(terminal_id, scr[0])
            except Exception:
                logger.exception("Error rendering screen for %s", terminal_id)
                return None


# Module-level singleton
status_monitor = StatusMonitor()
