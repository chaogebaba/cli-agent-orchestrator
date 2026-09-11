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
    "ANSWERED_STATES",
    "FORWARD_STATUS_MAP",
    "LEGACY_STATUS_MAP",
    "LOSSY_FORWARD_STATES",
    "STATE_ASSERTING_KINDS",
    "answered_state",
    "implied_state",
    "legacy_state",
    "legacy_status",
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
#: (whose state is in its payload), ``status.pane_classified``, ``pane.recovered``
#: (a restore) and every decision kind — is handled by a named rule in the
#: projector.
#:
#: ``status.pane_classified`` (phase 2, D1c) is absent DELIBERATELY, and its
#: absence is the decision rather than an omission.  The row records what the pane
#: classifier WOULD have published; letting it assert a state would put the pane
#: path back in charge of the projection through a door phase 2 built for the
#: opposite purpose, and I1 would be false as designed.  It folds to
#: ``no_implied_state``, which is exactly right: it is evidence for the D5
#: comparison, not an observation of the worker.
STATE_ASSERTING_KINDS: frozenset[EventKind] = frozenset(_IMPLIED)

#: The legacy ``TerminalStatus`` vocabulary in the new one.  ``unknown`` and
#: ``render_uncertain`` collapse into ``degraded``, which is precisely the pair
#: the audit §3.1 says ``degraded`` replaces.  ``completed`` is IDLE: the fork
#: uses it for "the turn finished", not for "the process ended".  ``error`` is
#: EXITED because the fork raises ``TerminalInputBlockedError`` on it with the
#: words "the terminal's provider process has exited (status ERROR)" — #571's
#: complaint is that legacy reaches it during a healthy teardown, and the
#: ``DIAG-LEGACY-DISAGREE`` check exists to surface exactly that kind of
#: divergence rather than to paper over it.  (The AC10 agreement report used to
#: count it in bulk; it went with shadow-live mode, #738.)
LEGACY_STATUS_MAP: dict[str, WorkerState] = {
    "unknown": WorkerState.DEGRADED,
    "idle": WorkerState.IDLE,
    "processing": WorkerState.BUSY,
    "completed": WorkerState.IDLE,
    "waiting_user_answer": WorkerState.AWAITING_INPUT,
    "render_uncertain": WorkerState.DEGRADED,
    "error": WorkerState.EXITED,
}


#: The FORWARD map: what the projection publishes as a legacy ``TerminalStatus``
#: string (WP-ARCH phase 2, D1).  Strings, never the legacy enum, for the reason
#: the module docstring gives for :func:`legacy_state`; a test on the legacy side
#: of the fence pins every value here against the real enum.
#:
#: Two rows are CONDITIONAL and therefore not in this table — see
#: :func:`legacy_status`, which holds them:
#:
#: * ``IDLE`` reached by ``turn.ended`` publishes ``completed`` rather than
#:   ``idle``.  ``completed`` is not a state the projection holds: it is a screen
#:   classification the fork uses for "the turn finished", and adding an eighth
#:   ``WorkerState`` for it would break the audit's frozen seven-member enum and
#:   its 49-cell table for a distinction no consumer reads as a state.  The
#:   discriminator is free — the publisher is called from the fold and so holds
#:   the causing event's kind — and the leg it preserves is
#:   ``agent_step.py``'s ``_CompletionOutcome.COMPLETED``.
#: * ``DEGRADED`` publishes ``unknown`` for ``no_signal`` and ``render_uncertain``
#:   for every other reason.  Both map back to ``DEGRADED``, so the round trip
#:   holds either way; the split keeps the audit §3.1 pairing that ``degraded``
#:   replaced.
FORWARD_STATUS_MAP: dict[WorkerState, str] = {
    # Lossy.  Legacy has no member for "booting", and the choice between the
    # remaining ones is not free: ``idle`` would let ``inbox_service``'s
    # admission paste into a worker that has not finished starting.
    WorkerState.STARTING: "processing",
    WorkerState.IDLE: "idle",
    WorkerState.BUSY: "processing",
    WorkerState.AWAITING_INPUT: "waiting_user_answer",
    # Lossy, and the least obvious row in the table.  The cap's legacy carrier is
    # the CONDITION LABEL (``legacy_egress.CAPPED_CONDITION_LABEL``), which is
    # what the fleet row and the capped-lane policy actually read; the status is
    # not the carrier and must not pretend to be.  ``error`` would be actively
    # wrong — it maps back to ``EXITED`` and makes the fork raise
    # ``TerminalInputBlockedError`` on a worker that is merely waiting out a
    # usage window.
    WorkerState.CAPPED: "processing",
    WorkerState.DEGRADED: "render_uncertain",
    WorkerState.EXITED: "error",
}

