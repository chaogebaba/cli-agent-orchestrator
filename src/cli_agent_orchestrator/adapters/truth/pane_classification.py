"""The pane classifier's own reading, recorded at the classification site (D1c).

WP-ARCH phase 2, sub-phase 2a.  This producer exists because of something phase 2
is about to do and phase 1 could not have anticipated.

Phase 1 hooked the legacy status path at the **egress**,
``StatusMonitor._publish_observation`` (hook point 2), and that was the right
seam for phase 1: the egress is where every origin converges, so one hook records
what the fleet and the inbox actually consume.  Phase 2's D1 then suppresses that
publish for a terminal whose authoritative source is healthy — and suppressing
the publish suppresses the ``status.legacy_published`` row with it.  For exactly
the terminals D5's comparison is about, one side of the comparison would vanish.

The classifier itself keeps running.  I7 requires it: the pane is a first-class
fallback for every unsourced terminal, so nothing may delete it, and D6's K2
demotes the publish rather than the classification.  So the reading still exists
at the moment the suppression happens; only its record was lost.  **This module
is that record**, appended inside ``_apply_detection`` rather than at the egress,
which is the whole content of the decision.

Two properties are load-bearing:

* **It is edge-triggered on the same ``(latched_status, origin)`` pair the legacy
  producer uses.**  A sourced terminal then costs one row per classification
  edge, not one per output chunk, which is the write-rate property §9 promises to
  measure.  The pair is computed with ``legacy_egress``'s own renderers rather
  than a second copy of the expression — see the note beside those aliases.
* **``confidence`` is ``derived``, always.**  A reading is not a fact.  What this
  row asserts is that the pane classifier *would have published* a status; the
  projection does not have to agree, and when it does not, that disagreement is
  the finding D5 repoints ``DIAG-PANE-DISAGREE`` at.

Sub-phase 2a ships the producer unconditionally: with no suppression in the tree
yet (D1 lands in 2b), a sourced terminal produces both this row and the egress's
``status.legacy_published``, describing one reading at two stages.  That is the
same shape an UNSOURCED terminal keeps permanently, so nothing here has to change
when 2b lands — only the egress side goes quiet for sourced terminals.

Sub-phase 2b then moves two DERIVED producers here for the same reason the
reading itself moved, and they are the whole of D1c's second half and D1f:

* **``usage.capped``** was appended by the egress producer off the same
  ``get_condition`` read.  The cap is the one thing a rollout can never report,
  and the capped-lane policy is the consumer that most needs it to survive — so
  it cannot be allowed to go quiet for exactly the sourced terminals D1
  suppresses the egress for.  It edge-triggers on the CONDITION crossing into
  ``CAPPED``, tracked apart from the classification edge because a cap can be
  detected while the latched status and origin sit still.
* **``prompt.awaiting`` / ``prompt.answered``** (D1f) edge-trigger on the latched
  status crossing into and out of ``waiting_user_answer``.  Every provider gets
  them, and codex is why: it has no dialog hook at all, so with the egress
  suppressed its ``AWAITING_INPUT`` would have no producer and #386's card would
  project as a busy terminal.  Both kinds are in the projector's
  ``DERIVED_ALWAYS_KINDS``, so they apply even while an authoritative source is
  healthy — which is the point, since the source is the thing that cannot see the
  card.

Both run BEFORE ``_publish_observation``, so on an unsourced terminal the
egress's own ``status.legacy_published`` folds immediately after and has the last
word on the state.  That ordering is what keeps a ``waiting -> idle`` edge — a
dismissed card rather than an answered one — from leaving the projection on
``prompt.answered``'s implied ``busy``: the pane's own reading arrives in the
same call and corrects it, and the two transition rows record what happened.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from cli_agent_orchestrator.adapters.truth.legacy_egress import (
    CAPPED_CONDITION_LABEL,
    UNKNOWN_STATUS,
    as_text,
    effective_origin,
)
from cli_agent_orchestrator.adapters.truth.wiring import emit, producer_runtime
from cli_agent_orchestrator.core.events import (
    Confidence,
    EventDraft,
    EventKind,
    Producer,
    SourceRefScheme,
    source_ref,
)

__all__ = [
    "AWAITING_STATUS",
    "forget",
    "record_pane_classification",
    "reset_edges",
]

logger = logging.getLogger(__name__)

#: The legacy ``TerminalStatus.WAITING_USER_ANSWER`` value, as a STRING.
#:
#: Spelled rather than imported for the reason ``app/worker_truth/mapping.py``
#: spells its whole vocabulary: ``adapters`` may not import ``models`` under
#: ``new-code-never-imports-legacy``.  A test on the legacy side of the fence
#: pins it against the real enum, which is what keeps the spelling honest.
AWAITING_STATUS = "waiting_user_answer"

_lock = threading.Lock()
#: terminal_id -> the last classified ``(latched_status, origin)`` pair.
_last_pair: dict[str, tuple[str, str]] = {}
#: terminal_id -> the last condition label seen at the classification site.
#:
#: Tracked APART from the classification pair (B9), and moved here from the
#: egress producer in 2b: a cap can be detected while the latched status and
#: origin are unchanged, and folding the condition into the pair would instead
#: make every condition change re-emit a classification row.
_last_condition: dict[str, str | None] = {}
#: terminal_id -> how many classification EDGES this producer has recorded for it.
#:
#: This is the ``<seq>`` of ``pane:<terminal_id>#<seq>`` (§5).  It is the
#: producer's own edge counter and deliberately not the store's ``seq``, which is
#: minted inside the append and so is unknowable at the moment the ref is built;
#: nor the monitor's ``_chunk_seq``, which does not advance on every edge and
#: would hand two different edges the same ref.  A provenance field that cannot
#: tell two observations apart is worse than a null, because a reader believes it.
_edge_seq: dict[str, int] = {}


def reset_edges() -> None:
    """Drop all edge state.  For tests, and for a bootstrap that re-installs."""
    with _lock:
        _last_pair.clear()
        _last_condition.clear()
        _edge_seq.clear()


def forget(terminal_id: str) -> None:
    """Drop one terminal's edge state when it is deleted."""
    with _lock:
        _last_pair.pop(terminal_id, None)
        _last_condition.pop(terminal_id, None)
        _edge_seq.pop(terminal_id, None)


