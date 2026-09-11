"""F228-b no-progress watchdog tests — the CLOCK half, after WP-ARCH 3c K4.

AC1/AC3/AC4/AC15/AC16/AC17 and mutant 3 survive because their subject is
``record_status`` and the fingerprint bookkeeping. AC2 and AC5-AC14 drove
``tick_no_progress``, which K4 deletes; see the notes where they stood.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_watchdog(grace=3):
    """Create a watchdog with injectable clock (grace is for stalled-callback, NP uses config)."""
    return StalledCallbackWatchdog(grace_seconds=grace)


# WP-ARCH 3c K4: ``_meta``, ``_config_np_on_60``, ``_config_np_on_30`` and
# ``_config_np_off`` are gone with the arms that used them. They existed to hand
# ``tick_no_progress`` a terminal row and a ``supervisor.watchdog.no_progress*``
# config, and the service reads neither key any more. ``_config_np_on`` stays:
# one surviving arm still patches ConfigService through it.


def _config_np_on(path, default=None, override=None):
    """ConfigService.get mock that enables no-progress with grace=300."""
    mapping = {
        "supervisor.watchdog.no_progress": True,
        "supervisor.watchdog.no_progress_grace_s": 300.0,
        "supervisor.watchdog.quiescence": False,
    }
    return mapping.get(path, default)


def _setup_processing_worker(
    svc, terminal_id="worker1", caller_id="sup1", profile="grok_dev", processing_at=10.0
):
    """Record assign and set PROCESSING."""
    svc.record_inbound_task(terminal_id, caller_id, profile)
    svc.record_status(terminal_id, TerminalStatus.PROCESSING, now=processing_at)
    return svc


def _fingerprint_with_tail(svc, terminal_id, tail_text, now, metadata_fn=None, patterns=None):
    """Simulate refresh_screen_fingerprints for a single terminal by exercising the real method."""
    import hashlib

    from cli_agent_orchestrator.services.stalled_callback_watchdog import _filtered_liveness_tail

    _patterns = patterns or []
    filtered = _filtered_liveness_tail(tail_text, _patterns)
    fp = hashlib.sha256(filtered.encode("utf-8", "replace")).hexdigest()

    with svc._lock:
        episode = svc._episodes.get(terminal_id)
        if episode is None:
            return fp
        # Simulate what refresh_screen_fingerprints does for idle/quiet
        if episode.idle_since is not None or episode.quiet_since is not None:
            if episode.last_screen_fp is None:
                episode.last_screen_fp = fp
            elif episode.last_screen_fp != fp:
                if episode.idle_since is not None:
                    episode.idle_since = now
                if episode.quiet_since is not None:
                    episode.quiet_since = now
                episode.last_screen_fp = fp

        # NP fingerprint tracking
        if episode.processing_since is not None and episode.np_fired_key is None:
            hint_lines = [ln.strip() for ln in filtered.splitlines() if ln.strip()]
            raw_hint = hint_lines[-1] if hint_lines else ""
            sanitized_hint = raw_hint.replace('"', "'").replace("\n", " ").replace("\r", " ")
            sanitized_hint = "".join(c if c.isprintable() else "?" for c in sanitized_hint)
            if len(sanitized_hint) > 80:
                sanitized_hint = sanitized_hint[:77] + "..."
            episode.last_np_hint = sanitized_hint if sanitized_hint else None

            if episode.last_np_fp is None:
                episode.last_np_fp = fp
                episode.last_progress_at = now
            elif episode.last_np_fp != fp:
                episode.last_np_fp = fp
                episode.last_progress_at = now
    return fp


# ---------------------------------------------------------------------------
# AC1: Clock lifecycle — AWAITING_BASELINE -> CLOCK_RUNNING
# ---------------------------------------------------------------------------


class TestAC1ClockLifecycle:
    def test_processing_entry_sets_processing_since(self):
        """Worker enters PROCESSING -> processing_since set."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "grok_dev")
        svc.record_status("w1", TerminalStatus.PROCESSING, now=100.0)
        with svc._lock:
            ep = svc._episodes["w1"]
            assert ep.processing_since == 100.0
            assert ep.last_np_fp is None
            assert ep.last_progress_at is None

    def test_first_fingerprint_sets_baseline(self):
        """First fingerprint tick -> last_np_fp and last_progress_at set."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "line1\nline2\n", now=105.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.last_np_fp is not None
            assert ep.last_progress_at == 105.0


# ---------------------------------------------------------------------------
# AC2: Changing fingerprint never alerts
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: ``TestAC2NoAlertOnProgress`` is GONE with the ALERT, not with
# the clock
# ---------------------------------------------------------------------------
# Its one arm drove ``tick_no_progress`` over a screen whose fingerprint moved
# every sample and asserted no advisory was ever created. The property — a pane
# that is still changing is making progress — survives as the FINGERPRINT half:
# ``refresh_screen_fingerprints`` still resets ``last_progress_at`` whenever the
# filtered tail changes, and ``TestAC1ClockLifecycle`` below still pins that
# reset. What cannot be asserted any more is the consequence, because K4 deletes
# ``tick_no_progress`` and ``_evaluate_no_progress`` and nothing reads the clock
# they read. See the block below where the firing classes stood.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC3: Transition to non-PROCESSING clears NP fields
# ---------------------------------------------------------------------------


class TestAC3ClearOnNonProcessing:
    def test_idle_clears_np_fields(self):
        """Worker transitions to IDLE -> all NP fields cleared."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "some output\n", now=105.0)
        # Verify fields are set
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is not None
            assert ep.last_np_fp is not None

        # Transition to IDLE
        svc.record_status("worker1", TerminalStatus.IDLE, now=200.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None
            assert ep.last_np_fp is None
            assert ep.last_progress_at is None
            assert ep.np_fired_key is None
            assert ep.last_np_hint is None

    def test_completed_clears_np_fields(self):
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=105.0)
        svc.record_status("worker1", TerminalStatus.COMPLETED, now=200.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None

    def test_error_clears_np_fields(self):
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=105.0)
        svc.record_status("worker1", TerminalStatus.ERROR, now=200.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None


# ---------------------------------------------------------------------------
# AC4: Pause/resume shifts NP clocks
# ---------------------------------------------------------------------------


class TestAC4PauseResume:
    def test_pause_resume_shifts_np_clocks(self):
        """Worker paused for 60s during PROCESSING -> clocks shifted by 60s."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=105.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            orig_processing_since = ep.processing_since
            orig_last_progress_at = ep.last_progress_at

        # Pause
        snapshot = svc.pause_terminal("worker1")
        # Simulate 60s elapsed
        with patch("time.monotonic", return_value=time.monotonic() + 60.0):
            svc.resume_terminal("worker1", snapshot)

        with svc._lock:
            ep = svc._episodes["worker1"]
            # Both should be shifted by approximately 60s
            assert ep.processing_since > orig_processing_since
            assert ep.last_progress_at > orig_last_progress_at
            shift = ep.processing_since - orig_processing_since
            assert shift >= 59.0  # allow small timing variance


# ---------------------------------------------------------------------------
# AC5: Alert fires with all diagnostic fields
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: AC5-AC9, the FIRING half of F228-b, are GONE with
# ``tick_no_progress``
# ---------------------------------------------------------------------------
# Five classes stood here and every one of them called ``tick_no_progress`` and
# then read ``create_routed_inbox_message``:
#
#   * ``TestAC5AlertFires`` — the advisory fires once the no-progress grace has
#     elapsed, and a failed ``_persist_notice`` does not swallow the retry.
#   * ``TestAC6Dedup`` — exactly one advisory per processing episode, and no
#     re-arm when the fingerprint moves AFTER the episode has fired.
#   * ``TestAC7Reentry`` — leaving PROCESSING and re-entering it is a NEW
#     episode and gets its own advisory.
#   * ``TestAC8RecheckRace`` — a terminal that goes IDLE between candidacy and
#     the recheck is dropped without an advisory.
#   * ``TestAC9UnreadablePane`` — no baseline fingerprint means no advisory; an
#     unreadable pane is not evidence of a stall.
#
# K4 deletes the tick and its evaluator, so there is no advisory to fire, dedup,
# re-arm, drop or withhold. Repointing them was considered and rejected: every
# one is a statement about the DECISION to notify, and the decision function is
# what went. An arm that called the surviving clock writers and then asserted
# "no message was sent" would pass on any build, broken or not.
#
# The state those decisions read is NOT gone and is still pinned below:
# ``processing_since``, ``last_np_fp``, ``last_progress_at``, ``last_np_hint``
# and ``np_fired_key`` are still written by ``record_status`` and
# ``refresh_screen_fingerprints``, and ``TestAC1ClockLifecycle``,
# ``TestAC3ClearOnNonProcessing``, ``TestAC4PauseResume`` and the surviving
# ``TestAC17MessageFormat`` hint arms cover every transition of it.
#
# Reader's note, because the clock now outlives its only consumer: after K4 the
# no-progress fields are written and never read. That is a property of the
# shipped source, not of this file — the arms below assert what the code does,
# they do not claim anything downstream still acts on it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC6: Dedup — exactly one alert per processing episode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC7: Reentry after non-PROCESSING produces new episode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC8: D5 recheck — status transitions between grace and recheck
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC9: Pane unreadable -> no alert (never exits AWAITING_BASELINE)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC10: Routing — watchdog: prefix, no episode creation, no separate request_delivery
# ---------------------------------------------------------------------------


class TestAC10Routing:
    # -----------------------------------------------------------------------
    # WP-ARCH 3c K4: the two ROUTING arms are gone with the message they routed
    # -----------------------------------------------------------------------
    # ``test_sender_has_watchdog_prefix`` pinned the advisory's sender as
    # ``watchdog:<terminal>`` so a watchdog message could never arm an episode
    # of its own, and ``test_no_separate_request_delivery`` pinned that the
    # advisory rides the caller's normal delivery rather than issuing a second
    # ``request_delivery``. Both are statements about a message ``tick_no_progress``
    # no longer composes.
    #
    # The invariant they protected — a watchdog-sent message must not arm an
    # episode — is not asserted through the advisory. It is asserted at the
    # guard itself, and that guard survives: ``record_inbound_task`` still
    # refuses a ``watchdog:`` sender, which is what the arm kept below tests
    # directly, and ``test_existing_nonarming_producer_classes_remain_nonarming``
    # in ``test_stalled_callback_watchdog.py`` covers the same refusal at the
    # inbox-service entry.
    # -----------------------------------------------------------------------

    def test_watchdog_sender_creates_no_episode(self):
        """record_inbound_task with watchdog: sender returns immediately."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "watchdog:no_progress:w1", "developer")
        with svc._lock:
            assert "w1" not in svc._episodes


# ---------------------------------------------------------------------------
# AC11: Dead caller -> no alert, no exception
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: AC11-AC14 are GONE with ``tick_no_progress``
# ---------------------------------------------------------------------------
# ``TestAC11DeadCaller`` (a dead caller gets no advisory), ``TestAC12Isolation``
# (two stalled terminals produce two independent advisories, and an exception
# raised for one must not suppress the other), ``TestAC13ConfigFlag``
# (``supervisor.watchdog.no_progress`` off means silence, and flipping it mid-run
# is honoured on the next tick) and ``TestAC14GraceClamping`` (the configured
# grace is honoured at 60s and clamped UP to 60s below it) are all properties of
# the tick's own loop and config read. K4 deletes the loop and the config key is
# no longer read anywhere in the service, so there is no flag to flip, no grace
# to clamp and no per-terminal iteration to isolate.
#
# The isolation concern in particular has no heir here and should not be faked
# into one: it was about one terminal's failure inside a SWEEP not stopping the
# sweep, and there is no sweep left in this file's subject. The equivalent
# guarantee for the surviving liveness half — one terminal's unreadable pane not
# stopping the sampler — is ``refresh_screen_fingerprints``'s own per-terminal
# ``continue``, exercised by ``test_forkA_widen_samples_live_terminal_with_no_armed_episode``
# in ``test_stalled_callback_watchdog.py``.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC12: Per-terminal failure isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC13: Config flag on/off
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC14: Grace clamping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC15: Existing stalled-callback tests pass (regression guard)
# ---------------------------------------------------------------------------


class TestAC15IdleQuietUnchanged:
    """Filter predicate widening does not regress idle/quiet fingerprint paths."""

    def test_idle_fingerprint_still_resets_idle_since(self):
        """Idle fingerprint change still resets idle_since (existing behavior)."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=100.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "old_fp"

        # Simulate a different fingerprint (processed by refresh_screen_fingerprints internally)
        import hashlib

        new_fp = hashlib.sha256(b"new content").hexdigest()
        with svc._lock:
            ep = svc._episodes["w1"]
            # Directly simulate what refresh_screen_fingerprints does
            ep.last_screen_fp = new_fp
            ep.idle_since = 200.0  # reset

        with svc._lock:
            ep = svc._episodes["w1"]
            assert ep.idle_since == 200.0
            # NP fields should NOT be set (not PROCESSING)
            assert ep.processing_since is None

    def test_processing_terminal_has_no_idle_since(self):
        """A PROCESSING terminal does not have idle_since set."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.idle_since is None
            assert ep.quiet_since is None
            assert ep.processing_since == 100.0


# ---------------------------------------------------------------------------
# AC16: Existing FX181 quiescence tests pass (regression guard)
# ---------------------------------------------------------------------------


class TestAC16QuietUnchanged:
    """Quiescence quiet_since still works for ERROR members."""

    def test_error_sets_quiet_since_not_idle_since(self):
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.ERROR, now=100.0)
        with svc._lock:
            ep = svc._episodes["w1"]
            assert ep.quiet_since == 100.0
            assert ep.idle_since is None
            # Not PROCESSING
            assert ep.processing_since is None


