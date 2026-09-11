"""Stalled-callback watchdog tests — the liveness half, after WP-ARCH 3c K4.

K4 demoted this service to episode bookkeeping plus pane sampling. What is left
here follows it: the arming and settling of an episode, the poller, the
fingerprint clocks, and ``emit_pre_delete_notice`` — the one notice path that
survives. Every arm that observed the watchdog through the deleted stall sweep
is accounted for in a note where it stood.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog

# WP-ARCH 3c K4: the two WPQ4 frames are gone with the capture-race suite that
# was their only consumer. They were a RUNNING grok frame and a COMPLETED one,
# and their whole purpose was to be the pane the watchdog captured at due time,
# so that T07 could suppress on evidence of work and T10 could emit on evidence
# of completion. There is no due-time capture left to feed. The provider
# classification they exercised in passing is still covered from a real recorded
# sample below, in ``test_positive_grok_sample_still_classifies_unknown``.


def _mark_screen_sampled(svc, terminal_id="worker1"):
    svc._episodes[terminal_id].last_screen_fp = "sample"


@pytest.fixture(autouse=True)
def _reset_pane_liveness():
    """F506: the sampler is a module singleton; clear its per-terminal state
    between tests so a reused terminal id ("worker1") never inherits a stale
    fingerprint / debounce count from a prior test."""
    from cli_agent_orchestrator.services.pane_liveness import pane_liveness

    pane_liveness._state.clear()
    yield
    pane_liveness._state.clear()


def _emit_deletion_notice(svc, terminal_id="worker1"):
    """Drive the ONE notice path K4 leaves behind and return what it produced.

    ``emit_pre_delete_notice`` is now the whole of the watchdog's notice
    surface, so it is also the only place an arm can still observe the two
    episode flags that used to be read through the stall sweep: it returns None
    for an episode with ``callback_seen``, and a notice for an open one. The
    patches are the same pair ``TestF128DeleteBeforeCallbackGuard`` uses — the
    durable insert and its barrier fallback — because the decision under test is
    upstream of both.
    """
    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            return_value=None,
        ),
        patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"),
    ):
        return svc.emit_pre_delete_notice(terminal_id)


def _watchdog_grok_provider() -> GrokCliProvider:
    return GrokCliProvider(
        terminal_id="worker1",
        session_name="cao-test",
        window_name="worker1",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )


# WP-ARCH 3c K4: ``_watchdog_guard_fakes`` is gone with the capture-race suite.
# It assembled the whole due-time environment in one contextmanager — a backend
# whose ``capture_viewport`` could raise or run a callback mid-capture, a
# two-element ``get_callback_status_since`` side effect for the before/after
# durable reads, terminal metadata and a real grok provider — and nothing but
# those arms ever used it. The watchdog no longer captures a pane to decide
# anything, so there is no due-time environment to assemble.


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: the DUE EVALUATOR is gone, and with it every arm that observed
# the watchdog through a stall notice
# ---------------------------------------------------------------------------
# ``collect_due_notifications`` was this file's instrument. It is the function
# that decided a worker had gone quiet without answering, and almost everything
# below the fixtures called it and read the returned ``WatchdogNotice`` list. K4
# deletes it, ``notify_due``, ``_push_notice`` and the reservation pair, so there
# is no longer a decision to observe.
#
# Two arms died here rather than moving:
#
#   * ``test_watchdog_pushes_exactly_one_due_notification`` — the grace boundary
#     itself: nothing at t-1, exactly one notice at t, nothing after. There is no
#     grace boundary left; ``emit_pre_delete_notice`` fires on an event (the
#     delete), not on a clock.
#   * ``test_watchdog_waits_for_initial_screen_fingerprint_before_firing`` — an
#     episode with no baseline sample must never fire, however long it has been
#     idle. The GUARD it protected is not gone, it simply has no firing left to
#     block: ``refresh_screen_fingerprints`` still takes the first sample as a
#     baseline and only treats a LATER difference as movement, which the two
#     fingerprint arms kept below pin directly on the episode clock.
#
# What survives of "did this worker answer" is ``callback_seen``, and it is still
# read by three live paths — ``poll_unarmed_statuses`` and
# ``refresh_screen_fingerprints`` skip an episode that has it,
# ``emit_pre_delete_notice`` returns None on it, and ``_gc_fired_episodes``
# collects on it. The arms below observe it there instead.
# ---------------------------------------------------------------------------


def test_watchdog_polls_idle_status_when_no_post_task_status_event():
    """The poller is the watchdog's only status source when no event arrives.

    WP-ARCH 3c K4 moved where this is observed, not what it asserts. The arm
    used to read the monitor once and then watch the notice fall due three ticks
    later; the notice is gone, so it now reads the EFFECT the poll has on the
    episode. That effect is still load-bearing: ``idle_since`` is one of the
    three clocks ``refresh_screen_fingerprints`` requires before it will sample
    a terminal's pane at all, so a poll that fails to record the status leaves
    the episode invisible to the sampler.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    assert svc._episodes["worker1"].idle_since is None

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
        return_value=TerminalStatus.IDLE,
    ) as mock_get_status:
        svc.poll_unarmed_statuses(now=10.0)

    mock_get_status.assert_called_once_with("worker1")
    episode = svc._episodes["worker1"]
    assert episode.status is TerminalStatus.IDLE
    assert episode.idle_since == 10.0


