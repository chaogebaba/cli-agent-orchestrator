"""§12's sampler re-drive seam (WP-ARCH phase 2, sub-phase 2a).

This is the cross-phase defect phase 2 notices and neither phase owns.
``pane_liveness.observe`` is called from exactly one place, the stalled-callback
watchdog's tick, and that module is phase 3's D6 K4 — deleted in 3c.  After that,
``fuse_status``'s rules 3a/3b read a sample nothing refreshes, which by the
sampler's own no-evidence rule degrades to ``None`` and SILENTLY disables the
pane-delta downgrade for every unsourced terminal: the ones I7 promises are
unaffected, with no acceptance criterion in phase 3 to catch it.

The blueprint moves the drive onto the liveness probe's existing
``PANE_HEARTBEAT_S`` tick, which already owns the fleet's periodic tmux work.
The probe supplies the TICK, not the capture, so the F506 single-sampler ban
still holds.
"""

from __future__ import annotations

from collections.abc import Callable

from cli_agent_orchestrator.adapters.truth.liveness_probe import LivenessProbe, PaneRecord

from .conftest import FakeEventStore


def _probe(sampler_tick: Callable[..., None] | None = None) -> LivenessProbe:
    return LivenessProbe(
        list_panes=lambda _fleet: [PaneRecord(session="s", window="0", pid=1)],
        fleet=lambda: [],
        sampler_tick=sampler_tick,
    )


def test_the_tick_drives_the_sampler_once_per_probe(ingest_on: FakeEventStore) -> None:
    calls: list[int] = []
    probe = _probe(sampler_tick=lambda _fleet: calls.append(1))

    probe.probe_once()
    probe.probe_once()

    assert len(calls) == 2


def test_a_failed_probe_still_refreshes_the_sample(ingest_on: FakeEventStore) -> None:
    """The drive runs BEFORE the probe's own work, and that ordering is the point.

    A tmux listing that could not be read says nothing about whether an individual
    pane changed, so a probe that failed and returned early must not also skip the
    sampler — that would couple the pane-delta evidence to the health of a
    completely different tmux call.
    """
    calls: list[int] = []
    probe = LivenessProbe(
        list_panes=lambda _fleet: (_ for _ in ()).throw(RuntimeError("tmux is gone")),
        fleet=lambda: [],
        sampler_tick=lambda _fleet: calls.append(1),
    )

    probe.probe_once()

    assert calls == [1]


def test_a_sampler_that_raises_cannot_break_the_probe(ingest_on: FakeEventStore) -> None:
    """A re-drive that could take the liveness probe down would be a strictly worse
    trade than the regression it prevents: the probe is ``process.exited``'s sole
    owner, so its failure is silent and total."""

    def explode() -> None:
        raise RuntimeError("sampler exploded")

    probe = _probe(sampler_tick=explode)
    probe.probe_once()  # must not raise


def test_the_seam_is_optional(ingest_on: FakeEventStore) -> None:
    """No ``sampler_tick`` is the current production shape, and it must stay legal.

    The drive cannot be MOVED off the watchdog in 2a: the liveness probe is
    constructed nowhere in the tree at this anchor, so moving it would hand the
    only surviving driver to a tick that never fires — the exact silent regression
    §12 exists to prevent, arriving one phase early.
    """
    probe = _probe()
    probe.probe_once()


def test_with_ingestion_off_the_probe_does_not_even_tick_the_sampler(
    store: FakeEventStore,
) -> None:
    """AC-2a's off arm reaches this too.

    The install guard in the composition root is the ONLY path from a hook to the
    store, and a sampler tick that ran with ingestion off would be phase 2 doing
    work in the arm that is meant to be phase-1 behaviour exactly.
    """
    calls: list[int] = []
    _probe(sampler_tick=lambda _fleet: calls.append(1)).probe_once()

    assert calls == []
