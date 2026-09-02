"""The invariant-check registry (WP-ARCH phase 1, AC8 — skeleton).

AC8's checks run ON APPEND, and this is the machinery that runs them.  The checks
themselves — ``DIAG-GHOST-TRANSITION``, ``DIAG-BAD-TRANSITION``,
``DIAG-LEGACY-DISAGREE`` — land with the projector; the registry ships first
because it is what the store is constructed with, and the store is built before
the projector exists.

Why the registry lives in ``app`` while the store that calls it lives in
``adapters``: ``adapters-are-leaves`` forbids an adapter from importing ``app``.
The store therefore depends on the ``core.ports.CheckRunner`` Protocol, and
``bootstrap.py`` injects this concrete registry.  The dependency arrow points the
right way and the contract enforces it.

**A check may never break an append.**  Every check runs inside its own
try/except here, and the store wraps the whole call again.  A diagnostic that can
take the event log down would be worse than no diagnostic at all — the log is
what every later phase reads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from cli_agent_orchestrator.core.events import WorkerEvent
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ports import FindingStore

logger = logging.getLogger(__name__)

__all__ = ["CheckOutcome", "CheckRegistry", "EventCheck"]


@dataclass(frozen=True)
class CheckOutcome:
    """What a check reports when its invariant is broken.

    ``dedupe_key`` is what makes two breaches "the same problem again" — the
    offending cell for a bad transition, the disagreeing pair for a legacy
    disagreement.  Only the check knows that, so only the check supplies it.
    """

    dedupe_key: str = ""
    detail: str = ""
    terminal_id: str | None = None
    sample_event_id: str | None = None


#: A check inspects one appended event and returns an outcome when its invariant
#: is broken, or ``None`` when it holds.
EventCheck = Callable[[WorkerEvent], CheckOutcome | None]


class CheckRegistry:
    """``core.ports.CheckRunner``: runs every registered check on each append."""

    def __init__(self, finding_store: FindingStore) -> None:
        self._finding_store = finding_store
        self._checks: list[tuple[FindingCode, EventCheck]] = []

    def register(self, code: FindingCode, check: EventCheck) -> None:
        """Register ``check`` to report findings under ``code``."""
        self._checks.append((code, check))

    @property
    def registered_codes(self) -> tuple[FindingCode, ...]:
        return tuple(code for code, _ in self._checks)

    def on_append(self, event: WorkerEvent) -> None:
        """Run every check against ``event``.  Never raises."""
        for code, check in self._checks:
            try:
                outcome = check(event)
            except Exception:  # noqa: BLE001 — one broken check must not stop the others
                logger.warning("worker-truth check %s raised; skipping", code.value, exc_info=True)
                continue
            if outcome is None:
                continue
            try:
                self._finding_store.record(
                    code,
                    terminal_id=(
                        outcome.terminal_id
                        if outcome.terminal_id is not None
                        else event.terminal_id
                    ),
                    dedupe_key=outcome.dedupe_key,
                    detail=outcome.detail,
                    sample_event_id=(
                        outcome.sample_event_id
                        if outcome.sample_event_id is not None
                        else event.event_id
                    ),
                )
            except Exception:  # noqa: BLE001 — recording a finding must not break an append
                logger.warning(
                    "worker-truth finding %s could not be recorded", code.value, exc_info=True
                )
