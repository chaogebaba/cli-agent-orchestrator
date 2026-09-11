"""The status cutover at the legacy seam (WP-ARCH phase 2, slice 3).

D1 is one decision with two halves — the projection starts publishing and the
pane path stops — and every test here is about one of those halves or about the
predicate that decides which terminals they apply to.  The ``off`` arm is
asserted as often as the ``on`` arm, because "no behaviour change while the
switch is unset" is the property the whole strangler rests on, and with the
predicate absent it is meant to be true by construction rather than by care.

The monitor is constructed directly rather than through the composition root:
these are tests of the SEAM, and a test that had to boot the server to reach it
would be testing the boot.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

TERMINAL = "t-cut"


class _View:
    """``core.ports.SourceHealthView``: one question, one answer."""

    def __init__(self, projected: set[str] | None = None) -> None:
        self.projected = projected if projected is not None else set()

    def is_projected(self, terminal_id: str) -> bool:
        return terminal_id in self.projected


def _monitor(projected: set[str] | None = None) -> StatusMonitor:
    monitor = StatusMonitor()
    if projected is not None:
        monitor.enable_projection(_View(projected))
    return monitor


def _metadata() -> dict[str, object]:
    return {
        "lifecycle_generation": 1,
        "tmux_window": "w1",
        "provider": "codex",
        "tmux_session": "s1",
    }


# ------------------------------------------------------------- the predicate


def test_with_no_view_no_terminal_is_projected() -> None:
    """The switch-off shape, and the reason it needs no other guard.

    ``_is_projected`` is ``None`` on every boot until an operator turns the
    cutover on, so every suppression below is unreachable rather than merely
    unused.
    """
    monitor = StatusMonitor()

    assert monitor._projected(TERMINAL) is False


def test_the_view_decides_per_terminal() -> None:
    monitor = _monitor({TERMINAL})

    assert monitor._projected(TERMINAL) is True
    assert monitor._projected("someone-else") is False


def test_a_view_that_raises_reads_as_not_projected() -> None:
    """The failure direction is the safe one, everywhere.

    A predicate that answered "projected" when it could not be evaluated would
    suppress the pane path for a terminal with nothing publishing in its place —
    inverting I1 rather than enforcing it.
    """

    class _Hostile:
        def is_projected(self, terminal_id: str) -> bool:
            raise RuntimeError("view unavailable")

    monitor = StatusMonitor()
    monitor.enable_projection(_Hostile())

    assert monitor._projected(TERMINAL) is False


def test_disable_returns_the_whole_fleet_to_the_pane() -> None:
    """AC-2b case 11c: with the projector stopped, EVERY terminal reads False."""
    monitor = _monitor({TERMINAL, "t-two"})

    monitor.disable_projection()

    assert monitor._projected(TERMINAL) is False
    assert monitor._projected("t-two") is False


# --------------------------------------------------- the read-time bypass (D1d)


@pytest.mark.parametrize(
    "published",
    [TerminalStatus.IDLE, TerminalStatus.COMPLETED, TerminalStatus.PROCESSING],
)
def test_fusion_is_bypassed_for_a_projected_terminal(published: TerminalStatus) -> None:
    """AC-2b case 10.  Rule 3a would turn a published IDLE into PROCESSING on
    pane evidence alone; that is #485 arriving through the seam this phase built.

    The sample below is what rule 3a reads: a usable observation whose
    ``unchanged_count`` is under the stable-sample threshold.  With the gate in
    place the status does not move and the reason is ``None``.
    """
    monitor = _monitor({TERMINAL})
    sample = MagicMock(unchanged_count=1, pane_hold_expired=False, children_count=0)
    sample.busy_marker = None
    sample.filtered_tail = ""

    with patch("cli_agent_orchestrator.services.pane_liveness.pane_liveness") as liveness:
        liveness.peek.return_value = sample
        fused, reason = monitor.fuse_status(TERMINAL, published)

    assert fused is published
    assert reason is None
    liveness.peek.assert_not_called()


def test_an_unprojected_terminal_still_fuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2b case 6, at the read seam: I7 promises the pane keeps every rule.

    The same inputs that the projected terminal above ignored must still move an
    unprojected one, or the cutover damaged the fallback instead of demoting it.
    """
    monitor = _monitor(set())
    sample = MagicMock(unchanged_count=1, pane_hold_expired=False, children_count=0)
    sample.busy_marker = None

    with patch("cli_agent_orchestrator.services.pane_liveness.pane_liveness") as liveness:
        liveness.peek.return_value = sample
        fused, reason = monitor.fuse_status(TERMINAL, TerminalStatus.IDLE)

    assert fused is TerminalStatus.PROCESSING
    assert reason == "pane_delta"


def test_the_bypass_does_not_invent_a_status() -> None:
    """``None`` in, ``None`` out — the precondition every rule shares."""
    monitor = _monitor({TERMINAL})

    assert monitor.fuse_status(TERMINAL, None) == (None, None)


