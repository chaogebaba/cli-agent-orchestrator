"""The two periodic drivers, wired (WP-ARCH phase 2, sub-phase 2b).

Phase 1 wrote ``Projector.sweep`` and the liveness probe and started neither.
Unwired, both were dead code with green tests: the sweep is the ONLY producer of
``degraded(no_signal)`` and the only thing that can lower a standing "this
terminal is projected" mark, and the probe is ``process.exited``'s sole owner and
— after 3c deletes the stalled-callback watchdog — the pane-delta sampler's only
driver.  These tests are about the wiring, which is the part that was missing.

Two decisions here are load-bearing enough to have their own tests:

* a backend that cannot enumerate windows gets NO pane listing rather than a
  failing one, because ``PROBE_FAIL_TICKS`` failures degrade the whole fleet and
  a missing capability is not an outage;
* the sampler re-drive defers to any sample taken in the last staleness window,
  so while the watchdog still runs this tick captures nothing at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.truth import liveness_probe as probe_module
from cli_agent_orchestrator.adapters.truth.liveness_probe import LivenessProbe, PaneRecord
from cli_agent_orchestrator.app.worker_truth import sweep as sweep_module
from cli_agent_orchestrator.app.worker_truth.sweep import ProjectorSweep

from .conftest import FakeClock


@pytest_asyncio.fixture(autouse=True)
async def _clean_runtime() -> AsyncIterator[None]:
    yield
    await bootstrap.shutdown_worker_truth()


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


@pytest.mark.asyncio
async def test_the_probe_is_started_even_with_no_pane_listing(
    db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Because the probe also owns the sampler re-drive (§12).

    Gating the whole probe on the backend's pane-listing capability would hand
    the pane-delta rules' only surviving driver to a tick that never fires, the
    moment 3c deletes the watchdog — the exact silent regression the re-drive
    exists to prevent.
    """
    monkeypatch.setattr(bootstrap, "_build_pane_lister", lambda: None)

    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_INGEST": "1"}
    )

    assert runtime.probe is not None


# ---------------------------------------------------------- the pane lister


class _Backend:
    """A backend that overrides ``enumerate_windows``, as tmux does."""

    def __init__(self, windows: dict[str, tuple[str, list[dict[str, object]] | None]]) -> None:
        self._windows = windows

    def enumerate_windows(self, session_name: str) -> tuple[str, list[dict[str, object]] | None]:
        return self._windows.get(session_name, ("ok", []))


def _roster(monkeypatch: pytest.MonkeyPatch, *members: tuple[str, str, str]) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_fleet_roster",
        lambda: [bootstrap._FleetMember(*member) for member in members],
    )


def test_a_backend_without_the_capability_yields_no_lister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """herdr inherits ``base.py``'s fail-closed default, the F893/F900 family.

    Handing the probe a lister that always fails would open a fleet-wide
    ``degraded(producer_error)`` episode within ``PROBE_FAIL_TICKS`` and keep it
    open forever, on a fleet that is perfectly healthy.
    """
    from cli_agent_orchestrator.backends import registry
    from cli_agent_orchestrator.backends.base import TerminalBackend

    class _NoCapability:
        enumerate_windows = TerminalBackend.enumerate_windows

    monkeypatch.setattr(registry, "get_backend", lambda: _NoCapability())

    assert bootstrap._build_pane_lister() is None


def test_the_lister_reads_one_listing_per_fleet_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.backends import registry

    backend = _Backend({"s1": ("ok", [{"name": "w1"}, {"name": "w2"}])})
    monkeypatch.setattr(registry, "get_backend", lambda: backend)
    _roster(monkeypatch, ("t1", "s1", "w1"), ("t2", "s1", "w2"))

    lister = bootstrap._build_pane_lister()

    assert lister is not None
    assert set(lister()) == {PaneRecord("s1", "w1"), PaneRecord("s1", "w2")}


def test_an_unreadable_session_fails_the_whole_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """B13 at the composition root.

    A session the backend could not read says something about the READ, not about
    the workers in it — and ``process.exited`` is a one-way door in the
    projection, so a partial listing must never be presented as a complete one.
    """
    from cli_agent_orchestrator.backends import registry

    backend = _Backend(
        {"s1": ("ok", [{"name": "w1"}]), "s2": ("error", None)},
    )
    monkeypatch.setattr(registry, "get_backend", lambda: backend)
    _roster(monkeypatch, ("t1", "s1", "w1"), ("t2", "s2", "w2"))

    lister = bootstrap._build_pane_lister()

    assert lister is not None
    assert lister() == []


# --------------------------------------------------------- the sampler tick


