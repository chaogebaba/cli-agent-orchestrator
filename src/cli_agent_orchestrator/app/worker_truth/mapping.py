"""What an event MEANS about a terminal's state (WP-ARCH phase 1, AC6 support).

Two translations live here, deliberately apart from the projector so the
projector reads as rules rather than as a table of special cases:

* :func:`implied_state` — the state a boundary event asserts the terminal is in.
* :func:`legacy_state` — the legacy ``TerminalStatus`` string, as carried in a
  ``status.legacy_published`` payload, expressed in the new vocabulary.

:func:`legacy_state` maps from the STRING, never from the legacy enum.  Importing
``models.terminal`` here would break ``new-code-never-imports-legacy`` (AC9) for
the sake of seven string constants; the strings are the wire format of the
payload the legacy egress writes, and a test pins them against the real enum from
the legacy side of the fence, where importing legacy is allowed.
"""

from __future__ import annotations

from cli_agent_orchestrator.core.events import AnyKind, EventKind
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState

__all__ = [
    "LEGACY_STATUS_MAP",
    "STATE_ASSERTING_KINDS",
    "implied_state",
    "legacy_state",
]


# Which boundary events assert a state, and which state.
#
# Three of the mappings are worth their comment because the obvious alternative
# is defensible and wrong:
#
# * ``session.started`` asserts STARTING, not IDLE.  The transition table makes
#   ``exited -> starting`` the only non-anomalous way into ``starting``, labelled
#   "respawn" in the audit; that is exactly what a fresh ``session_meta`` record
#   on a terminal means.  Mapping it to IDLE would spend the one cell the table
#   reserves for detecting a mis-attributed launch.
# * ``session.resumed`` asserts IDLE.  A resumed session did not start a process;
#   it re-attached to one that is ready for work.  ``idle -> starting`` is
#   anomalous, so mapping resume to STARTING would flag every ordinary resume.
# * ``prompt.answered`` asserts BUSY.  The dialog card is gone and the agent is
#   proceeding; the alternative — restoring ``prior_state`` — would be right only
#   if the card had interrupted an idle terminal, which is not the #386 shape.
#
# ``pane.recovered`` is absent on purpose: it asserts no state of its own.  It
# cancels a degradation, and the projector restores ``prior_state`` for it.
_IMPLIED: dict[EventKind, WorkerState] = {
    EventKind.SESSION_STARTED: WorkerState.STARTING,
    EventKind.SESSION_RESUMED: WorkerState.IDLE,
    EventKind.TURN_STARTED: WorkerState.BUSY,
    EventKind.TURN_ENDED: WorkerState.IDLE,
    EventKind.TOOL_CALLED: WorkerState.BUSY,
    EventKind.TOOL_RESULT: WorkerState.BUSY,
    EventKind.PROMPT_AWAITING: WorkerState.AWAITING_INPUT,
    EventKind.PROMPT_ANSWERED: WorkerState.BUSY,
    EventKind.SUBMISSION_CONFIRMED: WorkerState.BUSY,
    EventKind.USAGE_CAPPED: WorkerState.CAPPED,
    EventKind.PROCESS_EXITED: WorkerState.EXITED,
    EventKind.PANE_MISSING: WorkerState.DEGRADED,
}

#: The kinds that assert a state.  Everything else — ``status.legacy_published``
#: (whose state is in its payload), ``pane.recovered`` (a restore) and every
#: decision kind — is handled by a named rule in the projector.
STATE_ASSERTING_KINDS: frozenset[EventKind] = frozenset(_IMPLIED)

#: The legacy ``TerminalStatus`` vocabulary in the new one.  ``unknown`` and
#: ``render_uncertain`` collapse into ``degraded``, which is precisely the pair
#: the audit §3.1 says ``degraded`` replaces.  ``completed`` is IDLE: the fork
#: uses it for "the turn finished", not for "the process ended".  ``error`` is
#: EXITED because the fork raises ``TerminalInputBlockedError`` on it with the
#: words "the terminal's provider process has exited (status ERROR)" — #571's
#: complaint is that legacy reaches it during a healthy teardown, and the
#: agreement report (AC10) exists to measure exactly that kind of divergence
#: rather than to paper over it.
LEGACY_STATUS_MAP: dict[str, WorkerState] = {
    "unknown": WorkerState.DEGRADED,
    "idle": WorkerState.IDLE,
    "processing": WorkerState.BUSY,
    "completed": WorkerState.IDLE,
    "waiting_user_answer": WorkerState.AWAITING_INPUT,
    "render_uncertain": WorkerState.DEGRADED,
    "error": WorkerState.EXITED,
}


def implied_state(kind: AnyKind) -> WorkerState | None:
    """The state ``kind`` asserts, or ``None`` when it asserts none.

    Decision kinds always return ``None``: the server's own rows record what the
    server did, and a decision never moves the projection by itself.
    """
    if isinstance(kind, EventKind):
        return _IMPLIED.get(kind)
    return None


def legacy_state(latched_status: str) -> WorkerState | None:
    """Translate a ``status.legacy_published`` payload's ``latched_status``.

    Returns ``None`` for a status this map does not know, which is not an error:
    the legacy enum can grow, and the honest answer for an unrecognised value is
    "no opinion" rather than a guessed state that would then be compared against
    the projection in the agreement report.
    """
    return LEGACY_STATUS_MAP.get(latched_status)


#: The reason a ``pane.missing`` event degrades a terminal with.  Named here so
#: the projector never spells a reason inline.
PANE_MISSING_REASON = DegradedReason.PANE_UNREADABLE