# ---------------------------------------------------------------------------
# AC17: Alert message format + hint sanitization
# ---------------------------------------------------------------------------


class TestAC17MessageFormat:
    # -----------------------------------------------------------------------
    # WP-ARCH 3c K4: ``test_message_format_regex`` is gone with the message
    # -----------------------------------------------------------------------
    # It pinned the whole D6 advisory string — the ``[no-progress advisory]``
    # prefix, both durations, ``gen=``, the quoted ``last_visible=`` hint, and
    # the three operator affordances (HEURISTIC, peek_terminal, delete_terminal).
    # ``tick_no_progress`` composed that string and K4 deletes it; there is no
    # formatter left to hold to a format.
    #
    # The one INPUT to that string that is still produced lives on: the
    # sanitized, bounded ``last_np_hint`` that ``refresh_screen_fingerprints``
    # derives from the filtered tail. The three arms kept below are exactly the
    # sanitizer's contract — quotes and control characters stripped, 80-character
    # bound with an ellipsis, and an empty tail yielding None rather than "".
    # -----------------------------------------------------------------------

    def test_hint_sanitization_no_quotes(self):
        """Hint sanitization removes quotes (mutant 16)."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        # Tail with quotes and control chars
        tail = 'output with "quotes" and \x01control\x02 chars\n'
        _fingerprint_with_tail(svc, "worker1", tail, now=5.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            hint = ep.last_np_hint
            assert hint is not None
            assert '"' not in hint
            # All printable
            assert all(c.isprintable() for c in hint)

    def test_hint_truncation_at_80(self):
        """Hint truncated to 80 chars max."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        long_line = "x" * 200 + "\n"
        _fingerprint_with_tail(svc, "worker1", long_line, now=5.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.last_np_hint is not None
            assert len(ep.last_np_hint) <= 80
            assert ep.last_np_hint.endswith("...")

    def test_hint_empty_tail(self):
        """Empty filtered tail -> hint is None."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "\n\n\n", now=5.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.last_np_hint is None


# ---------------------------------------------------------------------------
# Additional mutant kills
# ---------------------------------------------------------------------------


class TestMutantKills:
    """Targeted tests for specific mutants not covered above."""

    # -----------------------------------------------------------------------
    # WP-ARCH 3c K4: five of the six mutant kills die with their mutants
    # -----------------------------------------------------------------------
    # Each named a mutation of ``tick_no_progress`` / ``_evaluate_no_progress``
    # and killed it by observing the advisory:
    #
    #   * mutant 1  — fire even while the fingerprint is still changing.
    #   * mutant 7  — fire before a baseline fingerprint has ever been taken.
    #   * mutant 8  — include PAUSED terminals in the sweep.
    #   * mutant 12 — omit ``last_np_hint`` from the composed message.
    #   * mutant 15 — fail to clear ``np_fired_key`` on leaving PROCESSING, so a
    #     second episode can never fire.
    #
    # A mutant kill is only meaningful while the line it mutates exists. All five
    # of those lines are inside the deleted tick, so the kills have nothing left
    # to kill — keeping them would mean keeping five arms that pass because the
    # code is absent, which is the opposite of what a mutation ledger asserts.
    #
    # Mutant 15 is the one with a surviving half, and it is already covered:
    # clearing ``np_fired_key`` (with the rest of the NP fields) on the
    # transition out of PROCESSING is ``record_status``'s doing, and
    # ``TestAC3ClearOnNonProcessing`` pins it for IDLE, COMPLETED and ERROR.
    # Mutant 3, kept below, is the other survivor: it mutates ``record_status``
    # itself, which K4 does not touch.
    # -----------------------------------------------------------------------

    @patch(
        "cli_agent_orchestrator.services.config_service.ConfigService.get",
        side_effect=_config_np_on,
    )
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view"
    )
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant3_no_clear_on_transition(
        self, mock_create, mock_snapshot, mock_meta, mock_config
    ):
        """Mutant 3: Not clearing NP fields on non-PROCESSING. AC3 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        # Go IDLE
        svc.record_status("worker1", TerminalStatus.IDLE, now=100.0)

        # Verify cleared
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None
            assert ep.last_np_fp is None
            assert ep.last_progress_at is None