def test_watchdog_polls_already_idle_episode_and_unarms_when_processing():
    """A poll that comes back PROCESSING UNARMS an already-idle episode.

    The un-arming is the point, and it survives K4 intact: ``record_status``
    clears ``idle_since`` and drops the retained fingerprint on any non-terminal
    status. The arm used to prove it by showing no notice ever fell due; it now
    reads the cleared clock, which is the state the deleted sweep was reading.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
        return_value=TerminalStatus.PROCESSING,
    ) as mock_get_status:
        svc.poll_unarmed_statuses(now=12.0)

    mock_get_status.assert_called_once_with("worker1")
    episode = svc._episodes["worker1"]
    assert episode.status is TerminalStatus.PROCESSING
    assert episode.idle_since is None
    assert episode.last_screen_fp is None


def test_watchdog_screen_fingerprint_change_resets_idle_timer():
    """A pane that is still changing restarts the idle clock; a static one does not.

    WP-ARCH 3c K4 renamed this arm (it was
    ``..._resets_idle_timer_then_static_fires``) because its second half — the
    notice that fell due once the pane went static — has no code left. The first
    half is the anti-false-idle rule itself and is untouched by the slice:
    ``refresh_screen_fingerprints`` takes the first sample as a baseline, treats
    a LATER difference as movement and restarts whichever clocks are armed, and
    leaves them alone when the sample repeats. The three ``get_history`` frames
    are unchanged, so the sampler is driven exactly as before.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    backend = MagicMock()
    backend.get_history.side_effect = ["frame 1", "frame 2", "frame 2"]
    metadata = {"id": "worker1", "tmux_session": "cao-test", "tmux_window": "win"}

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.list_all_terminals",
            return_value=[metadata],
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=backend,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=None,
        ),
    ):
        episode = svc._episodes["worker1"]
        svc.refresh_screen_fingerprints(now=10.5)
        # Baseline only: the first sample is not evidence of movement.
        assert episode.last_screen_fp is not None
        assert episode.idle_since == 10.0

        svc.refresh_screen_fingerprints(now=12.0)
        # "frame 2" differs from "frame 1" -> the idle clock restarts.
        assert episode.idle_since == 12.0

        svc.refresh_screen_fingerprints(now=14.0)
        # "frame 2" again -> a static pane must NOT restart it.
        assert episode.idle_since == 12.0

    backend.get_history.assert_any_call(
        "cao-test",
        "win",
        tail_lines=45,
        strip_escapes=True,
    )


