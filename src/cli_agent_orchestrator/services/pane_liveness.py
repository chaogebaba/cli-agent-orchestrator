"""Single pane-liveness sampler for F506 (D1).

Owns the *one* capture -> ``liveness_exclude_patterns`` filter -> sha256
pipeline, lifted verbatim from ``stalled_callback_watchdog.refresh_screen_
fingerprints`` (the second-sampler ban, f295-half2:247-251, forbids a second
``capture-pane``). The watchdog keeps the tick and its episode clocks but now
*reads* ``observe(tid)`` instead of capturing — net sampler count stays 1
(AC1/AC2).

Sampling widens from "terminals with an armed episode" to "all live terminals
with a readable pane" (Fork A pick (i)): the #361 incident happened with no
episode armed, so episode-scoped sampling could never see it (AC17). The widen
is backend-agnostic — tmux AND herdr panes are samplable through
``backend.get_history`` (R3-B2), so there is deliberately NO backend-identity
clause.

``observe(tid)`` is the SAMPLER call (the watchdog tick drives it); it captures
once and updates state. ``peek(tid)`` is the PURE read (``fuse_status`` and the
wedge caller consume it) and never captures. Both return ``None`` — NOT a
zero-count record — whenever no usable sample exists (never sampled, last
capture raised, evicted, or stale). That ``None`` is the whole no-evidence
rule the fusion consumes: rule 3a cannot fire without evidence, so a capture
outage suppresses only the pane-delta DOWNGRADE, never the marker RAISE
(R3-B1).

**Pane-hold bound (D12).** The SAMPLER owns the clock and the edge. Without a
bound, a periodic blink the exclude patterns miss pins a terminal PROCESSING
and withholds delivery forever — the exact monotonic-state failure #361
forbids. ``downgrade_since`` is SET only on a sample where rule 3a would
*actually* downgrade (published status in {IDLE, COMPLETED}, no open marker,
usable sample, ``unchanged_count < K``); any tick failing ANY of those four
clears ``downgrade_since`` AND ``pane_hold_expired`` together. The sampler
flips ``pane_hold_expired`` and emits EXACTLY ONE WARN on the tick that crosses
``liveness.pane_delta_max_hold_s`` (default 300); a later crossing after a
stabilize/re-churn cycle is a NEW episode and emits a NEW WARN. ``fuse_status``
only READS the flag (D2 — pure on the read path).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.config_service import ConfigService

if TYPE_CHECKING:
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

logger = logging.getLogger(__name__)

# Tail length matches the watchdog's WATCHDOG_SCREEN_TAIL_LINES — the pipeline
# is lifted verbatim, so the input window must be identical (AC2).
PANE_LIVENESS_TAIL_LINES = 45

# Staleness is a CONSTANT 10s — 2x the tick CEILING (min(5.0, ...)), NOT the
# instantaneous interval which varies 1-5s and would flip healthy terminals
# stale on a cadence decrease (R3-S1). "No sample in the last 2 completed
# sampler passes" is the intent.
_STALENESS_S = 10.0

_DEFAULT_STABLE_SAMPLES = 3
_DEFAULT_MAX_HOLD_S = 300.0


@dataclass(frozen=True)
class PaneObservation:
    """One usable sample's derived facts, as read by fusion + the wedge caller.

    ``fingerprint``/``fp_changed``/``filtered_tail`` exist so the watchdog can
    do its episode-clock and no-progress bookkeeping off the SAME sample (net
    sampler count stays 1, AC1/AC2) instead of capturing again.
    """

    unchanged_count: int
    unchanged_for_s: float
    pane_hold_expired: bool
    fingerprint: str
    fp_changed: bool
    filtered_tail: str


@dataclass
class _PaneState:
    fp: str | None = None
    unchanged_count: int = 0
    last_change_monotonic: float = 0.0
    sampled_at: float = 0.0
    downgrade_since: float | None = None
    pane_hold_expired: bool = False


@dataclass
class PaneLivenessService:
    """The single liveness sampler; process singleton."""

    _clock: Callable[[], float] = time.monotonic
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _state: dict[str, _PaneState] = field(default_factory=dict)

    # ---- config -----------------------------------------------------------
    def _stable_samples(self) -> int:
        try:
            return int(ConfigService.get("liveness.stable_samples", _DEFAULT_STABLE_SAMPLES))
        except Exception:
            return _DEFAULT_STABLE_SAMPLES

    def _max_hold_s(self) -> float:
        try:
            return float(ConfigService.get("liveness.pane_delta_max_hold_s", _DEFAULT_MAX_HOLD_S))
        except Exception:
            return _DEFAULT_MAX_HOLD_S

    # ---- capture pipeline (lifted verbatim from the watchdog) -------------
    def _capture(self, terminal_id: str) -> tuple[str, str] | None:
        """Return ``(fingerprint, filtered_tail)`` or None if unusable.

        The filter -> sha256 pipeline is lifted verbatim from the watchdog's
        refresh_screen_fingerprints (the second-sampler ban forbids a second
        capture-pane); the filtered tail is returned too so the watchdog can do
        its no-progress hint bookkeeping off the same read.
        """
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.providers.manager import provider_manager
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _filtered_liveness_tail,
        )

        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return None
        try:
            backend = get_backend()
            tail = backend.get_history(
                metadata["tmux_session"],
                metadata["tmux_window"],
                tail_lines=PANE_LIVENESS_TAIL_LINES,
                strip_escapes=True,
            )
            provider = provider_manager.get_provider(terminal_id)
            patterns = (
                getattr(provider, "liveness_exclude_patterns", []) if provider is not None else []
            )
            tail = _filtered_liveness_tail(tail, list(patterns or []))
            fingerprint = hashlib.sha256(tail.encode("utf-8", "replace")).hexdigest()
            return fingerprint, tail
        except Exception:
            logger.debug("pane_liveness capture failed for %s", terminal_id, exc_info=True)
            return None

    # ---- the tick entrypoint ---------------------------------------------
    def observe(
        self,
        terminal_id: str,
        *,
        now: float | None = None,
        monitor: "StatusMonitor | None" = None,
    ) -> PaneObservation | None:
        """Sample one terminal's pane and update its liveness state.

        Returns ``None`` when no usable sample exists on THIS tick (capture
        raised / pane unreadable). On a usable sample, updates the debounce
        counter and the pane-hold bound, then returns the derived facts.

        A tick with no usable sample clears ``downgrade_since`` and
        ``pane_hold_expired`` (r8 R8-S1 — an outage withholds nothing, so the
        bound must not keep counting), and returns ``None``.
        """
        now = self._clock() if now is None else now
        captured = self._capture(terminal_id)

        with self._lock:
            state = self._state.get(terminal_id)
            if state is None:
                state = _PaneState()
                self._state[terminal_id] = state

            if captured is None:
                # No usable sample: rule 3a cannot fire, so nothing is withheld.
                # Clear the hold bound (both fields together) and report None.
                state.downgrade_since = None
                state.pane_hold_expired = False
                return None

            fingerprint, filtered_tail = captured

            # Usable sample: advance the debounce counter.
            if state.fp is None:
                fp_changed = True
                state.fp = fingerprint
                state.unchanged_count = 1
                state.last_change_monotonic = now
            elif state.fp == fingerprint:
                fp_changed = False
                state.unchanged_count += 1
            else:
                fp_changed = True
                state.fp = fingerprint
                state.unchanged_count = 1
                state.last_change_monotonic = now
            state.sampled_at = now

            self._update_hold_bound(terminal_id, state, now, monitor)

            return PaneObservation(
                unchanged_count=state.unchanged_count,
                unchanged_for_s=max(0.0, now - state.last_change_monotonic),
                pane_hold_expired=state.pane_hold_expired,
                fingerprint=fingerprint,
                fp_changed=fp_changed,
                filtered_tail=filtered_tail,
            )

    def _update_hold_bound(
        self,
        terminal_id: str,
        state: _PaneState,
        now: float,
        monitor: "StatusMonitor | None",
    ) -> None:
        """Set/clear ``downgrade_since`` and flip ``pane_hold_expired`` (D12).

        Called ONLY on a usable sample (the no-sample path clears in ``observe``).
        The SET edge fires iff rule 3a would actually downgrade this tick:
        published status in {IDLE, COMPLETED}, no open marker, unchanged_count < K.
        Any other condition clears the bound (both fields).
        """
        k = self._stable_samples()
        would_downgrade = self._rule3a_would_downgrade(terminal_id, state, k, monitor)

        if not would_downgrade:
            # Not actually withholding — clear the bound (both fields together)
            # so a later real hold is a fresh episode with its own single WARN.
            state.downgrade_since = None
            state.pane_hold_expired = False
            return

        if state.downgrade_since is None:
            state.downgrade_since = now
            # A fresh hold episode: the expiry flag must start clear.
            state.pane_hold_expired = False
            return

        if not state.pane_hold_expired and (now - state.downgrade_since) >= self._max_hold_s():
            state.pane_hold_expired = True
            logger.warning(
                "pane_delta hold bound crossed for %s after %.0fs "
                '(pane still churning); admitting with fusion_reason="pane_delta_expired"',
                terminal_id,
                self._max_hold_s(),
            )

    def _rule3a_would_downgrade(
        self,
        terminal_id: str,
        state: _PaneState,
        k: int,
        monitor: "StatusMonitor | None",
    ) -> bool:
        """True iff rule 3a would downgrade on this sample (the SET-edge test).

        Reads the PRE-fusion published status via ``get_published_status`` (r8
        R8-S2) — never ``get_boundary_observation``/``get_raw_status``/
        ``get_status``, which fuse and would re-enter ``observe()``.
        """
        if state.unchanged_count >= k:
            return False
        try:
            from cli_agent_orchestrator.services.question_state import question_state

            if question_state.is_open(terminal_id):
                return False
        except Exception:
            pass
        resolved_monitor = monitor
        if resolved_monitor is None:
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            resolved_monitor = status_monitor
        published = resolved_monitor.get_published_status(terminal_id)
        return published in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)

    # ---- eviction ---------------------------------------------------------
    def forget(self, terminal_id: str) -> None:
        with self._lock:
            self._state.pop(terminal_id, None)

    def _is_stale(self, state: _PaneState, now: float) -> bool:
        return (now - state.sampled_at) > _STALENESS_S

    # ---- pure read (fusion + wedge) --------------------------------------
    def peek(self, terminal_id: str, *, now: float | None = None) -> PaneObservation | None:
        """Return the LAST usable observation without capturing (pure).

        This is what ``fuse_status`` (D2, read-time, must not capture) and the
        wedge caller (AC6, "without a second capture") consume. Returns ``None``
        — the whole no-evidence rule — when there is no usable sample: never
        sampled (``fp is None``), the last capture raised (bound cleared so
        ``fp`` may persist but ``sampled_at`` is old), or the entry is STALE
        (no sample in the last 2 completed sampler passes, a CONSTANT 10s — not
        the instantaneous interval, R3-S1).

        ``observe`` is the ONLY method that captures; ``peek`` never does, so
        the single-sampler invariant (AC1) and read-path purity (D2) both hold.
        """
        now = self._clock() if now is None else now
        with self._lock:
            state = self._state.get(terminal_id)
            if state is None or state.fp is None:
                return None
            if self._is_stale(state, now):
                return None
            return PaneObservation(
                unchanged_count=state.unchanged_count,
                unchanged_for_s=max(0.0, now - state.last_change_monotonic),
                pane_hold_expired=state.pane_hold_expired,
                fingerprint=state.fp,
                fp_changed=False,
                filtered_tail="",
            )


pane_liveness = PaneLivenessService()

__all__ = ["PaneLivenessService", "PaneObservation", "pane_liveness", "PANE_LIVENESS_TAIL_LINES"]