def _read_condition(monitor: Any, terminal_id: str) -> str | None:
    """The monitor's live condition label, or ``None``.

    Read defensively and never fused: ``get_condition`` is a pure read under the
    monitor's own re-entrant lock, which is the only kind of read that is safe
    from inside the locked detection path.  A monitor that raises, or a caller
    that passed none at all, yields ``None`` — the same answer as "no condition",
    because a cap that could not be read is not evidence of a cap.
    """
    getter = getattr(monitor, "get_condition", None)
    if not callable(getter):
        return None
    try:
        label = getter(terminal_id)
    except Exception:
        return None
    return label if isinstance(label, str) else None


def _prompt_kind(previous: str | None, current: str) -> EventKind | None:
    """D1f — the dialog edge this classification crosses, if any.

    Edge-triggered on the latched status alone, so a re-render of the same card
    (the #386 shape: the pane repaints continuously while the card is up) is one
    ``prompt.awaiting`` and not one per chunk.

    A terminal first seen ALREADY waiting emits ``prompt.awaiting``: the card is
    up, nobody has recorded it, and the alternative — treating an unknown prior
    status as "no edge" — would lose the card on every server restart, which is
    precisely when a worker has been sitting on one unattended.
    """
    if current == AWAITING_STATUS:
        return None if previous == AWAITING_STATUS else EventKind.PROMPT_AWAITING
    if previous == AWAITING_STATUS:
        return EventKind.PROMPT_ANSWERED
    return None


