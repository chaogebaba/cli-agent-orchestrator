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

# F568 D12d — marker_rows diagnostic (AC-7): at most 3 rows above the box top
# rail, each truncated to 120 printable chars with non-printables stripped.
_MARKER_ROWS_MAX = 3
_MARKER_ROW_MAX_CHARS = 120


@dataclass(frozen=True)
class PaneObservation:
    """One usable sample's derived facts, as read by fusion + the wedge caller.

    ``fingerprint``/``fp_changed``/``filtered_tail`` exist so the watchdog can
    do its episode-clock and no-progress bookkeeping off the SAME sample (net
    sampler count stays 1, AC1/AC2) instead of capturing again.

    F568 D12d fields (``busy_marker``/``children_count``/``marker_rows``) are
    sampled from the ONE snapshot and the ONE metadata dict ``_capture`` already
    holds — no second capture, no second metadata/DB read. ``fuse_status`` and
    ``_rule3a_would_downgrade`` read ONLY these fields. They are APPENDED and
    DEFAULTED so the ~construction sites and any positional test constructors
    compile unchanged.
    """

    unchanged_count: int
    unchanged_for_s: float
    pane_hold_expired: bool
    fingerprint: str
    fp_changed: bool
    filtered_tail: str
    # F568 D12d sampled facts (appended, defaulted).
    busy_marker: bool | None = None
    children_count: int = 0
    marker_rows: tuple[str, ...] = ()


@dataclass
class _PaneState:
    fp: str | None = None
    unchanged_count: int = 0
    last_change_monotonic: float = 0.0
    sampled_at: float = 0.0
    downgrade_since: float | None = None
    pane_hold_expired: bool = False
    # F568 D12d — sampled facts, stored atomically with the fingerprint.
    busy_marker: bool | None = None
    children_count: int = 0
    marker_rows: tuple[str, ...] = ()
    # F568 AC-7 veto-defect episode clock — independent of downgrade_since.
    veto_episode_open: bool = False
    veto_warned_at: float | None = None


@dataclass(frozen=True)
class _CaptureResult:
    """Internal: one raw sample's derived facts, before debounce bookkeeping."""

    fingerprint: str
    filtered_tail: str
    busy_marker: bool | None
    children_count: int
    marker_rows: tuple[str, ...]


def _children_count_from_metadata(metadata: dict[str, object]) -> int:
    """Length of the D12a children ledger on the terminal's free-form metadata.

    The ledger lives under the parsed ``metadata`` sub-dict (the JSON-decoded
    ``metadata_json`` column, where the D12a hooks write ``children`` via the
    metadata endpoint). Absent / malformed → 0. The hook WRITERS are the later
    D12a lane; only the READ side lands here.
    """
    try:
        free_form = metadata.get("metadata")
        if isinstance(free_form, dict):
            children = free_form.get("children")
            if isinstance(children, list):
                return len(children)
    except Exception:
        pass
    return 0


def _sanitize_marker_row(line: str) -> str:
    """Strip non-printables and truncate to the AC-7 diagnostic cap (120 chars)."""
    cleaned = "".join(ch for ch in line if ch.isprintable())
    return cleaned[:_MARKER_ROW_MAX_CHARS]


