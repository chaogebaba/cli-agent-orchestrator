"""D1e's gate: which terminals the projection publishes for (WP-ARCH phase 2).

Every suppression the status cutover performs asks ``is_projected`` and nothing
else, so the whole of I7 — "the pane stays a first-class fallback for every
unsourced terminal" — is carried by this one predicate.  The tests are written
against the ways it can fail OPEN, because failing open is the only direction
that hurts: a terminal wrongly marked projected has its pane path suppressed and
nothing publishing in its place, while a terminal wrongly marked unprojected
simply keeps today's behaviour.
"""

from __future__ import annotations

from test.app.conftest import Rig

from cli_agent_orchestrator.app.worker_truth.health import NullSourceHealth, SourceHealth
from cli_agent_orchestrator.core.events import EventKind
from cli_agent_orchestrator.core.ports import SourceHealthView
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S, PANE_HEARTBEAT_S

TERMINAL = "term-h1"
OTHER = "term-h2"


def _sourced_and_speaking(rig: Rig, terminal_id: str = TERMINAL) -> None:
    """A terminal with a registered tailer that has just stat-ed its file."""
    rig.sources.add(terminal_id)
    rig.states.touch_source_probe(terminal_id, probed_at=rig.clock.now())


# ------------------------------------------------------------ the default


def test_an_unknown_terminal_is_not_projected(rig: Rig) -> None:
    """Absence is the answer, not an error and not a guess.

    ``is_projected`` is consulted from the status monitor's locked publish path
    for terminals the projector may never have seen — every terminal, for the
    whole of every boot before the first event arrives.  The mutant is a view
    that defaults to ``True`` and relies on the writer to correct it.
    """
    assert rig.health.is_projected("never-heard-of-it") is False


