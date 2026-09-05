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
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from cli_agent_orchestrator.adapters.truth.legacy_egress import as_text, effective_origin
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
    "forget",
    "record_pane_classification",
    "reset_edges",
]

logger = logging.getLogger(__name__)

_lock = threading.Lock()
#: terminal_id -> the last classified ``(latched_status, origin)`` pair.
_last_pair: dict[str, tuple[str, str]] = {}
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
        _edge_seq.clear()


def forget(terminal_id: str) -> None:
    """Drop one terminal's edge state when it is deleted."""
    with _lock:
        _last_pair.pop(terminal_id, None)
        _edge_seq.pop(terminal_id, None)


def record_pane_classification(
    terminal_id: str,
    latched_status: Any,
    origin: str | None,
    frame_source: Any,
    pass_outcome: Any,
    raw_classification: Any = None,
) -> None:
    """D1c — append ``status.pane_classified`` for one classification edge.

    Called from inside ``_apply_detection``'s ``finally`` block, beside the
    publish it will one day outlive, with the status monitor's ``_lock`` held.
    Returns without doing anything when ingestion is off.  Never raises: a
    diagnostic that could raise into the locked detection path would turn a
    diagnosability feature into a status outage, which is the failure mode this
    whole work package exists to end.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        status_text = as_text(latched_status) or ""
        origin_text = effective_origin(origin, pass_outcome)
        pair = (status_text, origin_text)

        with _lock:
            if _last_pair.get(terminal_id) == pair:
                return
            _last_pair[terminal_id] = pair
            seq = _edge_seq.get(terminal_id, 0) + 1
            _edge_seq[terminal_id] = seq

        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=EventKind.STATUS_PANE_CLASSIFIED,
                producer=Producer.PANE,
                confidence=Confidence.DERIVED,
                observed_at=runtime.clock.now(),
                source_ref=source_ref(SourceRefScheme.PANE, terminal_id, seq),
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