class _Sampler:
    def __init__(self, fresh: bool) -> None:
        self._fresh = fresh
        self.observed: list[str] = []

    def peek(self, terminal_id: str, *, now: float | None = None) -> object | None:
        return object() if self._fresh else None

    def observe(self, terminal_id: str, *, now: float | None = None, monitor: object = None):
        self.observed.append(terminal_id)
        return None


def test_a_fresh_sample_is_not_re_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hand-off, and the reason it needs no flag.

    While the stalled-callback watchdog is alive it samples every 1-5 s, so this
    tick captures nothing and status fusion is byte-identical.  Without the
    guard, both would sample and the extra call would advance the sampler's
    ``unchanged_count`` on a cadence rule 3a reads — a behaviour change delivered
    by a re-drive whose whole purpose is to avoid one.
    """
    from cli_agent_orchestrator.services import pane_liveness as pane_liveness_module

    sampler = _Sampler(fresh=True)
    monkeypatch.setattr(pane_liveness_module, "pane_liveness", sampler)
    _roster(monkeypatch, ("t1", "s1", "w1"))

    bootstrap._build_sampler_tick()()

    assert sampler.observed == []


def test_a_stale_sample_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """And after 3c deletes the watchdog, every sample is stale."""
    from cli_agent_orchestrator.services import pane_liveness as pane_liveness_module

    sampler = _Sampler(fresh=False)
    monkeypatch.setattr(pane_liveness_module, "pane_liveness", sampler)
    _roster(monkeypatch, ("t1", "s1", "w1"), ("t2", "s1", "w2"))

    bootstrap._build_sampler_tick()()

    assert sampler.observed == ["t1", "t2"]


def test_one_terminal_that_explodes_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Hostile(_Sampler):
        def observe(self, terminal_id: str, *, now: float | None = None, monitor: object = None):
            if terminal_id == "t1":
                raise RuntimeError("pane unreadable")
            return super().observe(terminal_id, now=now, monitor=monitor)

    from cli_agent_orchestrator.services import pane_liveness as pane_liveness_module

    sampler = _Hostile(fresh=False)
    monkeypatch.setattr(pane_liveness_module, "pane_liveness", sampler)
    _roster(monkeypatch, ("t1", "s1", "w1"), ("t2", "s1", "w2"))

    bootstrap._build_sampler_tick()()

    assert sampler.observed == ["t2"]


# ------------------------------------------------------------- the cadence


class _CountingSweeper:
    def __init__(self) -> None:
        self.calls = 0
        self.ran = asyncio.Event()

    def sweep(self) -> None:
        self.calls += 1
        self.ran.set()


@pytest.mark.asyncio
async def test_the_sweep_task_actually_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_module, "PANE_HEARTBEAT_S", 0)
    projector = _CountingSweeper()
    task = ProjectorSweep(projector)

    await task.start()
    await asyncio.wait_for(projector.ran.wait(), timeout=5)
    await task.stop()

    assert projector.calls >= 1
    assert task.running is False


@pytest.mark.asyncio
async def test_a_sweep_that_raises_does_not_end_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A maintenance task that can kill itself stops silently, and the first
    symptom is a fleet that never degrades again."""
    monkeypatch.setattr(sweep_module, "PANE_HEARTBEAT_S", 0)
    calls: list[int] = []
    done = asyncio.Event()

    class _Hostile:
        def sweep(self) -> None:
            calls.append(1)
            if len(calls) >= 3:
                done.set()
            raise RuntimeError("projection unreadable")

    task = ProjectorSweep(_Hostile())
    await task.start()
    await asyncio.wait_for(done.wait(), timeout=5)
    await task.stop()

    assert len(calls) >= 3


@pytest.mark.asyncio
async def test_the_probe_task_actually_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """And it ticks OFF the event loop: one tick shells out to the backend."""
    monkeypatch.setattr(probe_module, "PANE_HEARTBEAT_S", 0)
    ticked = asyncio.Event()
    threads: set[int] = set()

    def sampler_tick() -> None:
        import threading

        threads.add(threading.get_ident())
        ticked.set()

    from cli_agent_orchestrator.adapters.truth import wiring

    from .truth.conftest import FakeClock as ProducerClock
    from .truth.conftest import FakeEventStore

    wiring.install_producers(wiring.ProducerRuntime(store=FakeEventStore(), clock=ProducerClock()))
    try:
        probe = LivenessProbe(fleet=lambda: [], sampler_tick=sampler_tick)
        await probe.start()
        await asyncio.wait_for(ticked.wait(), timeout=5)
        await probe.stop()
    finally:
        wiring.reset_producers()

    import threading as _threading

    assert threads and _threading.get_ident() not in threads