def record_pane_classification(
    terminal_id: str,
    latched_status: Any,
    origin: str | None,
    frame_source: Any,
    pass_outcome: Any,
    raw_classification: Any = None,
    *,
    monitor: Any = None,
) -> None:
    """D1c/D1f — the three producers that live at the classification site.

    Called from inside ``_apply_detection``'s ``finally`` block, beside the
    publish it will one day outlive, with the status monitor's ``_lock`` held.
    Returns without doing anything when ingestion is off.  Never raises: a
    diagnostic that could raise into the locked detection path would turn a
    diagnosability feature into a status outage, which is the failure mode this
    whole work package exists to end.

    ``monitor`` is the status monitor itself, and it is optional so that the
    pre-2b call shape stays legal: with no monitor there is no condition to read
    and the ``usage.capped`` producer simply has nothing to say.

    Three INDEPENDENT edges are computed under one lock and emitted after it:
    the classification pair, the condition crossing into ``CAPPED``, and the
    dialog edge.  They are independent in both directions — a cap arrives with
    the status sitting still, a card arrives with the condition sitting still —
    so an early return on any one of them would silence the other two.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        # Shared with the egress producer rather than spelled twice: the two
        # rows describe one reading at two stages, and a fallback that differed
        # between them would read as a disagreement between the producers.
        status_text = as_text(latched_status) or UNKNOWN_STATUS
        origin_text = effective_origin(origin, pass_outcome)
        pair = (status_text, origin_text)
        condition = _read_condition(monitor, terminal_id)

        with _lock:
            previous_pair = _last_pair.get(terminal_id)
            publish_edge = previous_pair != pair
            seq = _edge_seq.get(terminal_id, 0)
            if publish_edge:
                _last_pair[terminal_id] = pair
                seq += 1
                _edge_seq[terminal_id] = seq
            condition_edge = (
                condition == CAPPED_CONDITION_LABEL
                and _last_condition.get(terminal_id) != CAPPED_CONDITION_LABEL
            )
            _last_condition[terminal_id] = condition

        previous_status = previous_pair[0] if previous_pair is not None else None
        prompt_kind = _prompt_kind(previous_status, status_text)

        if not publish_edge and not condition_edge and prompt_kind is None:
            return

        observed_at = runtime.clock.now()
        # The ref every row from this edge carries.  Shared on purpose: the
        # dialog row and the classification row are two readings of ONE pane
        # observation, and a diag view that could not join them would make "what
        # did the screen say when the card appeared" unanswerable.
        ref = source_ref(SourceRefScheme.PANE, terminal_id, seq)

        if condition_edge:
            emit(
                EventDraft(
                    terminal_id=terminal_id,
                    kind=EventKind.USAGE_CAPPED,
                    producer=Producer.PANE,
                    confidence=Confidence.DERIVED,
                    observed_at=observed_at,
                    payload={"condition": condition, "latched_status": status_text},
                )
            )

        if prompt_kind is not None:
            emit(
                EventDraft(
                    terminal_id=terminal_id,
                    kind=prompt_kind,
                    producer=Producer.PANE,
                    confidence=Confidence.DERIVED,
                    observed_at=observed_at,
                    source_ref=ref,
                    payload={
                        "latched_status": status_text,
                        "prior_status": previous_status,
                        "origin": origin_text,
                    },
                )
            )

        if not publish_edge:
            return

        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=EventKind.STATUS_PANE_CLASSIFIED,
                producer=Producer.PANE,
                confidence=Confidence.DERIVED,
                observed_at=observed_at,
                source_ref=ref,
                payload={
                    # The three fields D1c names.  ``latched_status`` is the
                    # WOULD-BE publish — what the pane path is about to assert,
                    # whether or not D1's suppression lets it through — and
                    # ``raw_classification`` is what the provider classifier
                    # actually returned before the latch rules touched it.  Both,
                    # because the disagreement worth recording can live in either:
                    # a classifier that read the screen wrongly, or a latch that
                    # held a correct reading back.
                    "latched_status": status_text,
                    "origin": origin_text,
                    "frame_source": as_text(frame_source),
                    "pass_outcome": as_text(pass_outcome),
                    "raw_classification": as_text(raw_classification),
                },
            )
        )
    except Exception:  # pragma: no cover - the guarantee, not a branch under test
        logger.debug("worker-truth pane classification hook failed", exc_info=True)
