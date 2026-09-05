"""The claude_code hook producer — a server-side append at two shipped routes (D3b).

**Phase 2 adds no hook module, no hook binding and no route.**  That is the whole
of D3b, and it is a claim about what is already deployed rather than a plan:

* the per-terminal settings generator already binds ``question_marker`` to
  ``Notification``, ``PreToolUse``/``PostToolUse(AskUserQuestion)``,
  ``PostToolUseFailure`` and ``Stop``;
* that module already classifies the stdin payload's ``hook_event_name`` and
  POSTs to ``/terminals/{id}/interaction-marker``, authenticated by
  ``X-CAO-Terminal-Token`` against the terminal's own row;
* ``transcript_binding`` already reports the session and transcript path to
  ``/terminals/{id}/transcript-binding``.

So phase 2 appends its events **in those route handlers**, the way phase 1
appended at ``StatusMonitor._publish_observation``.  The only worker-side change
in the whole sub-phase is one field — D4's ``idempotency_key`` — added to the
body the marker hook already builds.

*Rejected:* a new ``hooks/worker_event.py`` with its own binding and its own
``POST /terminals/{id}/worker-events``, at the cost of a second hook process per
event on every claude_code turn, a second auth surface, a second dead-letter path
and a second thing that can be missing from a terminal's settings file — for a
payload the existing POST already carries.  The loopback bind, per-terminal token
and idempotency key the parent's AC5 fixed as the hooks contract are all satisfied
by the existing route; the contract named a shape, not a new endpoint.

**What this producer owns, and only this.**  ``prompt.awaiting`` and
``prompt.answered``, because the transcript does not record that the worker is
waiting on a human and these hooks do — the #386 family's missing signal.  The
tailer owns everything else, ``turn.ended`` included (D3).  A slow or dead hook
therefore delays one dialog signal rather than stalling a terminal's status,
which is the other half of D3's liveness argument and the reason the split is
evidential rather than stylistic.

**Confidence is ``authoritative``.**  The hook is the worker's own report of its
own dialog state, not a reading of a screen.  It is co-authoritative with the
tailer rather than subordinate to it: the two own disjoint kinds, so there is no
precedence question between them in this sub-phase.  D1f creates the first kind
with two producers, and it lands in 2b.
"""

from __future__ import annotations

import logging

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
    "MARKER_KIND_TO_EVENT",
    "record_interaction_marker",
]

logger = logging.getLogger(__name__)

#: The interaction-marker route's closed ``kind`` vocabulary, mapped onto the two
#: event kinds this producer owns.  The route's vocabulary is deliberately
#: provider-agnostic (F507's Do-NOT #8) and this mapping keeps it that way: the
#: marker says an interaction opened or cleared, and the event names what that
#: means for the projection.
MARKER_KIND_TO_EVENT = {
    "question_open": EventKind.PROMPT_AWAITING,
    "question_clear": EventKind.PROMPT_ANSWERED,
}


def record_interaction_marker(
    terminal_id: str,
    marker_kind: str,
    *,
    hook_event: str = "",
    tool_name: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """Append one ``prompt.awaiting``/``prompt.answered`` for a marker POST.

    Called from inside the ``interaction-marker`` route handler, after the marker
    has been applied to ``question_state``.  Returns without doing anything when
    ingestion is off.  Never raises: an append that could fail the route would
    turn a diagnostic into a dropped dialog signal, which is the #386 family's
    own failure mode arriving through the fix for it.

    ``idempotency_key`` is the caller's, and its absence is not an error.  The
    hook's existing cooldown gate is a RATE LIMITER, not a dedup — its own comment
    says the endpoint is idempotent anyway — so the partial unique index on the
    column is what actually makes a retried POST append once.  Stripe's shape: the
    client generates the key and the server uses it to recognise retries.  One
    divergence is deliberate: Stripe replays a cached response and the duplicate
    leaves no row, whereas here a duplicate OBSERVATION is data, because the
    ``producer`` column exists to record that two sources saw one turn.  What must
    not double is one producer's own retry.
    """
    runtime = producer_runtime()
    if runtime is None:
        return
    kind = MARKER_KIND_TO_EVENT.get(marker_kind)
    if kind is None:
        return
    try:
        # §5: ``hook:<hook_event_name>#<idempotency_key>``.  With no key supplied
        # the marker kind stands in as the discriminator, so the ref still names
        # what it came from rather than being dropped — provenance is worth having
        # even when the dedup guarantee is not.
        ref = source_ref(
            SourceRefScheme.HOOK,
            hook_event or marker_kind,
            idempotency_key or marker_kind,
        )
        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=kind,
                producer=Producer.HOOK,
                confidence=Confidence.AUTHORITATIVE,
                observed_at=runtime.clock.now(),
                source_ref=ref,
                idempotency_key=idempotency_key,
                payload={
                    "marker_kind": marker_kind,
                    "hook_event": hook_event,
                    "tool_name": tool_name,
                },
            )
        )
    except Exception:  # pragma: no cover - the guarantee, not a branch under test
        logger.debug("worker-truth interaction marker hook failed", exc_info=True)
