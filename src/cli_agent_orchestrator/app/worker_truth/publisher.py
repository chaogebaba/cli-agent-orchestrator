"""The projection publishes the status (WP-ARCH phase 2, D1 — the cutover).

This is the half of D1 that adds a writer.  The other half — the pane path
ceasing to write for the same terminals — lives in the legacy monitor, and the
two are ONE decision: adding a producer without removing one buys a race, because
the slot every writer lands in is last-write-wins and the pane writer fires per
output chunk.

Three properties carry the design:

**It publishes through the SINGLE legacy egress, not into a second store.**  The
egress is where every origin already converges before a status reaches a
consumer; it stamps the lifecycle generation, the window identity and the
observation epoch from terminal metadata, which is the fencing a cross-
incarnation publish needs without reading a pane.  So every downstream consumer —
the fleet, the inbox, all 47 call sites of the audit's §11 — reads the projection
without learning a second truth.  The egress lives in the legacy tree, which
``app`` may not import, so it arrives as :class:`~core.ports.StatusEgress` and
the composition root fills it.

**It is gated on the SAME predicate the suppression is.**  ``is_projected`` is
the one question (D1e): a terminal whose source is registered and healthy and
whose provider the operator allowlisted.  Publishing on a different predicate
than the one that suppresses the pane would produce the two failure modes the
phase exists to end — two writers, or none.

The sweep's ``degraded(no_signal)`` is the case that makes this concrete, and it
answers DIFFERENTLY for the two cohorts.  For an ordinary terminal AC-2b case 7
still holds: the source is gone, ``is_projected`` is already ``False``, and the
right answer is to publish nothing and let the pane resume.

For a CERTIFIED terminal whose source has ever delivered, WP-HERDR §6(ii)
overrides case 7 and ``_projected`` short-circuits to ``True`` on a stale source
(``app/worker_truth/projector.py``).  So this publisher keeps publishing, the
status becomes ``unknown``, and the pane is NOT handed the lifecycle back.  That
is deliberate, and the reason is an asymmetry rather than a preference: under
§6(ii) the terminal reads ``unknown``, inbox admission withholds, the row is
already time-bounded by ``DELIVERY_VETO_CEILING_S``, and the queued message
survives the source coming back.  Under case 7 the scraper's guess silently
replaces a known-unknown, and a false ``idle`` pastes into a mid-turn worker —
unbounded, invisible, and not undoable (#361, #439, F582 are its history).
Withholding is observable and reversible; a corrupted turn is neither.

The standing ``unknown`` this creates is bounded by a finding rather than by a
timeout: ``DIAG-CERTIFIED-SOURCE-STALE``, deduped per terminal, is written where
the sweep degrades, so a certified source that has been quiet for hours is
visible instead of merely silent.

**Every publish names the event that caused it.**  I2 in one field: the
``status.transition`` row's ``event_id`` rides on the observation, and
``cao diag --why`` walks it back to the worker's own record.  A status a
consumer cannot trace is what this whole work package exists to end.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from cli_agent_orchestrator.app.worker_truth.mapping import legacy_status
from cli_agent_orchestrator.core.events import AnyKind
from cli_agent_orchestrator.core.ports import SourceHealthView, StatusEgress
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState

logger = logging.getLogger(__name__)

__all__ = ["PublishTransition", "StatusPublisher"]


class PublishTransition(Protocol):
    """What the projector calls when a fold moves a terminal's state.

    A Protocol rather than the concrete class, for the reason every other seam on
    the projector is one: it must stay runnable — and testable — without the
    cutover wired, and a projector that named its publisher could not be.
    """

    def __call__(
        self,
        terminal_id: str,
        state: WorkerState,
        *,
        causing_kind: AnyKind | None,
        degraded_reason: DegradedReason | None,
        event_id: str | None,
        since: datetime,
    ) -> bool: ...


class StatusPublisher:
    """Turns an applied fold into one publish through the legacy egress."""

    def __init__(self, egress: StatusEgress, view: SourceHealthView) -> None:
        self._egress = egress
        self._view = view

    def __call__(
        self,
        terminal_id: str,
        state: WorkerState,
        *,
        causing_kind: AnyKind | None,
        degraded_reason: DegradedReason | None,
        event_id: str | None,
        since: datetime,
    ) -> bool:
        """Publish one transition.  Returns whether it was published.

        Never raises.  The projector calls this from inside its critical
        section, on the path a hook reaches from the status monitor's own locked
        publish: an exception here would turn the diagnosability feature into the
        status outage it was built to prevent.
        """
        try:
            if not self._view.is_projected(terminal_id):
                # Not ours to publish.  Either the terminal never was projected,
                # or — the interesting case — its source has just died and this
                # very fold is the ``degraded(no_signal)`` that says so.  The
                # pane path is publishing for it again by the time we are asked,
                # and a publish here would be the second writer.
                return False
            self._egress.publish(
                terminal_id,
                legacy_status(state, causing_kind=causing_kind, degraded_reason=degraded_reason),
                event_id=event_id,
                worker_state=state.value,
                since=since,
            )
            return True
        except Exception:  # noqa: BLE001 — the guarantee, not a branch under test
            logger.warning(
                "worker-truth status publish failed for %s; the pane path still holds the "
                "terminal's status",
                terminal_id,
                exc_info=True,
            )
            return False
