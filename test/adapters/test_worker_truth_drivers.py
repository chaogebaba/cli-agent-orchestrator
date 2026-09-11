"""The two periodic drivers, wired (WP-ARCH phase 2, sub-phase 2b).

Phase 1 wrote ``Projector.sweep`` and the liveness probe and started neither.
Unwired, both were dead code with green tests: the sweep is the ONLY producer of
``degraded(no_signal)`` and the only thing that can lower a standing "this
terminal is projected" mark, and the probe is ``process.exited``'s sole owner and
— after 3c deletes the stalled-callback watchdog — the pane sample's only driver.
These tests are about the wiring and the cadence, which is the part that was
missing.

**No test here sleeps, and none sets a period to zero.**  Both tasks take their
sleep as an injected callable, so a test asserts the SEQUENCE of durations the
loop asks for and decides when the loop ends.  A period of zero would have made
the two properties that matter untestable: that the sweep sleeps BEFORE its first
pass (a crash-looping server must not become a delete loop against the live
database) and that the probe interleaves one pane listing per
``PANE_HEARTBEAT_S`` with a sample every ``PANE_SAMPLE_S``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.truth.liveness_probe import LivenessProbe, PaneRecord
from cli_agent_orchestrator.app.worker_truth.sweep import ProjectorSweep
from cli_agent_orchestrator.core.timing import (
    PANE_HEARTBEAT_S,
    PANE_LIVENESS_STALENESS_S,
    PANE_SAMPLE_S,
)

from .conftest import FakeClock


@pytest_asyncio.fixture(autouse=True)
async def _clean_runtime() -> AsyncIterator[None]:
    yield
    await bootstrap.shutdown_worker_truth()


class _Clockwork:
    """A sleeper that records what it was asked for and ends the loop after N.

    Returning ``True`` is how the real sleeper says "stop", so a test ends the
    task deterministically at a tick boundary rather than by cancelling it
    mid-flight.
    """

    def __init__(self, ticks: int) -> None:
        self._ticks = ticks
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> bool:
        self.sleeps.append(seconds)
        return len(self.sleeps) >= self._ticks


# --------------------------------------------------------------- the wiring


@pytest.mark.asyncio
async def test_the_switch_on_starts_both_drivers(db_path: Path, clock: FakeClock) -> None:
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_INGEST": "1"}
    )

    assert runtime.sweep is not None and runtime.sweep.running is True
    assert runtime.probe is not None

    await bootstrap.shutdown_worker_truth()
    assert runtime.sweep.running is False


@pytest.mark.asyncio
async def test_the_switch_off_starts_neither(db_path: Path, clock: FakeClock) -> None:
    """AC5 again: with ingestion off nothing exists to contend for the writer."""
    runtime = await bootstrap.start_worker_truth(db_path=db_path, clock=clock, env={})

    assert runtime.probe is None
    assert runtime.sweep is None


# ---------------------------------------------------------- the pane lister


class _Backend:
    """A backend that overrides ``enumerate_windows``, as tmux does."""

    def __init__(self, windows: dict[str, tuple[str, list[dict[str, object]] | None]]) -> None:
        self._windows = windows
        self.calls: list[str] = []

    def enumerate_windows(self, session_name: str) -> tuple[str, list[dict[str, object]] | None]:
        self.calls.append(session_name)
        return self._windows.get(session_name, ("ok", []))


def _fleet(*members: tuple[str, str, str]) -> list[bootstrap._FleetMember]:
    return [bootstrap._FleetMember(*member) for member in members]


def test_a_backend_without_the_capability_answers_none_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` and ``[]`` are different answers, and the difference is the fleet.

    herdr inherits ``base.py``'s fail-closed ``enumerate_windows``, the F893/F900
    family.  ``[]`` is a FAILED probe, and ``PROBE_FAIL_TICKS`` of those open a
    fleet-wide ``degraded(producer_error)`` episode — permanently, on a fleet that
    is perfectly healthy, on the strength of a feature nobody implemented.
    """
    from cli_agent_orchestrator.backends import registry
    from cli_agent_orchestrator.backends.base import TerminalBackend

    class _NoCapability:
        enumerate_windows = TerminalBackend.enumerate_windows

    monkeypatch.setattr(registry, "get_backend", lambda: _NoCapability())

    assert bootstrap._build_pane_lister()(_fleet(("t1", "s1", "w1"))) is None