def test_watchdog_excludes_rotating_codex_prompt_from_liveness_fingerprint():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    backend = MagicMock()
    backend.get_history.side_effect = [
        "stable output\n› Summarize recent commits\n? for shortcuts",
        "stable output\n› Explain this codebase\n? for shortcuts",
    ]
    provider = MagicMock()
    provider.liveness_exclude_patterns = [r"^\s*›", r"\?\s+for shortcuts"]
    metadata = {"id": "worker1", "tmux_session": "cao-test", "tmux_window": "win"}

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.list_all_terminals",
            return_value=[metadata],
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=backend,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        episode = svc._episodes["worker1"]
        svc.refresh_screen_fingerprints(now=10.5)
        svc.refresh_screen_fingerprints(now=12.0)

    # WP-ARCH 3c K4: the arm used to prove the filter worked by showing the
    # notice still fell due on schedule. The filter is upstream of that and is
    # untouched — ``_filtered_liveness_tail`` drops the provider's excluded lines
    # before the fingerprint is taken — so the surviving statement of the same
    # property is that the rotating prompt did not count as movement and the idle
    # clock never restarted. If the filter regresses, the second frame differs,
    # ``idle_since`` becomes 12.0 and this fails.
    assert episode.idle_since == 10.0


def test_watchdog_keeps_spinner_ticks_as_liveness_signal():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    backend = MagicMock()
    backend.get_history.side_effect = [
        "• Working (1s • esc to interrupt)\n› Summarize recent commits\n? for shortcuts",
        "• Working (2s • esc to interrupt)\n› Explain this codebase\n? for shortcuts",
    ]
    provider = MagicMock()
    provider.liveness_exclude_patterns = [r"^\s*›", r"\?\s+for shortcuts"]
    metadata = {"id": "worker1", "tmux_session": "cao-test", "tmux_window": "win"}

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.list_all_terminals",
            return_value=[metadata],
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=backend,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        episode = svc._episodes["worker1"]
        svc.refresh_screen_fingerprints(now=10.5)
        svc.refresh_screen_fingerprints(now=12.0)

    # The counterpart to the arm above, and the reason the filter has to be
    # narrow: a spinner tick is NOT in the exclude list, so it reaches the
    # fingerprint and restarts the clock. A filter wide enough to swallow it
    # would show up here as ``idle_since == 10.0``.
    assert episode.idle_since == 12.0


def test_watchdog_suppresses_notification_after_callback_to_recorded_caller():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"caller_id": "caller1"},
    ):
        svc.record_callback_if_to_caller("worker1", "caller1")

    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    # WP-ARCH 3c K4: the suppression is still real and still observable, at the
    # one notice path that survives. ``callback_seen`` is what
    # ``record_callback_if_to_caller`` sets and what ``emit_pre_delete_notice``
    # refuses to fire over, so both halves are asserted: the flag, and the
    # refusal it causes.
    assert svc._episodes["worker1"].callback_seen is True
    assert _emit_deletion_notice(svc) is None


def test_f92_barrier_routed_callback_to_mailbox_suppresses_notification():
    """F92: a callback addressed to the caller's MAILBOX must clear the episode.

    Barrier-routed replies land on ``caller_mailbox_id``, not ``caller_id``. The
    outer gate accepted either identity while the inner comparison tested
    ``caller_id`` only, so the clear was a silent no-op and the next episode fired
    a false ``idle ... without callback`` push at a worker that had already
    replied. Observed 10+ times in one session across 3 terminals / 2 providers.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"caller_id": "caller1", "caller_mailbox_id": "mb_caller1"},
    ):
        svc.record_callback_if_to_caller("worker1", "mb_caller1")

    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    # Arm everything a firing needs, so the empty result below can ONLY be caused
    # by the callback having cleared the episode. Without this the assertion is
    # vacuous -- it passes on an unfixed build because nothing could fire anyway.
    _mark_screen_sampled(svc)

    assert svc._episodes["worker1"].callback_seen is True
    assert _emit_deletion_notice(svc) is None


def test_f92_fix_does_not_silence_the_genuine_hang_push():
    """F92 guard: a worker assigned work that NEVER replies must still notify.

    A fix that suppresses everything is worse than the bug it closes. Same setup
    as the test above, minus the callback. WP-ARCH 3c K4: the notice this arm
    demands is now the deletion notice rather than the stall notice — that is
    the only one left — but the guard is the same one, and it is the reason the
    two arms around it are not vacuous.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    assert svc._episodes["worker1"].callback_seen is False
    notice = _emit_deletion_notice(svc)
    assert notice is not None
    assert notice.caller_id == "caller1"