# ------------------------------------------------------ the publisher (D1, I2)


def test_publish_projection_writes_the_latch_and_the_observation() -> None:
    monitor = _monitor({TERMINAL})
    monitor._status_fusion_reason[TERMINAL] = "resync_after_drop"

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(monitor, "_announce_published") as announce,
    ):
        published = monitor.publish_projection(
            TERMINAL,
            TerminalStatus.IDLE,
            event_id="01EVENT",
            worker_state="idle",
            since="2026-09-11T10:00:00+00:00",
        )

    assert published is True
    # The single writer of value: what every getter reads is now the projection.
    assert monitor.get_published_status(TERMINAL) is TerminalStatus.IDLE
    # A stored pane-derived label must not ride along on an observation no rule
    # touched — ``resync_after_drop`` is written outside the fusion path.
    assert TERMINAL not in monitor._status_fusion_reason
    announce.assert_called_once()


def test_the_observation_names_the_event_that_caused_it() -> None:
    """AC-2b case 8, the half that lives in the publish.

    I2 is "the status a consumer reads can be traced to the event that caused
    it", and this field is the whole of it: ``cao diag --why`` resolves the id
    back to the worker's own record.
    """
    monitor = _monitor({TERMINAL})
    captured: list[object] = []

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(monitor, "_announce_published"),
        patch.object(
            monitor._receiver_state_store,
            "publish_observation",
            lambda observation, **kwargs: captured.append(observation),
        ),
    ):
        monitor.publish_projection(
            TERMINAL,
            TerminalStatus.PROCESSING,
            event_id="01CAUSE",
            worker_state="busy",
            since="2026-09-11T10:00:00+00:00",
        )

    observation = captured[0]
    assert observation.origin == "worker_truth"
    assert observation.latched_status is TerminalStatus.PROCESSING
    assert observation.projection_evidence is not None
    assert observation.projection_evidence.event_id == "01CAUSE"
    # The un-narrowed answer rides too: two states publish as ``processing``.
    assert observation.projection_evidence.worker_state == "busy"


def test_a_publish_without_metadata_is_declined_not_raised() -> None:
    """The publisher runs inside the fold; it may never raise into it."""
    monitor = _monitor({TERMINAL})

    with patch("cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=None):
        assert monitor.publish_projection(TERMINAL, TerminalStatus.IDLE) is False


def test_a_publish_that_fails_leaves_the_pane_in_charge() -> None:
    monitor = _monitor({TERMINAL})

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(monitor, "_publish_observation", side_effect=RuntimeError("store down")),
    ):
        assert monitor.publish_projection(TERMINAL, TerminalStatus.IDLE) is False


# ------------------------------------------- the write-side suppression (D1)


def _detect(monitor: StatusMonitor, status: TerminalStatus) -> list[object]:
    """Run one classification pass, capturing what the pane path published."""
    published: list[object] = []
    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(
            monitor, "_publish_observation", lambda *args, **kwargs: published.append(kwargs)
        ),
        patch.object(monitor, "_announce_published"),
    ):
        monitor._apply_detection(TERMINAL, status)
    return published


def test_a_projected_terminal_does_not_publish_from_the_pane() -> None:
    """D1's second half.  Adding a writer without removing one buys a race: the
    slot both land in is last-write-wins and the pane writer fires per output
    chunk."""
    monitor = _monitor({TERMINAL})

    assert _detect(monitor, TerminalStatus.PROCESSING) == []


def test_a_projected_terminal_does_not_move_the_latch() -> None:
    """The publisher is the single writer of VALUE.

    ``_last_status`` is what ``get_published_status`` returns and what fusion
    reads, so a pane path that kept writing it would leave two producers of one
    value with no rule saying which wins.
    """
    monitor = _monitor({TERMINAL})
    monitor._last_status[TERMINAL] = TerminalStatus.IDLE

    _detect(monitor, TerminalStatus.PROCESSING)

    assert monitor.get_published_status(TERMINAL) is TerminalStatus.IDLE


def test_an_unprojected_terminal_publishes_exactly_as_before() -> None:
    """I7 at the write seam: the fallback is demoted, not damaged."""
    monitor = _monitor(set())

    published = _detect(monitor, TerminalStatus.PROCESSING)

    assert len(published) == 1
    assert published[0]["latched_status"] is TerminalStatus.PROCESSING
    assert monitor.get_published_status(TERMINAL) is TerminalStatus.PROCESSING


def test_the_sticky_latch_does_not_apply_to_a_projected_terminal() -> None:
    """D1b.  The latch exists to stop an unreliable READING from flapping, and a
    fold is not a reading — it is an ordered event whose every cell the
    transition table has already classified.

    Applying stickiness on top would let a ``turn.started`` from the worker's own
    record be refused because the pane had last been read as idle, which is
    #439's mechanism arriving through the new path.  The pane pass simply has no
    opinion here: it neither rejects nor latches.
    """
    monitor = _monitor({TERMINAL})
    monitor._last_status[TERMINAL] = TerminalStatus.COMPLETED

    _detect(monitor, TerminalStatus.PROCESSING)  # a downgrade the latch would refuse

    assert monitor.get_published_status(TERMINAL) is TerminalStatus.COMPLETED
    assert monitor._allow_processing_revert.get(TERMINAL) is None