#: The two states with NO legacy preimage: ``LEGACY_STATUS_MAP``'s image is the
#: other five, so a round trip through the legacy vocabulary cannot return them.
#:
#: This set is the blueprint §5b correction.  §5b asserts that
#: ``WorkerState -> TerminalStatus -> WorkerState`` is the identity, and for
#: these two it is unsatisfiable rather than unimplemented: both land on
#: ``processing`` and come back as ``BUSY``.  A test that enumerated all seven
#: and asserted identity would be asserting something false about the legacy
#: enum, so the round trip is scoped to the five that have a preimage and these
#: two are asserted lossy BY NAME, with the reason each lands where it does
#: written beside its row above.
LOSSY_FORWARD_STATES: frozenset[WorkerState] = frozenset({WorkerState.STARTING, WorkerState.CAPPED})


def legacy_status(
    state: WorkerState,
    *,
    causing_kind: AnyKind | None = None,
    degraded_reason: DegradedReason | None = None,
) -> str:
    """The legacy ``TerminalStatus`` string the projection publishes for ``state``.

    The forward direction of :func:`legacy_state`, and the function D1's publisher
    calls with the state it just folded into, the kind of the event that caused
    the fold, and the standing degraded reason.  Pure: it reads no projection, no
    clock and no configuration, so the whole mapping is decidable from a table
    plus two discriminators.

    ``causing_kind`` and ``degraded_reason`` are both optional and both ignored
    for every state that does not name them.  A caller with no causing kind — a
    sweep, a re-publish, a test — gets the unconditional row, which for ``IDLE``
    is ``idle``: the plain reading, and the safe one, since ``completed`` asserts
    that a turn just finished.
    """
    if state is WorkerState.IDLE and causing_kind is EventKind.TURN_ENDED:
        return "completed"
    if state is WorkerState.DEGRADED and degraded_reason is DegradedReason.NO_SIGNAL:
        return "unknown"
    return FORWARD_STATUS_MAP[state]


#: The only two states a ``prompt.answered`` row may assert (D1f).
#:
#: A dialog ending means the agent proceeded (``BUSY``) or the card was dismissed
#: and the terminal is ready (``IDLE``).  Every other reading in the legacy
#: vocabulary is a statement about something else — a dead process, an unreadable
#: screen — and this producer has no standing to make it.  See
#: :func:`answered_state` for what each rejected value would have cost.
ANSWERED_STATES: frozenset[WorkerState] = frozenset({WorkerState.IDLE, WorkerState.BUSY})


def answered_state(payload: dict[str, object]) -> WorkerState | None:
    """The state a ``prompt.answered`` row asserts, read from its own payload.

    D1f's producer records the pane reading that ENDED the dialog, and that
    reading is what the terminal is now in — ``processing`` for an answered card,
    ``idle`` for a dismissed one.  ``prompt.answered`` therefore cannot assert a
    state by kind: the same kind covers both outcomes, and the difference is the
    whole content of the event.

    CLAMPED to the two states a dialog edge can legitimately end in, and the
    clamp is the load-bearing part.  ``prompt.answered`` is in the projector's
    ``DERIVED_ALWAYS_KINDS``, so it applies with an authoritative source perfectly
    healthy — it is the one derived kind that bypasses source precedence for
    exactly the terminals D1 exists to protect.  Passing the pane's whole legacy
    vocabulary through would hand a dialog producer authority it does not have:

    * ``error`` maps to ``EXITED``, which is an ABSORBING state — the sweep skips
      an exited terminal forever and only ``session.started`` re-enters it — and
      ``process.exited`` belongs to the liveness probe, "of which it is the sole
      owner, in phase 1 and after".  A transient pane misread while a card is up
      (a redraw, a buffer eviction — the sticky-latch rules exist because this
      happens) would take a healthy sourced lane through a one-way door.
    * ``unknown`` and ``render_uncertain`` map to ``DEGRADED``, and this caller
      carries no :class:`DegradedReason`, so they would write an UNLABELLED
      degradation — which the closed-reason design exists to make impossible.

    Anything outside the clamp returns ``None`` and the caller falls back to the
    implied ``BUSY``: wrong in the same recoverable way the pre-clamp code was,
    and corrected by the source's next event rather than by a respawn.
    """
    raw = payload.get("latched_status")
    if not isinstance(raw, str):
        return None
    state = legacy_state(raw)
    return state if state in ANSWERED_STATES else None


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
    the projection by the disagreement check.
    """
    return LEGACY_STATUS_MAP.get(latched_status)


#: The reason a ``pane.missing`` event degrades a terminal with.  Named here so
#: the projector never spells a reason inline.
PANE_MISSING_REASON = DegradedReason.PANE_UNREADABLE