def test_f92_callback_from_an_unrelated_third_party_still_does_not_clear():
    """F92 scope guard: widening the comparison must not accept a stranger.

    ``mb_other`` is a real mailbox id, just not this episode's caller -- the outer
    gate rejects it, and the episode must remain armed.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"caller_id": "caller1", "caller_mailbox_id": "mb_caller1"},
    ):
        svc.record_callback_if_to_caller("worker1", "mb_other")

    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    assert svc._episodes["worker1"].callback_seen is False
    assert _emit_deletion_notice(svc) is not None


def test_f92_callback_to_new_current_caller_does_not_clear_older_episode():
    """A callback for the current caller cannot clear an older caller's episode."""
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    episode = svc._episodes["worker1"]

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"caller_id": "caller2", "caller_mailbox_id": "mb_caller2"},
    ):
        svc.record_callback_if_to_caller("worker1", "caller2")

    assert episode.callback_seen is False, "old caller episode was cleared by new caller callback"

    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    notice = _emit_deletion_notice(svc)
    assert notice is not None
    # The episode still belongs to caller1, and the notice is addressed to the
    # episode's caller rather than to whoever the terminal's CURRENT caller is.
    assert notice.caller_id == "caller1"


def test_msgtrace_confirmed_commit_performs_watchdog_operations_exactly_once():
    """FX7 operations are grouped at the confirmed-delivery commit boundary."""
    from cli_agent_orchestrator.services.inbox_service import InboxService

    service = InboxService()
    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
    ) as watchdog:
        watchdog.has_episode.return_value = True
        service._commit_watchdog_ops(
            "worker1",
            "caller1",
            OrchestrationType.SEND_MESSAGE,
            {"caller_id": "caller1", "agent_profile": "developer"},
        )
        watchdog.record_callback_if_to_caller.assert_called_once_with("caller1", "worker1")
        watchdog.record_inbound_task.assert_called_once_with("worker1", "caller1", "developer")


def test_parked_commit_still_settles_sender_and_never_clears_existing_episode():
    from cli_agent_orchestrator.services.inbox_service import InboxService

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
    ) as watchdog:
        InboxService()._commit_watchdog_ops(
            "worker1",
            "caller1",
            OrchestrationType.SEND_MESSAGE,
            {"caller_id": "caller1", "agent_profile": "developer"},
            park_warm=True,
        )
    watchdog.record_callback_if_to_caller.assert_called_once_with("caller1", "worker1")
    watchdog.record_inbound_task.assert_not_called()
    watchdog.clear_terminal.assert_not_called()


@pytest.mark.parametrize(
    ("sender_id", "orchestration_type"),
    [
        ("watchdog:T", OrchestrationType.SEND_MESSAGE),
        ("barrier-alert:7", OrchestrationType.SEND_MESSAGE),
        ("mailbox-digest", OrchestrationType.MAILBOX_DIGEST),
    ],
)
def test_existing_nonarming_producer_classes_remain_nonarming(sender_id, orchestration_type):
    from cli_agent_orchestrator.services.inbox_service import InboxService

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
    ) as watchdog:
        InboxService()._commit_watchdog_ops(
            "worker1",
            sender_id,
            orchestration_type,
            {"caller_id": "caller1", "agent_profile": "developer"},
        )
    watchdog.record_inbound_task.assert_not_called()