def test_the_pane_reading_is_still_recorded_for_a_projected_terminal() -> None:
    """D1c: the classifier keeps running and its reading keeps a record.

    And the record is the PANE's reading, not the projection's — reading the
    latch here would hand D5's comparison the projection's own answer, and the
    check that exists for exactly these terminals would agree with itself
    forever.
    """
    monitor = _monitor({TERMINAL})
    monitor._last_status[TERMINAL] = TerminalStatus.IDLE
    recorded: list[object] = []

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch(
            "cli_agent_orchestrator.adapters.truth.pane_classification.record_pane_classification",
            lambda terminal_id, latched, *args, **kwargs: recorded.append(latched),
        ),
        patch.object(monitor, "_announce_published"),
    ):
        monitor._apply_detection(TERMINAL, TerminalStatus.PROCESSING)

    assert recorded == [TerminalStatus.PROCESSING]


# ------------------------------------------- the announce is edge-gated (B1)


def _publish(monitor: StatusMonitor, status: TerminalStatus, event_id: str) -> None:
    with patch(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=_metadata()
    ):
        monitor.publish_projection(
            TERMINAL, status, event_id=event_id, worker_state="", since="2026-09-11T10:00:00+00:00"
        )


def test_two_projection_transitions_onto_one_status_announce_once() -> None:
    """B1.  ``session.started`` then ``turn.started`` is the ordinary case.

    The projection's vocabulary is wider than the legacy one — ``starting``,
    ``busy`` and ``capped`` all publish as ``processing`` — so a real transition
    is routinely NOT a change of published status.  Announcing on every applied
    fold fires all four consumers twice for one move, and one of them is F611's
    "one event per terminal transition" seam by name.

    The OBSERVATION still publishes both times: its evidence names a different
    ``status.transition`` each time, which is the chain ``cao diag --why`` walks.
    """
    monitor = _monitor({TERMINAL})
    observations: list[object] = []

    with (
        patch.object(monitor, "_announce_published") as announce,
        patch.object(
            monitor._receiver_state_store,
            "publish_observation",
            lambda observation, **kwargs: observations.append(observation),
        ),
    ):
        _publish(monitor, TerminalStatus.PROCESSING, "01STARTING")  # starting
        _publish(monitor, TerminalStatus.PROCESSING, "01BUSY")  # busy

    assert announce.call_count == 1
    assert len(observations) == 2
    assert [o.projection_evidence.event_id for o in observations] == ["01STARTING", "01BUSY"]


def test_a_real_status_change_still_announces() -> None:
    monitor = _monitor({TERMINAL})

    with patch.object(monitor, "_announce_published") as announce:
        _publish(monitor, TerminalStatus.PROCESSING, "01BUSY")
        _publish(monitor, TerminalStatus.IDLE, "01IDLE")

    assert announce.call_count == 2


def test_the_first_publish_of_a_terminals_life_announces() -> None:
    """No previous latch is not "unchanged": nothing has been announced yet."""
    monitor = _monitor({TERMINAL})

    with patch.object(monitor, "_announce_published") as announce:
        _publish(monitor, TerminalStatus.IDLE, "01FIRST")

    assert announce.call_count == 1


def test_the_pane_path_has_the_same_edge_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule being matched, asserted rather than assumed.

    One writer replacing another has to keep the edge semantics the replaced
    writer had, so the projected path is only correct here if the pane path is
    the same shape — which is what makes this a regression test for both.
    """
    monitor = _monitor(set())

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(monitor, "_publish_observation"),
        patch.object(monitor, "_announce_published") as announce,
    ):
        monitor._apply_detection(TERMINAL, TerminalStatus.PROCESSING)
        monitor._apply_detection(TERMINAL, TerminalStatus.PROCESSING)

    assert announce.call_count == 1


def test_the_monitor_records_which_producer_wrote_the_status() -> None:
    """N7's mechanism: one marker, written by both writers, read by the fleet."""
    monitor = _monitor({TERMINAL})

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(monitor, "_announce_published"),
    ):
        monitor.publish_projection(TERMINAL, TerminalStatus.PROCESSING, event_id="01E")
    assert monitor.status_written_by_projection(TERMINAL) is True

    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=_metadata(),
        ),
        patch.object(monitor, "_publish_observation"),
        patch.object(monitor, "_announce_published"),
    ):
        monitor.disable_projection()  # the terminal falls back to the pane
        # processing -> idle: a rise out of PROCESSING, which the sticky-ready
        # latch does not refuse, so the pane path really does write the latch.
        monitor._apply_detection(TERMINAL, TerminalStatus.IDLE)

    assert monitor.status_written_by_projection(TERMINAL) is False