def test_the_lister_reads_one_listing_per_fleet_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.backends import registry

    backend = _Backend({"s1": ("ok", [{"name": "w1"}, {"name": "w2"}])})
    monkeypatch.setattr(registry, "get_backend", lambda: backend)

    panes = bootstrap._build_pane_lister()(_fleet(("t1", "s1", "w1"), ("t2", "s1", "w2")))

    assert set(panes or []) == {PaneRecord("s1", "w1"), PaneRecord("s1", "w2")}
    assert backend.calls == ["s1"]


def test_an_unreadable_session_fails_the_whole_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """B13 at the composition root.

    A session the backend could not read says something about the READ, not about
    the workers in it — and ``process.exited`` is a one-way door in the
    projection, so a partial listing must never be presented as a complete one.
    """
    from cli_agent_orchestrator.backends import registry

    backend = _Backend({"s1": ("ok", [{"name": "w1"}]), "s2": ("error", None)})
    monkeypatch.setattr(registry, "get_backend", lambda: backend)

    assert bootstrap._build_pane_lister()(_fleet(("t1", "s1", "w1"), ("t2", "s2", "w2"))) == []


def test_a_transient_backend_failure_does_not_disable_the_listing_for_ever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capability is decided per TICK, not once at boot.

    Deciding it once meant a factory hiccup during the lifespan disabled the
    fleet listing for the whole life of the server process, with one debug line
    to show for it.  The next tick must simply work.
    """
    from cli_agent_orchestrator.backends import registry

    backend = _Backend({"s1": ("ok", [{"name": "w1"}])})
    calls: list[int] = []

    def flaky() -> object:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("backend factory unavailable")
        return backend

    monkeypatch.setattr(registry, "get_backend", flaky)
    lister = bootstrap._build_pane_lister()

    assert lister(_fleet(("t1", "s1", "w1"))) is None
    assert lister(_fleet(("t1", "s1", "w1"))) == [PaneRecord("s1", "w1")]


def test_the_missing_capability_is_logged_once_at_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ "The probe is doing half its job" is not a debug-level fact — but it is
    also not worth a line every twenty seconds for the life of the server."""
    from cli_agent_orchestrator.backends import registry
    from cli_agent_orchestrator.backends.base import TerminalBackend

    class _NoCapability:
        enumerate_windows = TerminalBackend.enumerate_windows

    monkeypatch.setattr(registry, "get_backend", lambda: _NoCapability())
    lister = bootstrap._build_pane_lister()

    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.bootstrap"):
        for _ in range(5):
            lister(_fleet(("t1", "s1", "w1")))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "cannot enumerate windows" in warnings[0].getMessage()


# --------------------------------------------------------- the sampler tick


class _Sampler:
    """A ``pane_liveness`` double that counts CAPTURES, which is the whole point."""

    def __init__(self, fresh: bool) -> None:
        self._fresh = fresh
        self.observed: list[str] = []

    def peek(self, terminal_id: str, *, now: float | None = None) -> object | None:
        if self._fresh or terminal_id in self.observed:
            return _Retained()
        return None

    def observe(self, terminal_id: str, *, now: float | None = None, monitor: object = None):
        self.observed.append(terminal_id)
        return _Retained()


class _Retained:
    filtered_tail = "tail"


class _Monitor:
    def __init__(self) -> None:
        self.resyncs: list[str] = []

    def resync_from_pane_tail(self, terminal_id: str, tail: str, *, now: float | None = None):
        self.resyncs.append(terminal_id)