def test_watchdog_resets_on_new_task_after_firing():
    """A fired-and-answered episode does not deafen the worker for good.

    WP-ARCH 3c K4: the firing and the second notice were both stall notices;
    they are now both deletion notices, which is the same latch read through the
    only emitter left. Everything between them is untouched service code —
    ``record_callback_if_to_caller`` settling the fired episode,
    ``record_inbound_task`` refusing to reuse it and allocating the next
    generation — and the ``source_generation`` on the second notice is what
    proves the new episode, not the old one, is what fired.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    first = _emit_deletion_notice(svc)
    assert first is not None and first.source_generation == 1

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"caller_id": "caller1"},
    ):
        svc.record_callback_if_to_caller("worker1", "caller1")

    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=20.0)
    _mark_screen_sampled(svc)

    second = _emit_deletion_notice(svc)
    assert second is not None
    assert second.source_generation == 2


def test_caller_messages_replace_fired_episode_with_fresh_alarm():
    """Three further assignments after a firing produce ONE replacement, not three.

    WP-ARCH 3c K4: the firing is driven through ``emit_pre_delete_notice``
    instead of the deleted sweep, and the closing "the replacement does not fire
    again" assertion is now the same emitter returning None on the second call.
    The generation arithmetic in between is ``record_inbound_task``'s and is
    untouched: a fired episode is replaced exactly once, and the two further
    assignments JOIN the replacement rather than allocating a third generation.
    """
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    assert _emit_deletion_notice(svc) is not None
    episode = svc._episodes["worker1"]
    started = episode.episode_started_wall_at
    for _ in range(3):
        svc.record_inbound_task("worker1", "caller1", "developer")
    replacement = svc._episodes["worker1"]
    assert replacement is not episode
    assert replacement.generation == episode.generation + 1
    assert not replacement.fired
    assert replacement.episode_started_wall_at != started
    assert replacement.last_join_wall_at is not None


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: the DUE-TIME CAPTURE RACE suite is gone with the capture
# ---------------------------------------------------------------------------
# Ten arms stood here, and they were the most valuable thing in this file: the
# window between "this episode looks due" and "the notice is written" was where
# the watchdog's real bugs lived, so each one drove a concurrent mutation during
# the pane capture and asserted the decision was abandoned rather than acted on.
#
#   * T07 running frame suppresses and re-arms the grace; T10 completed frame
#     emits; a capture EXCEPTION emits (fail loud, not silent).
#   * The episode is replaced mid-capture -> candidate dropped, replacement NOT
#     re-armed from the stale decision.
#   * The same episode goes PROCESSING mid-capture -> dropped, no emission.
#   * The same episode's fingerprint/grace resets mid-capture -> dropped.
#   * A callback commits mid-capture -> dropped, and the callback thread is not
#     starved by the watchdog holding its lock across the capture.
#   * Provisional (PENDING/DELIVERING) callback rows suppress and are re-queried
#     next tick; durable (DELIVERED/DIGESTED) rows suppress and are NOT
#     re-queried; the second read during capture still clears.
#   * ``test_join_keeps_first_assignment_as_d4_suppression_lower_bound`` — a
#     re-join must query the durable callback log from the FIRST assignment's
#     timestamp, not the latest, or a reply to the first message is invisible.
#
# All ten are properties of a decision function that no longer exists. There is
# no due-time capture in the shipped watchdog: ``refresh_screen_fingerprints``
# samples on a schedule and writes clocks, and it never decides anything, so
# there is no decision for a concurrent mutation to invalidate. Repointing them
# at the sampler would change what they assert, not where they assert it.
#
# The one member of this family with a surviving heir is the durable-callback
# read. ``emit_pre_delete_notice`` still performs exactly that F310 query —
# ``get_callback_status_since(terminal_id, caller_id, episode_started)`` outside
# the lock, then a re-check of generation and flags under it before latching
# ``fired`` — and it is the same re-check-after-a-blocking-read shape. Its arms
# are ``TestF128DeleteBeforeCallbackGuard`` at the end of this file.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: ``test_watchdog_prunes_deleted_terminal_without_push`` is gone
# with the prune
# ---------------------------------------------------------------------------
# An armed episode whose terminal has been deleted (metadata lookup returns None)
# had to be dropped silently: no notice to a caller about a worker that no longer
# exists, and no episode left behind. The prune was a branch INSIDE
# ``collect_due_notifications`` — it was the sweep's own housekeeping — and K4
# deletes the sweep.
#
# There is no level-triggered prune left to repoint at. Episode removal in the
# shipped build is event-driven: ``clear_terminal`` at deletion, and
# ``_gc_fired_episodes`` for an episode that has both fired and seen its
# callback. Neither notices a terminal that vanished without either event, so a
# stale episode now survives until one of them happens. That is a real behaviour
# change of the slice, not a gap in this file, and it is recorded here rather
# than papered over with an arm that asserts something weaker.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: ``test_notify_due_sends_only_to_caller`` is gone with
# ``notify_due``
# ---------------------------------------------------------------------------
# It pinned the routing of a stall notice: one ``create_routed_inbox_message``
# addressed from ``watchdog:<worker>`` to the episode's caller, and exactly one
# ``request_delivery`` for that caller — never a broadcast, never a second
# delivery. ``notify_due`` is deleted, so nothing routes a stall notice.
#
# The addressing rule itself did not change and is still enforced on the notice
# that survives: ``_persist_notice`` writes ``notice.caller_id`` and nothing
# else, and ``TestF128DeleteBeforeCallbackGuard::test_open_episode_emits_notice``
# asserts the returned notice carries the episode's caller.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: the WAITING-INBOX alert and the relational (blocker-chain)
# suite are gone with their ticks
# ---------------------------------------------------------------------------
# ``TestWaitingInboxAlert`` (arms a-n plus the clear_terminal teardown) was the
# whole contract of ``tick_waiting_inbox``: a terminal sitting in
# WAITING_USER_ANSWER with pending inbox rows pushes exactly once to its caller
# after the grace, obeys a per-terminal repeat floor, is suppressed by each
# auto-responder gate, is pruned when the terminal is deleted, warns and
# permanently suppresses on an invalid caller, and commits its episode and floor
# even when the transport throws. K4 deletes that tick, the
# ``WaitingInboxEpisode`` type it kept its state in, and the
# ``_waiting_inbox_episodes`` / ``_waiting_inbox_last_push`` dicts the teardown
# arm asserted against.
#
# The relational arms that followed it (blocker suppression preserving the
# original clock, order-independence of the blocker set, the safety-net repeat on
# the OLDEST inbound clock, the phase-P/phase-A frame probes, and the trigger-A/
# trigger-B dedup and rollback arms) all drove ``collect_due_notifications`` and
# ``notify_due`` over a caller->worker->target chain. Both are deleted, and so
# are the chain reservation (``_reserve_chain_notice`` /
# ``_release_chain_reservation``) and the auto-resume pair
# (``_auto_resume_enabled`` / ``_execute_auto_resume``) that two of them patched.
#
# Nothing here was re-homed, and it is worth being exact about why rather than
# pointing at a replacement that does not exist: the waiting-inbox alert and the
# chain escalation are WITHDRAWN by WP-ARCH 3c, not moved. An unread supervisor
# inbox is the delivery queue's problem now, and the queue's own arms live under
# ``test/app/delivery/``.
#
# Two helpers went with them — ``_waiting_inbox_fakes`` (pending rows, status,
# auto-responder gate and the grace constant) and ``_relational_watchdog_fakes``
# / ``_arm_watchdog_episode`` (a live-terminal set plus a patched
# ``_fresh_frame_decides_running``). READER'S NOTE: K4 leaves
# ``_fresh_frame_decides_running`` and ``_blockers_locked`` in the service with
# no caller at all — ``collect_due_notifications`` was the only one. They are
# dead code in the shipped build, and this file deliberately does not invent
# coverage for them.
# ---------------------------------------------------------------------------


def test_positive_grok_sample_still_classifies_unknown():
    """A recorded grok roster-flap pane must classify UNKNOWN, not IDLE.

    This is the sample-backed half of what used to be
    ``test_positive_grok_sample_keeps_existing_alarm_class``, renamed by
    WP-ARCH 3c K4 because there is no alarm class left to keep. The pane is a
    real capture from the 2026-07-20 incident, kept in the fixtures tree
    precisely so a classifier change cannot quietly turn it into a ready prompt.
    The classification is what the whole liveness half of the watchdog reads:
    ``_fresh_frame_decides_running`` and ``status_monitor.resync_from_pane_tail``
    both consume it, so UNKNOWN here is the difference between "we do not know"
    and a false IDLE.

    The second half of the old arm drove ``collect_due_notifications`` over the
    same frame and asserted the stall notice was worded unchanged. That function
    and that notice are deleted; nothing about the recorded sample is lost with
    them, because the sample's value was always the classification.
    """
    sample = (
        Path(__file__).parents[1]
        / "fixtures/error-pane-samples/2026-07-20-grok-roster-flap-d86a724d.txt"
    )
    rows = sample.read_bytes().decode("utf-8").splitlines()
    classification = _watchdog_grok_provider().classify_screen(rows)
    assert classification.status == TerminalStatus.UNKNOWN


class TestF128DeleteBeforeCallbackGuard:
    """Tests for emit_pre_delete_notice (F128)."""

    def test_open_episode_emits_notice(self):
        """AC1: open episode -> one WatchdogNotice returned with correct fields."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "insert_barrier_escalation_message",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"
            ) as mock_create,
        ):
            notice = svc.emit_pre_delete_notice("w1")
        assert notice is not None
        assert notice.caller_id == "caller1"
        assert notice.kind == "deletion"
        assert "w1" in notice.message
        assert "developer" in notice.message
        mock_create.assert_called_once()

    def test_callback_seen_no_notice(self):
        """AC2: callback_seen=True -> None."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        svc._episodes["w1"].callback_seen = True
        notice = svc.emit_pre_delete_notice("w1")
        assert notice is None

    def test_already_fired_no_notice(self):
        """AC3/AC6: fired=True -> None (no double-notify)."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        svc._episodes["w1"].fired = True
        notice = svc.emit_pre_delete_notice("w1")
        assert notice is None

    def test_no_episode_no_notice(self):
        """AC4: no episode -> None."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        notice = svc.emit_pre_delete_notice("w1")
        assert notice is None

    def test_persist_failure_propagates_to_caller(self):
        """AC8: _persist_notice raises via barrier path -> exception propagates
        (Design B). episode.fired=True already set under _lock."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            side_effect=RuntimeError("db down"),
        ):
            with pytest.raises(RuntimeError, match="db down"):
                svc.emit_pre_delete_notice("w1")
        # fired=True was set before _persist_notice was called
        assert svc._episodes["w1"].fired is True

    def test_persist_failure_on_fallback_propagates(self):
        """AC8: create_routed_inbox_message raises -> exception propagates
        (Design B). Covers the common non-barrier supervisor path.
        episode.fired=True already set under _lock."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "insert_barrier_escalation_message",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
                side_effect=RuntimeError("db down"),
            ),
        ):
            with pytest.raises(RuntimeError, match="db down"):
                svc.emit_pre_delete_notice("w1")
        assert svc._episodes["w1"].fired is True

    # -----------------------------------------------------------------------
    # WP-ARCH 3c K4: ``test_fired_prevents_subsequent_collect_due`` is gone
    # -----------------------------------------------------------------------
    # It was the integration half of AC6: after ``emit_pre_delete_notice`` has
    # latched ``fired``, the periodic sweep must not notify a second time about
    # the same episode. There is no periodic sweep left to skip.
    #
    # The half that is still reachable is kept above:
    # ``test_already_fired_no_notice`` asserts the same ``fired`` latch turns the
    # NEXT call to ``emit_pre_delete_notice`` into None, which is the only
    # double-notify this build can still have.
    # -----------------------------------------------------------------------

    def test_cascade_multiple_children(self):
        """AC9: K open episodes in cascade -> K notices."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        svc.record_inbound_task("w2", "caller1", "reviewer")
        svc.record_inbound_task("w3", "caller1", "developer")
        svc._episodes["w2"].callback_seen = True  # w2 already acked
        results = []
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "insert_barrier_escalation_message",
                return_value=None,
            ),
            patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"),
        ):
            for tid in ["w1", "w2", "w3"]:
                results.append(svc.emit_pre_delete_notice(tid))
        assert results[0] is not None  # w1: open
        assert results[1] is None  # w2: callback_seen
        assert results[2] is not None  # w3: open


