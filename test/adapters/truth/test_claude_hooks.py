"""The claude_code hook producer (WP-ARCH phase 2, D3b / D4).

D3b's whole claim is negative — phase 2 adds no hook module, no hook binding and
no route — so what is left to test is small and load-bearing: the two kinds this
producer owns and nothing else, the ``hook:`` provenance scheme, and the
idempotency key reaching the draft rather than being dropped on the way.

The key's actual GUARANTEE is not testable here: it lives in the store's partial
unique index, and a fake store has no index.  ``test_idempotent_append.py``
asserts it against real SQLite, which is where it can be broken.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.adapters.truth import claude_hooks, wiring
from cli_agent_orchestrator.core.events import Confidence, EventKind, Producer

from .conftest import FakeClock, FakeEventStore

TERMINAL = "t-claude"


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("question_open", "prompt.awaiting"), ("question_clear", "prompt.answered")],
)
def test_the_two_marker_kinds_map_to_the_two_prompt_kinds(
    ingest_on: FakeEventStore, marker: str, expected: str
) -> None:
    """The hook fills the hatch phase 1 left open.

    ``PROMPT_AWAITING`` and ``PROMPT_ANSWERED`` were put in
    ``DERIVED_ALWAYS_KINDS`` by phase 1 so a dialog signal folds even while an
    authoritative source is healthy — and at that anchor nothing emitted either:
    three references in the tree, none of them a producer.  A mute-rule exemption
    cannot help an event nothing emits.
    """
    claude_hooks.record_interaction_marker(TERMINAL, marker, hook_event="Notification")

    assert ingest_on.kinds(TERMINAL) == [expected]


def test_the_hook_owns_these_kinds_and_no_others(ingest_on: FakeEventStore) -> None:
    """D3's split is evidential: the transcript records what the worker SAID and
    DID; it does not record that the worker is waiting on a human.

    ``turn.ended`` in particular is the tailer's — the r1 draft gave it to the
    ``Stop`` hook and the census refuted that — so a hook producer that grew it
    back would be two producers for one fact with no rule saying which wins.
    """
    assert set(claude_hooks.MARKER_KIND_TO_EVENT.values()) == {
        EventKind.PROMPT_AWAITING,
        EventKind.PROMPT_ANSWERED,
    }


def test_an_unknown_marker_kind_is_dropped_not_guessed(ingest_on: FakeEventStore) -> None:
    """The route's ``kind`` is a closed Literal, so this is defence against a
    future third value arriving before this producer knows what it means.

    Guessing would put a terminal into a state on the strength of a string nobody
    has defined.
    """
    claude_hooks.record_interaction_marker(TERMINAL, "question_maybe")
    assert ingest_on.rows == []


def test_the_row_is_authoritative_and_produced_by_the_hook(
    ingest_on: FakeEventStore,
) -> None:
    """The hook is the worker's own report of its own dialog state, not a reading
    of a screen — which is what makes it co-authoritative with the tailer rather
    than one more derived opinion."""
    claude_hooks.record_interaction_marker(TERMINAL, "question_open", hook_event="PreToolUse")
    row = ingest_on.rows[0]

    assert row.producer is Producer.HOOK
    assert row.confidence is Confidence.AUTHORITATIVE


def test_the_source_ref_carries_the_hook_scheme_and_the_key(
    ingest_on: FakeEventStore,
) -> None:
    """§5: ``hook:<hook_event_name>#<idempotency_key>``."""
    claude_hooks.record_interaction_marker(
        TERMINAL, "question_open", hook_event="PreToolUse", idempotency_key="k-1"
    )

    assert ingest_on.rows[0].source_ref == "hook:PreToolUse#k-1"


def test_the_idempotency_key_reaches_the_draft(ingest_on: FakeEventStore) -> None:
    """The mutant this kills is the key being used for the ref and dropped from
    the column — the ref would look right and the index would have nothing to
    enforce, so a retried POST would append twice with identical provenance."""
    claude_hooks.record_interaction_marker(
        TERMINAL, "question_open", hook_event="PreToolUse", idempotency_key="k-1"
    )

    assert ingest_on.rows[0].idempotency_key == "k-1"


def test_a_marker_with_no_key_is_still_recorded(ingest_on: FakeEventStore) -> None:
    """An older worker's hook carries no key, and the marker must still be applied.

    Refusing it would trade a dialog signal — the #386 family's missing signal,
    the reason this producer exists — for a dedup guarantee.  Provenance is worth
    having even where the guarantee is not, so the ref falls back to the marker
    kind rather than being dropped.
    """
    claude_hooks.record_interaction_marker(TERMINAL, "question_open", hook_event="Stop")
    row = ingest_on.rows[0]

    assert row.idempotency_key is None
    assert row.source_ref == "hook:Stop#question_open"


def test_with_ingestion_off_the_hook_producer_writes_nothing(
    store: FakeEventStore, clock: FakeClock
) -> None:
    """AC-2a's off arm, for this producer."""
    wiring.reset_producers()
    claude_hooks.record_interaction_marker(TERMINAL, "question_open", hook_event="Notification")

    assert store.rows == []


def test_a_failing_store_never_raises_into_the_route(ingest_on: FakeEventStore) -> None:
    """The route applies the marker to ``question_state`` first and appends second.

    An append that raised would take the interaction-marker endpoint down, so the
    dialog signal this producer exists to record would be the thing it broke.
    """
    ingest_on.fail_next = True
    claude_hooks.record_interaction_marker(TERMINAL, "question_open", hook_event="Notification")

    assert ingest_on.rows == []
