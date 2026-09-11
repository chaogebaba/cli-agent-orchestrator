"""The legacy status egress as a truth producer (AC4b (i), hook points 2 and 2b).

``StatusMonitor._publish_observation`` is the single egress that every status
origin passes through — incremental, probe, forced, native and native_poll all
converge there before a ``ReceiverState`` reaches the receiver store.  That is
why the hook is there and not at the on-demand probe path (``:2772``): the probe
fires only when the watchdog, the doorbell or the inbox asks, so a live
idle→busy flip on the continuous path would produce no row at all.  "Producer
hooked at the on-demand probe path" is one of the phase-1 mutants precisely
because the two look interchangeable and are not.

What this producer records is **what the fleet and the inbox actually consume**
(``fleet_service.py:209``).  Any comparison of the state projection is made
against THESE rows rather than against the raw classifier output, because
comparing a projection against its own upstream signal would be self-referential
— r9 retired that framing, and it outlived the AC10 report that first needed it.

Confidence is ``derived``, always.  The pane classifier is a first-class
fallback and never a deprecated one, but it is not authoritative for a terminal
whose adapter declares a JSONL source.

One edge: the ``(latched_status, origin)`` pair (B9).  A hundred identical
publishes are one row; that is what keeps the write rate off the single SQLite
writer on a busy fleet.

There were TWO until WP-ARCH sub-phase 2b.  The second was the fleet condition
label crossing into ``CAPPED``, and it moved to the classification site with the
rest of D1c: the cap is the one thing a rollout can never report, so a producer
that went quiet for exactly the terminals D1 suppresses this egress for would
take the capped-lane policy's evidence with it.  The condition is still READ here
and still recorded in the publish payload — it is part of what the fleet
consumed at this moment — but it no longer produces an event of its own.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from cli_agent_orchestrator.adapters.truth.wiring import emit, producer_runtime
from cli_agent_orchestrator.core.events import (
    PROJECTION_ORIGIN,
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
)

__all__ = [
    "CAPPED_CONDITION_LABEL",
    "UNKNOWN_STATUS",
    "PROJECTION_ORIGIN",
    "as_text",
    "fed_by",
    "effective_origin",
    "forget",
    "last_published_event_id",
    "record_fleet_override",
    "record_legacy_publish",
    "reset_edges",
]

logger = logging.getLogger(__name__)

#: ``StatusMonitor.get_condition`` returns the FLEET LABEL, not the
#: ``ConditionKind``; ``ConditionDelivery._fleet_label`` maps ``CAPPED`` to this
#: string.  Matching the label rather than the kind is deliberate — the label is
#: what the fleet row, the capped-lane policy and the operator all read.
CAPPED_CONDITION_LABEL = "CAPPED"

#: What a status that could not be rendered is recorded as, on BOTH truth
#: producers (WP-ARCH phase 2, plan §2).
#:
#: It used to be ``""``, and the empty string is the one value the vocabulary
#: cannot read: ``legacy_state('')`` is ``None``, so ``DIAG-PANE-DISAGREE``
#: SKIPPED such a row rather than comparing it — the check went quiet instead of
#: failing, and a box round counting disagreements by raw string comparison read
#: those rows as real ones.  ``unknown`` is in the legacy vocabulary, maps to
#: ``degraded``, and is therefore a value the checks can disagree with.
#:
#: No live caller can produce it: all five ``_publish_observation`` call sites
#: pass a real ``TerminalStatus`` (three ``classification.status``, one
#: ``resolution.status or UNKNOWN``, one ``_last_status.get(..., UNKNOWN)``).
#: It is the floor under a ``None`` that a future caller might introduce, and
#: under an object whose rendering fails.
UNKNOWN_STATUS = "unknown"


def fed_by(origin: str) -> str:
    """Which producer of record caused this publish (D5).

    D5's hazard, and the reason a one-word field is worth a decision: the moment
    D1 publishes the projection through this same egress, the published status is
    *caused by* the projection, so ``DIAG-LEGACY-DISAGREE`` would compare the
    projection with itself and report perfect agreement forever.  The comparison
    would not break; it would go quiet, which is worse.  Dark launching's shared
    invariant — Scientist is "only safe for wrapping methods that aren't changing
    data", Envoy's mirrored responses "are always ignored" — is that the candidate
    stays causally inert with respect to the control, and D1 ends that.

    So every publish names its feeder.  Its one reader, the AC10 agreement
    classifier, went with shadow-live mode (#738), so today the field is written
    and read by nothing.  It stays anyway, and deliberately: D5 must be written
    BEFORE D1 or the first publisher of the projection would already have made
    every comparison downstream self-confirming, and stamping provenance is
    cheaper than reconstructing it afterwards.  In sub-phase 2a there is no such
    publisher yet and this always answers ``pane``.
    """
    return PROJECTION_ORIGIN if origin == PROJECTION_ORIGIN else Producer.PANE.value


_lock = threading.Lock()
#: terminal_id -> the last published ``(latched_status, origin)`` pair.
_last_pair: dict[str, tuple[str, str]] = {}
#: terminal_id -> ``event_id`` of the most recent ``status.legacy_published``.
#: This is the evidence a ``fleet.override`` decision cites.  When it is absent
#: the override row is written with ``evidence=None`` on purpose: a decision that
#: cites nothing is exactly what ``DIAG-GHOST-TRANSITION`` exists to notice, and
#: silently inventing an evidence id would hide it.
_last_event_id: dict[str, str] = {}


def reset_edges() -> None:
    """Drop all edge state.  For tests, and for a bootstrap that re-installs."""
    with _lock:
        _last_pair.clear()
        _last_event_id.clear()


def last_published_event_id(terminal_id: str) -> str | None:
    """``event_id`` of this terminal's most recent ``status.legacy_published`` row.

    The evidence a server decision cites (AC4c).  ``None`` when nothing has been
    published yet for the terminal — a decision row is then written citing
    nothing, which is what ``DIAG-GHOST-TRANSITION`` is for.  Fabricating an id
    here to make the row look complete would defeat that check entirely, and
    "evidence dropped from the decision row" is a phase-1 mutant.
    """
    with _lock:
        return _last_event_id.get(terminal_id)


def forget(terminal_id: str) -> None:
    """Drop one terminal's edge state when it is deleted."""
    with _lock:
        _last_pair.pop(terminal_id, None)
        _last_event_id.pop(terminal_id, None)


def _as_text(value: Any) -> str | None:
    """Render an arbitrary legacy value as a JSON-safe string.

    ``latched_status`` is a ``TerminalStatus``, ``raw_classification`` is an
    opaque provider object, and both have to survive a ``json.dumps`` of the
    payload.  ``str()`` on an unknown object can itself raise, so it is guarded:
    losing one payload field is acceptable, raising into the status monitor's
    locked publish path is not.
    """
    if value is None:
        return None
    try:
        return str(getattr(value, "value", value))
    except Exception:  # pragma: no cover - defensive, an exotic __str__
        return "<unrenderable>"


def _effective_origin(origin: str | None, pass_outcome: Any) -> str:
    """Reproduce ``_publish_observation``'s own origin defaulting.

    The hook runs at the TOP of the function, before the ``ReceiverState`` that
    applies this defaulting is built.  Recording the raw parameter instead would
    make a ``None``-then-``"incremental"`` sequence look like an edge and emit a
    second row for one unchanged observation, which is the opposite of B9's
    intent.  The expression is kept identical to the legacy one on purpose; a
    test asserts ``None`` and ``"incremental"`` collapse to a single row.
    """
    if origin is not None:
        return origin
    return "forced" if _as_text(pass_outcome) == "forced" else "incremental"


#: Public aliases for the two renderers WP-ARCH phase 2's classification-site
#: producer (D1c) must reuse rather than reimplement.  Both producers edge-trigger
#: on the SAME ``(latched_status, origin)`` pair, and that pair has to be computed
#: by the same expression: a second copy would drift, and a drifted pair means one
#: producer emits an edge the other does not — which D5's comparison would then
#: report as a disagreement between the projection and the pane, when what
#: actually disagreed was two renderings of one string.
as_text = _as_text
effective_origin = _effective_origin


def record_legacy_publish(
    monitor: Any,
    terminal_id: str,
    latched_status: Any,
    origin: str | None,
    frame_source: Any,
    pass_outcome: Any,
    raw_classification: Any = None,
) -> None:
    """Hook point 2 — append ``status.legacy_published`` for one egress.

    Called with the status monitor's ``_lock`` held (it is an ``RLock``, so the
    ``get_condition`` read below is reentrant and safe).  Returns without doing
    anything when ingestion is off.  Never raises.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        status_text = _as_text(latched_status) or UNKNOWN_STATUS
        origin_text = _effective_origin(origin, pass_outcome)
        pair = (status_text, origin_text)

        condition: str | None = None
        getter = getattr(monitor, "get_condition", None)
        if callable(getter):
            try:
                condition = getter(terminal_id)
            except Exception:
                condition = None

        # ``fusion_reason`` is read straight out of the monitor's plain dict, the
        # way ``status_monitor`` itself reads it at ``:1302``.  The obvious
        # alternative, ``get_boundary_observation``, FUSES AT READ TIME and would
        # be called from inside the locked publish path — a re-entrant fuse
        # during a publish is a hazard this producer has no business creating,
        # and the dict already holds the evidence tag the payload wants.
        fusion_reason = _as_text(getattr(monitor, "_status_fusion_reason", {}).get(terminal_id))

        with _lock:
            publish_edge = _last_pair.get(terminal_id) != pair
            if publish_edge:
                _last_pair[terminal_id] = pair

        if not publish_edge:
            return

        stored = emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=EventKind.STATUS_LEGACY_PUBLISHED,
                producer=Producer.PANE,
                confidence=Confidence.DERIVED,
                observed_at=runtime.clock.now(),
                payload={
                    "latched_status": status_text,
                    "origin": origin_text,
                    "frame_source": _as_text(frame_source),
                    "pass_outcome": _as_text(pass_outcome),
                    "raw_classification": _as_text(raw_classification),
                    "fusion_reason": fusion_reason,
                    "condition": condition,
                    "fed_by": fed_by(origin_text),
                },
            )
        )
        if stored is not None:
            with _lock:
                _last_event_id[terminal_id] = stored.event_id
    except Exception:  # pragma: no cover - the guarantee, not a branch under test
        logger.debug("worker-truth legacy egress hook failed", exc_info=True)


def record_fleet_override(terminal_id: str, reason: str, detail: str = "") -> None:
    """Hook point 2b — one ``fleet.override`` decision row per ERROR override.

    ``fleet_service`` stamps ``TerminalStatus.ERROR`` over an observed status in
    three places (quarantine, window absence outside a teardown, failed init
    health).  #571 was exactly this: a healthy teardown rendered as ERROR and the
    reconstruction had to come out of pane archaeology.  Recording the override
    beside the observation it overrode is what makes that a one-command read.

    ``evidence`` is the ``event_id`` of this terminal's most recent
    ``status.legacy_published`` row — the observation the override replaced.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    try:
        with _lock:
            evidence = _last_event_id.get(terminal_id)
        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=DecisionKind.FLEET_OVERRIDE,
                producer=Producer.SERVER,
                confidence=Confidence.DERIVED,
                observed_at=runtime.clock.now(),
                decision=DecisionKind.FLEET_OVERRIDE,
                evidence=evidence,
                payload={"reason": reason, "detail": detail, "overridden_to": "ERROR"},
            )
        )
    except Exception:  # pragma: no cover - the guarantee, not a branch under test
        logger.debug("worker-truth fleet override hook failed", exc_info=True)