def _install_sampler(
    monkeypatch: pytest.MonkeyPatch, sampler: _Sampler, monitor: _Monitor | None = None
) -> _Monitor:
    from cli_agent_orchestrator.services import pane_liveness as pane_liveness_module
    from cli_agent_orchestrator.services import status_monitor as status_monitor_module

    resolved = monitor if monitor is not None else _Monitor()
    monkeypatch.setattr(pane_liveness_module, "pane_liveness", sampler)
    monkeypatch.setattr(status_monitor_module, "status_monitor", resolved)
    monkeypatch.setattr(bootstrap, "_reconcile_question_marker", lambda terminal_id: None)
    return resolved


def test_a_fresh_sample_is_not_re_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hand-off, counted.

    While the stalled-callback watchdog is alive it samples every 1-5 s, so
    ``peek`` always answers fresh and this tick takes ZERO captures — today's
    capture count per pane, exactly.  Without the guard both drivers would
    capture, and the extra call would advance the sampler's ``unchanged_count``
    on a cadence rule 3a reads: a behaviour change delivered by a re-drive whose
    whole purpose is to avoid one.
    """
    sampler = _Sampler(fresh=True)
    _install_sampler(monkeypatch, sampler)
    tick = bootstrap._build_sampler_tick()

    for _ in range(4):
        tick(_fleet(("t1", "s1", "w1"), ("t2", "s1", "w2")))

    assert sampler.observed == []


def test_a_stale_sample_is_taken_once_per_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """And after 3c deletes the watchdog, every sample is stale."""
    sampler = _Sampler(fresh=False)
    _install_sampler(monkeypatch, sampler)

    bootstrap._build_sampler_tick()(_fleet(("t1", "s1", "w1"), ("t2", "s1", "w2")))

    assert sampler.observed == ["t1", "t2"]


def test_the_tick_drives_all_three_consumers_of_one_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3: the watchdog tick fed three consumers, not one.

    ``resync_from_pane_tail`` (F521 D15) and the F507 question-marker reconcile
    ride the same sample as ``observe``.  A re-drive that carried only the first
    would take the other two dark the moment 3c deletes the watchdog, with no
    finding anywhere to say so.
    """
    sampler = _Sampler(fresh=False)
    monitor = _install_sampler(monkeypatch, sampler)
    reconciled: list[str] = []
    monkeypatch.setattr(bootstrap, "_reconcile_question_marker", reconciled.append)

    bootstrap._build_sampler_tick()(_fleet(("t1", "s1", "w1")))

    assert sampler.observed == ["t1"]
    assert monitor.resyncs == ["t1"]
    assert reconciled == ["t1"]


def test_a_tick_that_takes_no_sample_drives_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4: the guard gates all three consumers, not only the capture.

    A fresh sample means another driver took it and is driving its riders.  A
    tick that re-drove them anyway would double-call two consumers the watchdog
    is already calling at 1-5 s — and ``resync_from_pane_tail`` CONSUMES the
    drop-seq edge, so the forced re-derive would fire from whichever caller
    arrived first.  Self-guarded and safe, but no longer one pass per sample,
    and no longer today's behaviour.
    """
    sampler = _Sampler(fresh=True)
    monitor = _install_sampler(monkeypatch, sampler)
    reconciled: list[str] = []
    monkeypatch.setattr(bootstrap, "_reconcile_question_marker", reconciled.append)

    for _ in range(4):
        bootstrap._build_sampler_tick()(_fleet(("t1", "s1", "w1")))

    assert sampler.observed == []
    assert monitor.resyncs == []
    assert reconciled == []


def test_an_unusable_sample_drives_nothing_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable pane or a capture outage leaves nothing to re-derive from,
    which is exactly how the watchdog's own loop reads it."""

    class _NoSample(_Sampler):
        def observe(self, terminal_id: str, *, now: float | None = None, monitor: object = None):
            return None

    sampler = _NoSample(fresh=False)
    monitor = _install_sampler(monkeypatch, sampler)
    reconciled: list[str] = []
    monkeypatch.setattr(bootstrap, "_reconcile_question_marker", reconciled.append)

    bootstrap._build_sampler_tick()(_fleet(("t1", "s1", "w1")))

    assert monitor.resyncs == []
    assert reconciled == []


