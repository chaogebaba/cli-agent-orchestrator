"""Which terminals the projection publishes for (WP-ARCH phase 2, D1e / I7).

The status cutover needs one question answered from two very different places:
the projector, which knows whether a terminal's source is registered and still
speaking, and the legacy status monitor, which has to decide — on the locked
publish path, thousands of times an hour — whether to get out of the way.  The
monitor cannot ask the projector: it would mean a legacy service reaching into
``app`` and taking whatever lock the projection is under, from inside the very
path a projection fold can reach.

So the answer is WRITTEN by the projector as it folds and swept, and READ through
:class:`~core.ports.SourceHealthView` as a plain dictionary lookup.  The read is
a dict ``get`` under a lock held for the length of one comparison; nothing else
happens on the read side, and that is the property that makes it safe to call
from the publish path.

Three properties are load-bearing, and each is a way to get this wrong:

* **Absence is NOT projected.**  Every unknown terminal, every terminal before
  its first fold, and the whole fleet when nothing ever marked anything, all
  answer ``False``.  The alternative — defaulting to projected and letting the
  writer correct it — inverts the failure: a boot where the projector never
  started would suppress the pane path for the entire fleet and publish nothing
  in its place.
* **The mark is LEVEL, not edge.**  The projector re-marks every terminal on
  every sweep rather than only the ones that changed, so a stale ``True`` cannot
  outlive one ``PANE_HEARTBEAT_S``.  A map that only recorded transitions would
  hold a terminal projected forever after its source died quietly, which is the
  exact half of I7 the sweep exists to close.
* **The admission predicate is composed HERE, not at the call sites.**  Source
  health is one input; phase 2's provider allowlist is another, and the herdr
  seam's §6 amendment to I7 will be a third.  They meet in ``is_projected`` so
  that a suppression site never grows a second opinion about when to fall back.
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol

__all__ = [
    "NullSourceHealth",
    "SourceHealth",
    "SourceHealthWriter",
]


class SourceHealthWriter(Protocol):
    """The write side, which only the projector holds.

    Separate from :class:`~core.ports.SourceHealthView` on purpose: the view is
    what crosses into the legacy monitor, and it must not carry a way to declare
    a terminal projected.  A consumer of the status path that could mark its own
    terminal would close the causal loop ``fed_by`` exists to keep open.
    """

    def mark(self, terminal_id: str, *, projected: bool) -> None: ...

    def forget(self, terminal_id: str) -> None: ...


class SourceHealth:
    """The in-memory map, written by the projector and read by the monitor.

    ``admits`` is the composition seam described in the module docstring.  It is
    consulted ONLY for a terminal the projector has marked projected, so it can
    never turn the fallback OFF — narrowing is always safe, widening never is.
    Phase 2's rollout fills it with the provider allowlist; leaving it unset
    means "source health alone decides", which is what a test and the sub-phase
    that has no allowlist yet both want.
    """

    def __init__(self, admits: Callable[[str], bool] | None = None) -> None:
        self._lock = threading.Lock()
        self._projected: dict[str, bool] = {}
        self._admits = admits

    # -- the write side ------------------------------------------------------

    def mark(self, terminal_id: str, *, projected: bool) -> None:
        """Record whether the projection currently owns this terminal's status."""
        with self._lock:
            self._projected[terminal_id] = projected

    def forget(self, terminal_id: str) -> None:
        """Drop a terminal, returning it to the default answer of ``False``."""
        with self._lock:
            self._projected.pop(terminal_id, None)

    def reset(self) -> None:
        """Drop every mark.  For a bootstrap that re-installs, and for tests."""
        with self._lock:
            self._projected.clear()

    # -- the read side (core.ports.SourceHealthView) -------------------------

    def is_projected(self, terminal_id: str) -> bool:
        """True only when the projection is this terminal's publisher of record.

        Never raises, whatever ``admits`` does.  This is called from the status
        monitor's locked publish path, where an exception would turn a
        suppression decision into a status outage — and the honest answer when
        the admission predicate is broken is "fall back to the pane", which is
        what a failure produces here.
        """
        with self._lock:
            projected = self._projected.get(terminal_id, False)
        if not projected:
            return False
        if self._admits is None:
            return True
        try:
            return bool(self._admits(terminal_id))
        except Exception:
            return False


class NullSourceHealth:
    """Nothing is projected, and nothing is recorded.

    The correct default for a projector built without a view, and the shape the
    whole fleet has while the projector is stopped: the marks a running projector
    left behind are not consulted, because the composition root hands the monitor
    the view it built beside the projector and drops both together.
    """

    def mark(self, terminal_id: str, *, projected: bool) -> None:
        return None

    def forget(self, terminal_id: str) -> None:
        return None

    def is_projected(self, terminal_id: str) -> bool:
        return False