def test_forkA_widen_samples_live_terminal_with_no_armed_episode():
    """F506 Fork-A (AC17 / #361 shape) BITING test at the WATCHDOG level.

    A LIVE terminal that has NO armed episode must still be sampled by
    refresh_screen_fingerprints — this is the #361 false-idle shape (no episode
    armed). The live terminal id is DISTINCT from any episode, so it is present
    ONLY via the Fork-A live_ids union.

    BITES: revert the widen in stalled_callback_watchdog.refresh_screen_
    fingerprints to `sample_ids = list(armed_episode_ids)` (drop the live_ids
    union) and this fails — the episode-free live terminal is never sampled.
    """
    from cli_agent_orchestrator.services.pane_liveness import pane_liveness

    pane_liveness._state.clear()
    svc = StalledCallbackWatchdog(grace_seconds=3)
    # NO episodes at all — self._episodes is empty (the #361 incident shape).
    assert not svc._episodes

    live_meta = {"id": "live0001", "tmux_session": "cao-test", "tmux_window": "w-live"}
    backend = MagicMock()
    backend.get_history.return_value = "live pane content"
    try:
        with (
            patch(
                "cli_agent_orchestrator.clients.database.list_all_terminals",
                return_value=[live_meta],
            ),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value=live_meta,
            ),
            patch(
                "cli_agent_orchestrator.backends.registry.get_backend",
                return_value=backend,
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
                return_value=None,
            ),
        ):
            svc.refresh_screen_fingerprints(now=10.0)

        # The episode-free live terminal WAS sampled (Fork-A widen).
        assert "live0001" in pane_liveness._state
        assert pane_liveness.peek("live0001", now=10.0) is not None
    finally:
        pane_liveness._state.clear()