def test_one_terminal_that_explodes_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Hostile(_Sampler):
        def observe(self, terminal_id: str, *, now: float | None = None, monitor: object = None):
            if terminal_id == "t1":
                raise RuntimeError("pane unreadable")
            return super().observe(terminal_id, now=now, monitor=monitor)

    sampler = _Hostile(fresh=False)
    _install_sampler(monkeypatch, sampler)

    bootstrap._build_sampler_tick()(_fleet(("t1", "s1", "w1"), ("t2", "s1", "w2")))

    assert sampler.observed == ["t2"]


def test_a_missing_reconcile_method_is_not_an_error() -> None:
    """3c demotes the module that owns F507's reconcile.

    A consumer that is not driven is a degradation the next reader can see; an
    ImportError at boot is an outage.
    """
    bootstrap._reconcile_question_marker("nobody")  # must not raise


# ------------------------------------------------------------- the cadence


@dataclass(frozen=True)
class _Ref:
    terminal_id: str
    tmux_session: str
    tmux_window: str


class _CountingSweeper:
    def __init__(self) -> None:
        self.calls = 0

    def sweep(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_the_sweep_sleeps_before_its_first_pass() -> None:
    """The crash-loop guard, pinned.

    A server that restarts in a loop must not turn into a sweep loop against the
    live database, and a sweep at the instant of boot would judge every terminal
    silent before its first probe had a chance to land.  With three sleeps
    granted there are exactly two passes, which is only true if the sleep comes
    first.
    """
    projector = _CountingSweeper()
    sleeper = _Clockwork(ticks=3)
    task = ProjectorSweep(projector, sleeper=sleeper)

    await task.start()
    await task.stop()

    assert sleeper.sleeps == [PANE_HEARTBEAT_S] * 3
    assert projector.calls == 2
    assert task.running is False


@pytest.mark.asyncio
async def test_a_sweep_that_raises_does_not_end_the_sweep() -> None:
    """A maintenance task that can kill itself stops silently, and the first
    symptom is a fleet that never degrades again."""
    calls: list[int] = []

    class _Hostile:
        def sweep(self) -> None:
            calls.append(1)
            raise RuntimeError("projection unreadable")

    task = ProjectorSweep(_Hostile(), sleeper=_Clockwork(ticks=4))
    await task.start()
    await task.stop()

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_the_probe_interleaves_one_listing_with_three_samples() -> None:
    """The two cadences, on one task.

    The pane listing is a HEARTBEAT and the pane sample is a SAMPLE: the sampler
    calls its own evidence stale after ``PANE_LIVENESS_STALENESS_S``, so a sample
    running at the heartbeat would leave ``fuse_status``'s rules 3a/3b blind for
    half of every window — while a listing at the sample rate would quadruple the
    fleet's tmux work for nothing.
    """
    listings: list[int] = []
    samples: list[int] = []
    sleeper = _Clockwork(ticks=8)
    # A non-empty fleet, because an empty one short-circuits the listing before
    # it is attempted (an empty fleet is not a failed probe).
    ref = _Ref("t1", "s1", "w1")
    probe = LivenessProbe(
        list_panes=lambda fleet: (listings.append(1), [PaneRecord("s1", "w1", 1)])[1],
        fleet=lambda: [ref],
        sampler_tick=lambda fleet: samples.append(1),
        sleeper=sleeper,
    )

    from cli_agent_orchestrator.adapters.truth import wiring

    from .truth.conftest import FakeClock as ProducerClock
    from .truth.conftest import FakeEventStore

    wiring.install_producers(wiring.ProducerRuntime(store=FakeEventStore(), clock=ProducerClock()))
    try:
        await probe.start()
        await probe.stop()
    finally:
        wiring.reset_producers()

    assert sleeper.sleeps == [PANE_SAMPLE_S] * 8
    # Eight ticks at PANE_SAMPLE_S = two heartbeats: the listing is attempted on
    # ticks 0 and 4, and every tick drives the sample.
    assert len(samples) == 8
    assert len(listings) == 2
    assert PANE_SAMPLE_S * 2 <= PANE_LIVENESS_STALENESS_S < PANE_HEARTBEAT_S


@pytest.mark.asyncio
async def test_the_tick_runs_off_the_event_loop() -> None:
    """One tick shells out to the backend and then samples every pane.

    Run inline, a slow tmux or herdr call would stall every request the server is
    serving.
    """
    threads: set[int] = set()
    probe = LivenessProbe(
        fleet=lambda: [],
        sampler_tick=lambda fleet: threads.add(threading.get_ident()),
        sleeper=_Clockwork(ticks=1),
    )

    from cli_agent_orchestrator.adapters.truth import wiring

    from .truth.conftest import FakeClock as ProducerClock
    from .truth.conftest import FakeEventStore

    wiring.install_producers(wiring.ProducerRuntime(store=FakeEventStore(), clock=ProducerClock()))
    try:
        await probe.start()
        await probe.stop()
    finally:
        wiring.reset_producers()

    assert threads and threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_stop_waits_for_the_tick_that_is_in_flight() -> None:
    """``stop`` is not a cancel, and it must not be.

    A task parked on ``asyncio.to_thread`` raises ``CancelledError`` in the
    coroutine while the worker thread runs on to completion — so a cancelling
    ``stop`` would return with a real ``capture-pane`` still in progress against
    a server that believes it has shut down.
    """
    released = threading.Event()
    finished: list[int] = []

    def slow_tick(fleet: Sequence[object]) -> None:
        released.wait(timeout=5)
        finished.append(1)

    probe = LivenessProbe(fleet=lambda: [], sampler_tick=slow_tick, sleeper=_Clockwork(ticks=1))

    from cli_agent_orchestrator.adapters.truth import wiring

    from .truth.conftest import FakeClock as ProducerClock
    from .truth.conftest import FakeEventStore

    wiring.install_producers(wiring.ProducerRuntime(store=FakeEventStore(), clock=ProducerClock()))
    try:
        await probe.start()
        await asyncio.sleep(0)  # let the tick reach the executor
        released.set()
        await probe.stop()
    finally:
        wiring.reset_producers()

    assert finished == [1]


# ------------------------------------------- the provider allowlist (D9c)


def test_the_allowlist_admits_only_the_named_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """D9c: the allowlist is the operator's control, the registry is the fact."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: {"provider": "codex" if terminal_id == "t-codex" else "kiro"},
    )
    allowlist = bootstrap._ProviderAllowlist(frozenset({"codex"}))

    assert allowlist("t-codex") is True
    assert allowlist("t-kiro") is False


def test_an_unresolvable_provider_is_not_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicate narrows and never widens: it is consulted only for a
    terminal already marked projected, so failing closed leaves the pane in
    charge rather than suppressing it."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: None,
    )

    assert bootstrap._ProviderAllowlist(frozenset({"codex"}))("t-gone") is False


def test_the_provider_is_read_once_per_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1e forbids a database read on the getter path, and this predicate sits on
    it.  A terminal's provider is fixed for its lifetime, so one read is enough —
    and the teardown path drops the entry with the rest of its state, so a
    recycled id cannot inherit it."""
    reads: list[str] = []

    def counting(terminal_id: str) -> dict[str, str]:
        reads.append(terminal_id)
        return {"provider": "codex"}

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", counting)
    allowlist = bootstrap._ProviderAllowlist(frozenset({"codex"}))

    for _ in range(5):
        allowlist("t-codex")
    allowlist.forget("t-codex")
    allowlist("t-codex")

    assert reads == ["t-codex", "t-codex"]
