"""F295 Half 2 — Wedge watchdog tests (AC7-AC11)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
    _Episode,
)


def _make_watchdog() -> StalledCallbackWatchdog:
    return StalledCallbackWatchdog(grace_seconds=10)


def _make_episode(
    caller_id: str = "caller1",
    profile: str = "grok_dev",
    processing_since: float | None = None,
    generation: int = 1,
) -> _Episode:
    return _Episode(
        caller_id=caller_id,
        profile=profile,
        inbound_at=time.monotonic(),
        episode_started_wall_at=datetime.now(timezone.utc),
        processing_since=processing_since,
        generation=generation,
        status=TerminalStatus.PROCESSING,
    )


# ---------------------------------------------------------------------------
# AC7: wedged grok pane with live spinner fires exactly one wedge notice
# ---------------------------------------------------------------------------


class TestWedgeArmFiringAndDedup:
    """AC7's firing arm is gone with ``tick_wedge``; the provider pattern it
    depended on is not, and is what this class still pins."""

    # -----------------------------------------------------------------------
    # WP-ARCH 3c K4: ``test_fires_once_on_grok_cli_after_age_threshold`` is GONE
    # -----------------------------------------------------------------------
    # It was the arm for the whole AC7 contract: a grok_cli episode PROCESSING
    # past ``grok_wedge_age_s`` with a fingerprint that has not moved fires
    # exactly one notice, writes ``wedge_suspect: True`` onto the terminal's
    # metadata once, and the SECOND tick of the same episode fires nothing. All
    # three halves of that — the age predicate, the metadata write and the
    # ``wedge_fired_key`` dedup — lived in ``tick_wedge`` and ``_evaluate_wedge``,
    # which K4 deletes. Nothing sets ``wedge_flagged`` or ``wedge_fired_key`` any
    # more, so there is no firing to count once.
    #
    # The dedup KEY itself is not orphaned in the same way: ``record_status``
    # still clears both wedge fields on any transition out of PROCESSING, and
    # ``TestWedgeFlagProjection.test_clears_on_status_transition`` below still
    # pins that clear. What is gone is the producer, not the reset.
    # -----------------------------------------------------------------------

    def test_f228b_arm_does_not_fire_while_spinner_animating(self):
        """With liveness_exclude_patterns, spinner animation = stable FP → NP never fires."""
        # This test verifies that GrokCliProvider.liveness_exclude_patterns is set
        from cli_agent_orchestrator.providers.grok_cli import PROCESSING_PATTERN, GrokCliProvider

        assert GrokCliProvider.liveness_exclude_patterns == [PROCESSING_PATTERN]


# ---------------------------------------------------------------------------
# AC8: liveness_exclude_patterns stabilizes fingerprint
# ---------------------------------------------------------------------------


class TestLivenessExcludePatterns:
    """Spinner-only changes produce a stable fingerprint."""

    def test_grok_provider_has_processing_pattern(self):
        from cli_agent_orchestrator.providers.grok_cli import PROCESSING_PATTERN, GrokCliProvider

        assert PROCESSING_PATTERN in GrokCliProvider.liveness_exclude_patterns


# ---------------------------------------------------------------------------
# AC9: flag-and-notify only — no key, no status write, no reap
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC10: flag reaches fleet and it clears
# ---------------------------------------------------------------------------


class TestWedgeFlagProjection:
    """wedge_suspect projects in fleet and clears on status transition."""

    def test_wedge_suspect_in_fleet(self):
        """After firing, fleet projects wedge_suspect: True."""
        from cli_agent_orchestrator.services.fleet_service import _is_wedge_suspect

        row = {"provider": "grok_cli", "metadata": {"cao": {"wedge_suspect": True}}}
        assert _is_wedge_suspect(row) is True

    def test_non_grok_returns_none(self):
        from cli_agent_orchestrator.services.fleet_service import _is_wedge_suspect

        row = {"provider": "codex_cli", "metadata": {"cao": {"wedge_suspect": True}}}
        assert _is_wedge_suspect(row) is None

    def test_no_wedge_returns_none(self):
        from cli_agent_orchestrator.services.fleet_service import _is_wedge_suspect

        row = {"provider": "grok_cli", "metadata": {"cao": {}}}
        assert _is_wedge_suspect(row) is None

    def test_clears_on_status_transition(self):
        """When status transitions from PROCESSING, wedge state clears."""
        wd = _make_watchdog()
        now = time.monotonic()
        episode = _make_episode(processing_since=now - 1000, generation=1)
        episode.wedge_fired_key = (1, now - 1000)
        episode.wedge_flagged = True

        with wd._lock:
            wd._episodes["t1"] = episode

        # Simulate status transition to IDLE
        wd.record_status("t1", TerminalStatus.IDLE)

        with wd._lock:
            ep = wd._episodes.get("t1")
            assert ep is not None
            assert ep.wedge_fired_key is None
            assert ep.wedge_flagged is False


# ---------------------------------------------------------------------------
# AC11: a reaped terminal is never flagged or announced
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D11: caller dead → fallback to supervisor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: AC9, AC11 and D11 are GONE with ``tick_wedge``
# ---------------------------------------------------------------------------
# Three classes stood here and all three asserted something about what the wedge
# TICK does when it fires, so all three die with the tick that K4 deletes:
#
#   * ``TestFlagAndNotifyOnly`` (AC9) — the arm that made the wedge detector
#     safe to ship: on firing it may flag and notify, and may NEVER send keys,
#     write a terminal status or reap. It patched the backend and asserted
#     ``send_keys`` was not called. A deleted tick sends no keys, so the
#     assertion is now true of every build, including a broken one; it is
#     exactly the vacuous arm this sweep is meant not to leave behind.
#   * ``TestReapedTerminalNotFlagged`` (AC11) — a terminal reaped between
#     candidacy and recheck must be dropped silently: metadata lookup returns
#     None, and neither the metadata write nor the notice may happen. Same
#     shape, same reason.
#   * ``TestCallerFallback`` (D11) — a wedge notice whose caller is dead is
#     re-addressed to the current supervisor mailbox rather than dropped.
#
# The D11 fallback is the one with an heir worth naming. The rule it encodes —
# a notice for a dead caller goes to the supervisor — belongs to the notice
# path, not to the wedge probe, and the notice path that survives K4 is
# ``emit_pre_delete_notice`` / ``_persist_notice``, covered by
# ``TestF128DeleteBeforeCallbackGuard`` in
# ``test/services/test_stalled_callback_watchdog.py``. The wedge-specific
# fallback had no other caller and is withdrawn with the probe.
#
# What remains in this file is real and still passes against the shipped build:
# the grok provider's ``liveness_exclude_patterns`` (AC8), the fleet projection
# of ``wedge_suspect`` (AC10), and ``record_status`` clearing the wedge fields
# on a status transition. NOTE for the reader: AC10's projection now has no
# WRITER anywhere in the tree — ``fleet_service._is_wedge_suspect`` and the TUI
# cell that ranks it survive K4, the only code that ever set the flag does not.
# The arms below pin the projection as the pure function it is, which is honest,
# but a wedged grok pane will not light it up until something writes the flag
# again.
# ---------------------------------------------------------------------------