def _marker_rows_from_snapshot(snapshot: str) -> tuple[str, ...]:
    """The ≤3 rows immediately above the composer box top rail (AC-7 diagnostic).

    Anchored to the same box ``new_tui_box_spinner_live`` uses (the box holding
    the freshest prompt). Each row is truncated to 120 printable chars with
    non-printables stripped, so a redraw-garbled 400-char row is emitted safe.
    Empty tuple when there is no identifiable current box (e.g. non-claude
    providers, or a claude snapshot without a complete rail-prompt-rail box).
    """
    try:
        from cli_agent_orchestrator.providers.claude_code import (
            IDLE_PROMPT_PATTERN,
            NEW_TUI_BOX_PATTERN,
        )
    except Exception:
        return ()

    import re as _re

    last_prompt = None
    for m in _re.finditer(IDLE_PROMPT_PATTERN, snapshot):
        last_prompt = m
    if last_prompt is None:
        return ()

    input_box = None
    for m in NEW_TUI_BOX_PATTERN.finditer(snapshot):
        if m.start() <= last_prompt.start() < m.end():
            input_box = m
    if input_box is None:
        return ()

    above_lines = snapshot[: input_box.start()].rstrip("\n").split("\n")
    rows = above_lines[-_MARKER_ROWS_MAX:]
    return tuple(_sanitize_marker_row(r) for r in rows)


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
    def _capture(self, terminal_id: str) -> "_CaptureResult | None":
        """Return the sampled facts or None if unusable.

        The filter -> sha256 pipeline is lifted verbatim from the watchdog's
        refresh_screen_fingerprints (the second-sampler ban forbids a second
        capture-pane); the filtered tail is returned too so the watchdog can do
        its no-progress hint bookkeeping off the same read.

        F568 D12d: from the ONE ``tail`` (read BEFORE ``_filtered_liveness_tail``
        so no exclude pattern can erase the evidence) and the ONE ``metadata``
        dict it already holds, ``_capture`` also computes ``busy_marker`` (the
        provider's ``rule3a_busy_marker`` on the raw snapshot), ``children_count``
        (D12a ledger length; absent → 0 — the read side lands here, the hook
        writers are the LATER D12a lane), and ``marker_rows`` (the AC-7
        diagnostic). No second capture, no second metadata/DB read.
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

            # F568 D12d: derive the busy-marker + diagnostic rows from the RAW
            # (pre-filter) snapshot so any present/future exclude pattern cannot
            # erase the evidence (§10 predicate note). children_count comes from
            # the metadata dict already in hand.
            busy_marker = self._sample_busy_marker(provider, tail)
            children_count = _children_count_from_metadata(metadata)
            marker_rows = _marker_rows_from_snapshot(tail)

            patterns = (
                getattr(provider, "liveness_exclude_patterns", []) if provider is not None else []
            )
            tail = _filtered_liveness_tail(tail, list(patterns or []))
            fingerprint = hashlib.sha256(tail.encode("utf-8", "replace")).hexdigest()
            return _CaptureResult(
                fingerprint=fingerprint,
                filtered_tail=tail,
                busy_marker=busy_marker,
                children_count=children_count,
                marker_rows=marker_rows,
            )
        except Exception:
            logger.debug("pane_liveness capture failed for %s", terminal_id, exc_info=True)
            return None

    @staticmethod
    def _sample_busy_marker(provider: object, snapshot: str) -> bool | None:
        """Call the provider's ``rule3a_busy_marker`` defensively (never raises).

        Non-``claude_code`` providers return ``None`` (BaseProvider default), so
        their rule-3a behaviour is byte-identical. A provider that lacks the
        method or raises is treated as no-signal (``None``).
        """
        if provider is None:
            return None
        marker = getattr(provider, "rule3a_busy_marker", None)
        if marker is None:
            return None
        try:
            result = marker(snapshot)
        except Exception:
            logger.debug("rule3a_busy_marker raised; treating as no-signal", exc_info=True)
            return None
        if result is None or isinstance(result, bool):
            return result
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

        # HOTFIX 2026-08-26 (live deadlock): read the published status BEFORE
        # taking the pane lock. get_published_status acquires the monitor lock,
        # while fuse_status holds the monitor lock and calls peek (pane lock) --
        # reading it under our lock nests the two locks in ABBA order and
        # deadlocks the server. Pre-reading fixes the order: the monitor lock is
        # never acquired while the pane lock is held. The value is at most one
        # capture older than before, which the 5s sampler tick already tolerates.
        resolved_monitor = monitor
        if resolved_monitor is None:
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            resolved_monitor = status_monitor
        published = resolved_monitor.get_published_status(terminal_id)

        with self._lock:
            state = self._state.get(terminal_id)
            if state is None:
                state = _PaneState()
                self._state[terminal_id] = state

            if captured is None:
                # No usable sample: rule 3a cannot fire, so nothing is withheld.
                # Clear the hold bound (both fields together) and report None.
                # AC-7: a non-usable sample neither OPENS nor CLOSES a veto
                # episode — leave veto_episode_open / veto_warned_at untouched.
                state.downgrade_since = None
                state.pane_hold_expired = False
                return None

            fingerprint = captured.fingerprint
            filtered_tail = captured.filtered_tail

            # F568 D12d: store the sampled facts atomically with the fingerprint.
            state.busy_marker = captured.busy_marker
            state.children_count = captured.children_count
            state.marker_rows = captured.marker_rows

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

            self._update_hold_bound(terminal_id, state, now, published)
            self._update_veto_clock(terminal_id, state, now, published, fp_changed)

            return PaneObservation(
                unchanged_count=state.unchanged_count,
                unchanged_for_s=max(0.0, now - state.last_change_monotonic),
                pane_hold_expired=state.pane_hold_expired,
                fingerprint=fingerprint,
                fp_changed=fp_changed,
                filtered_tail=filtered_tail,
                busy_marker=state.busy_marker,
                children_count=state.children_count,
                marker_rows=state.marker_rows,
            )

    def _update_hold_bound(
        self,
        terminal_id: str,
        state: _PaneState,
        now: float,
        published: "TerminalStatus | None",
    ) -> None:
        """Set/clear ``downgrade_since`` and flip ``pane_hold_expired`` (D12).

        Called ONLY on a usable sample (the no-sample path clears in ``observe``).
        The SET edge fires iff rule 3a would actually downgrade this tick:
        published status in {IDLE, COMPLETED}, no open marker, unchanged_count < K.
        Any other condition clears the bound (both fields).
        """
        k = self._stable_samples()
        would_downgrade = self._rule3a_would_downgrade(terminal_id, state, k, published)

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
        published: "TerminalStatus | None",
    ) -> bool:
        """True iff rule 3a would downgrade on this sample (the SET-edge test).

        Reads the PRE-fusion published status via ``get_published_status`` (r8
        R8-S2) — never ``get_boundary_observation``/``get_raw_status``/
        ``get_status``, which fuse and would re-enter ``observe()``.

        F568 D12d: a ledger veto (``children_count > 0``) or a marker veto
        (``busy_marker is False``) means rule 3a does NOT withhold, so the hold
        bound must not count. Reads ONLY the sampled fields on ``state`` (no
        second metadata/DB read under the pane lock — D12b's lock discipline).
        The children veto is checked FIRST, mirroring fuse_status's order (a
        spinner never overrides delegating, S2).
        """
        if state.unchanged_count >= k:
            return False
        try:
            from cli_agent_orchestrator.services.question_state import question_state

            if question_state.is_open(terminal_id):
                return False
        except Exception:
            pass
        # F568 D12d ledger veto: children in flight ⇒ rule 3 admits published
        # with "pane_delta_delegating"; nothing is withheld, so no hold episode.
        if state.children_count > 0:
            return False
        # F568 D12d marker veto: the seat's own TUI spinner is absent ⇒ rule 3
        # admits published with "pane_delta_vetoed"; nothing withheld.
        if state.busy_marker is False:
            return False
        # HOTFIX 2026-08-26: published is pre-read by observe() BEFORE the pane
        # lock is taken (lock-order fix); this method must NOT touch the monitor.
        return published in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)

    def _update_veto_clock(
        self,
        terminal_id: str,
        state: _PaneState,
        now: float,
        published: "TerminalStatus | None",
        fp_changed: bool,
    ) -> None:
        """AC-7 veto-defect episode clock (own clock, independent of the bound).

        Open edge: a usable sample with ``fp_changed`` AND published ∈ {IDLE,
        COMPLETED} AND no open question marker AND ``children_count == 0`` AND
        ``busy_marker is False``. Close edge: the first (usable) sample where any
        of those five is false. At most ONE WARNING per episode, emitted at open,
        subject to a cross-episode limiter — suppressed if ``veto_warned_at`` is
        within ``pane_delta_max_hold_s``. The line names the terminal and the
        (already truncated/stripped) ``marker_rows``.
        """
        try:
            from cli_agent_orchestrator.services.question_state import question_state

            marker_open = question_state.is_open(terminal_id)
        except Exception:
            marker_open = False

        episode_condition = (
            fp_changed
            and published in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
            and not marker_open
            and state.children_count == 0
            and state.busy_marker is False
        )

        if not episode_condition:
            # Close edge (any of the five false) — a usable sample only.
            state.veto_episode_open = False
            return

        if state.veto_episode_open:
            # Same episode, already warned once — no repeat.
            return

        # Open edge — start the episode.
        state.veto_episode_open = True

        max_hold = self._max_hold_s()
        if state.veto_warned_at is not None and (now - state.veto_warned_at) < max_hold:
            # Cross-episode limiter: a prior WARNING within the bound suppresses
            # this one (still opens the episode so a close edge can re-arm).
            return

        state.veto_warned_at = now
        logger.warning(
            "pane_delta rule-3a vetoed for %s (seat idle, no TUI spinner, "
            "children=0); marker_rows=%r",
            terminal_id,
            list(state.marker_rows),
        )

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
                busy_marker=state.busy_marker,
                children_count=state.children_count,
                marker_rows=state.marker_rows,
            )


pane_liveness = PaneLivenessService()

__all__ = ["PaneLivenessService", "PaneObservation", "pane_liveness", "PANE_LIVENESS_TAIL_LINES"]