# -- WP-ARCH 3c K4: the episode map must not grow without bound ---------------


class TestEvictVanishedEpisodes:
    """The eviction K4 nearly deleted by accident.

    Before slice 4 this happened inside ``collect_due_notifications``, as a side
    effect of resolving each candidate's metadata. K4 deletes that notifier, and
    without an explicit sweep the episode map leaks: ``has_episode`` is read by
    ``inbox_service`` to decide whether a terminal is mid-episode, so a leaked
    entry answers True forever for a terminal that no longer exists.

    The two survivors that also remove episodes are EVENT-driven —
    ``clear_terminal`` needs a delete to be observed, ``_gc_fired_episodes``
    needs the callback to arrive. A terminal that vanishes without either event
    is precisely what these arms cover.
    """

    def _armed(self, monkeypatch, live: set[str]):
        from cli_agent_orchestrator.services import stalled_callback_watchdog as mod

        wd = mod.StalledCallbackWatchdog()
        monkeypatch.setattr(mod, "terminal_exists", lambda tid: tid in live)
        monkeypatch.setattr(mod, "get_terminal_metadata", lambda tid: {"metadata": {}})
        for tid in ("t-gone", "t-live"):
            wd.record_inbound_task(tid, "sup-1", "developer")
        return wd

    def test_an_episode_whose_terminal_vanished_is_evicted(self, monkeypatch):
        wd = self._armed(monkeypatch, live={"t-live"})

        evicted = wd.evict_vanished_episodes()

        assert evicted == ["t-gone"]
        assert wd.has_episode("t-gone") is False

    def test_a_live_terminal_keeps_its_episode(self, monkeypatch):
        """The negative control, and the one that matters.

        An eviction that cleared everything would satisfy the arm above while
        destroying every in-flight episode on the next tick.
        """
        wd = self._armed(monkeypatch, live={"t-live"})

        wd.evict_vanished_episodes()

        assert wd.has_episode("t-live") is True

    def test_a_sweep_with_nothing_to_evict_is_a_no_op(self, monkeypatch):
        wd = self._armed(monkeypatch, live={"t-gone", "t-live"})

        assert wd.evict_vanished_episodes() == []
        assert wd.has_episode("t-gone") is True
        assert wd.has_episode("t-live") is True

    def test_the_sweep_is_scheduled_by_the_run_loop(self):
        """An eviction nothing calls is the leak with extra steps."""
        import inspect

        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            StalledCallbackWatchdog,
        )

        assert "evict_vanished_episodes" in inspect.getsource(StalledCallbackWatchdog.run)