def test_a_terminal_with_no_source_is_never_projected(rig: Rig) -> None:
    """I7's promise, at the only place it is decided.

    A pi or kiro lane has pane classification and no ``EventSource`` at all, so
    it must stay legacy by construction — not by being left off an allowlist.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    assert rig.health.is_projected(TERMINAL) is False


def test_a_registered_source_that_has_never_probed_is_not_projected(rig: Rig) -> None:
    """A tailer that failed to start is the moment the fallback matters most."""
    rig.sources.add(TERMINAL)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.health.is_projected(TERMINAL) is False


# --------------------------------------------------------------- the fold


def test_the_fold_marks_a_sourced_healthy_terminal_projected(rig: Rig) -> None:
    _sourced_and_speaking(rig)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.health.is_projected(TERMINAL) is True


def test_the_mark_is_per_terminal(rig: Rig) -> None:
    """The mutant is a single fleet-wide boolean, which would suppress the pane
    path for the unsourced lanes the moment one codex terminal got a source."""
    _sourced_and_speaking(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.emit(OTHER, EventKind.TURN_STARTED)

    assert rig.health.is_projected(TERMINAL) is True
    assert rig.health.is_projected(OTHER) is False


def test_the_mark_is_restated_on_every_fold_not_only_on_a_transition(rig: Rig) -> None:
    """A no-op fold still re-states the gate.

    The diagonal is the common case on a busy terminal — a hundred identical
    publishes fold to one transition row — so a mark written only on a real
    transition would be written almost never.
    """
    _sourced_and_speaking(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.health.reset()

    rig.emit(TERMINAL, EventKind.TOOL_CALLED)  # same state: NoOp

    assert rig.health.is_projected(TERMINAL) is True


def test_a_muted_derived_event_still_re_states_the_gate(rig: Rig) -> None:
    """The muted path is exactly where a projected terminal lives.

    Source-level precedence mutes the pane's derived rows for a terminal with a
    healthy source, and that is the same condition the gate reports — so a mark
    placed after the muting branch would never fire for the terminals it is about.
    """
    _sourced_and_speaking(rig)
    rig.health.reset()

    rig.legacy(TERMINAL, "processing")

    assert rig.health.is_projected(TERMINAL) is True


# -------------------------------------------------------------- the sweep


def test_the_sweep_unmarks_a_terminal_whose_source_went_quiet(rig: Rig) -> None:
    """The half of the gate the fold cannot write.

    Silence produces no event, so nothing folds and the standing ``True`` would
    never be lowered.  A terminal whose source died quietly would then have its
    pane path suppressed with nothing publishing in its place — the exact failure
    I7 exists to prevent, arriving as a status that never moves again.
    """
    _sourced_and_speaking(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    assert rig.health.is_projected(TERMINAL) is True

    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.projector.sweep()

    assert rig.health.is_projected(TERMINAL) is False
    assert rig.state_of(TERMINAL) is WorkerState.DEGRADED
    assert rig.states.get(TERMINAL).degraded_reason is DegradedReason.NO_SIGNAL


def test_the_sweep_re_states_the_gate_for_a_terminal_it_does_not_degrade(rig: Rig) -> None:
    """Level, not edge.  The sweep marks before its own guards skip the row."""
    _sourced_and_speaking(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.health.reset()

    rig.clock.advance(PANE_HEARTBEAT_S)
    rig.projector.sweep()

    assert rig.health.is_projected(TERMINAL) is True


def test_one_sweep_period_bounds_how_stale_a_mark_can_be() -> None:
    """The staleness bound is an ORDERING between two named constants.

    The sweep re-states every mark once per ``PANE_HEARTBEAT_S`` and the silence
    horizon is ``NO_SIGNAL_S``, so a mark can only be wrong for less than one
    sweep period — and that is a property of the numbers, not of the code, which
    is why it is asserted over the constants rather than by advancing a clock.
    """
    assert PANE_HEARTBEAT_S < NO_SIGNAL_S


def test_a_recovered_source_is_marked_projected_again(rig: Rig) -> None:
    """The gate has to be re-openable, or a single blip is permanent."""
    _sourced_and_speaking(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.projector.sweep()
    assert rig.health.is_projected(TERMINAL) is False

    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.projector.sweep()

    assert rig.health.is_projected(TERMINAL) is True


# ------------------------------------------------- the view in isolation


def test_the_view_satisfies_the_port() -> None:
    assert isinstance(SourceHealth(), SourceHealthView)
    assert isinstance(NullSourceHealth(), SourceHealthView)


def test_a_stopped_projector_projects_nothing() -> None:
    """What every consumer sees when the projector is not running.

    The composition root drops the view with the runtime, so the marks a stopped
    projector left behind are unreachable and the whole fleet falls back to the
    pane.  ``NullSourceHealth`` is that state made explicit for a projector built
    without a view at all.
    """
    view = NullSourceHealth()
    view.mark(TERMINAL, projected=True)

    assert view.is_projected(TERMINAL) is False


def test_forget_returns_a_terminal_to_the_default(rig: Rig) -> None:
    _sourced_and_speaking(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    rig.health.forget(TERMINAL)

    assert rig.health.is_projected(TERMINAL) is False


def test_the_admission_predicate_can_only_narrow() -> None:
    """Phase 2's provider allowlist hangs here, and it must not be able to widen.

    Composed inside the view rather than at the suppression sites (§5), so the
    herdr seam's amendment to I7 is an edit to one predicate.  Consulted ONLY for
    a terminal the projector marked projected, so a broken predicate can lose the
    cutover but can never suppress the pane path for an unsourced lane.
    """
    view = SourceHealth(admits=lambda terminal_id: terminal_id == TERMINAL)
    view.mark(TERMINAL, projected=True)
    view.mark(OTHER, projected=False)

    assert view.is_projected(TERMINAL) is True
    assert view.is_projected(OTHER) is False


def test_an_admission_predicate_that_raises_falls_back_to_the_pane() -> None:
    """The read runs on the monitor's locked publish path.

    An exception there would turn a suppression decision into a status outage,
    and the honest answer when the gate cannot be evaluated is "not projected".
    """

    def explode(terminal_id: str) -> bool:
        raise RuntimeError("allowlist unavailable")

    view = SourceHealth(admits=explode)
    view.mark(TERMINAL, projected=True)

    assert view.is_projected(TERMINAL) is False


# --------------------------------------------------- bounded reads (S5)


def test_the_sweep_never_reads_a_terminals_whole_history(rig: Rig) -> None:
    """The sweep runs 4320 times a day; its reads must not grow with uptime.

    An unbounded read here materialises every row a terminal has ever stored —
    thirty days of retention — to answer a question about the newest one, once
    per silent terminal, per tick.
    """
    windows: list[int | None] = []
    inner = rig.events.read

    def recording_read(terminal_id=None, **kwargs):
        windows.append(kwargs.get("since_seq"))
        return inner(terminal_id, **kwargs)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    for index in range(600):
        rig.legacy(TERMINAL, "processing" if index % 2 else "idle")
    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.events.read = recording_read  # type: ignore[method-assign]

    rig.projector.sweep()

    assert windows, "the sweep read nothing at all"
    # Every read names a window, and over a long history every window has moved
    # off zero — which is the difference between "bounded" and "the whole log".
    assert all(window is not None for window in windows)
    assert min(windows) > 0  # type: ignore[type-var]


def test_a_pruned_evidence_row_skips_the_terminal_rather_than_citing_nothing(
    rig: Rig,
) -> None:
    """Retention can take the row the sweep would cite.

    A degradation with no surviving evidence is exactly what
    ``DIAG-GHOST-TRANSITION`` exists to complain about, so the sweep stays quiet
    instead of writing one.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.events.prune(rig.clock.now())  # retention takes the evidence

    outcomes = rig.projector.sweep()

    assert outcomes == []
    assert rig.state_of(TERMINAL) is WorkerState.BUSY
