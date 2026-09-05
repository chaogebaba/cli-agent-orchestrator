"""AC-2a — the ``source_ref`` prefix set is closed at exactly three (D4).

The criterion in the blueprint's own words: *"A test asserts the set of prefixes
the constructor D4 adds to ``core/events.py`` can produce is exactly
``{transcript:, hook:, pane:}`` … a fourth prefix fails it."*

What makes this worth a test rather than a convention is D4's correction.  An
earlier draft claimed both producers derived ``source_ref`` from one transcript
record and so joined by identity.  They do not: there are three schemes, one per
producer, and no two are alike, so a key of ``(terminal_id, kind, source_ref)``
does NOT collapse a pair observing one fact.  The field is PROVENANCE, and the
fold's idempotency is per-producer and lives in the transition table's diagonal.
A fourth scheme appearing unnoticed is how that stops being true — a reader would
find a ref shape no producer claims and have no way to tell which one wrote it.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.core.events import (
    SOURCE_REF_PREFIXES,
    SourceRefScheme,
    source_ref,
)


def test_the_prefix_set_is_exactly_the_three() -> None:
    """EQUALITY, not containment — a fourth prefix fails it, and so does a lost one."""
    assert SOURCE_REF_PREFIXES == {"transcript:", "hook:", "pane:"}


def test_every_prefix_the_constructor_can_produce_is_in_the_set() -> None:
    """The set is derived from the enum, so this catches the two drifting apart."""
    produced = {
        source_ref(scheme, "subject", "discriminator").split("#")[0].split(":")[0] + ":"
        for scheme in SourceRefScheme
    }
    assert produced == SOURCE_REF_PREFIXES


@pytest.mark.parametrize(
    ("scheme", "subject", "discriminator", "expected"),
    [
        (
            SourceRefScheme.TRANSCRIPT,
            "/home/u/.claude/projects/p/abc.jsonl",
            "rec-uuid",
            "transcript:/home/u/.claude/projects/p/abc.jsonl#rec-uuid",
        ),
        (SourceRefScheme.HOOK, "PreToolUse", "key-1", "hook:PreToolUse#key-1"),
        (SourceRefScheme.PANE, "t-1", 7, "pane:t-1#7"),
    ],
)
def test_each_scheme_builds_the_shape_the_contract_names(
    scheme: SourceRefScheme, subject: str, discriminator: object, expected: str
) -> None:
    """§5's table, one row each.  The shapes are the contract, not an example."""
    assert source_ref(scheme, subject, discriminator) == expected


@pytest.mark.parametrize("subject, discriminator", [("", "d"), ("s", ""), ("  ", "d")])
def test_a_half_built_ref_is_refused(subject: str, discriminator: str) -> None:
    """A ref missing either half would look like provenance and identify nothing.

    That is worse than a null, because a null tells a reader there is nothing to
    trace while ``transcript:/some/path#`` tells them there is.
    """
    with pytest.raises(ValueError):
        source_ref(SourceRefScheme.TRANSCRIPT, subject, discriminator)


def test_the_codex_rollout_prefix_is_deliberately_not_a_member() -> None:
    """Phase 1's tailer keys its own refs ``rollout:`` and phase 2 does not touch it.

    Recorded as an assertion rather than left implicit, because the blueprint's D4
    states that at its anchor "nothing constructs" a ``source_ref``, which is not
    so — ``CodexRolloutSource._source_ref`` does, under a fourth prefix.  This
    constructor is the closed set for the schemes PHASE 2 introduces; rewriting a
    shipped producer's provenance format is not in 2a's build line.  A future
    revision that decides to fold codex in has to change this test, which is the
    point of writing it down.
    """
    assert "rollout:" not in SOURCE_REF_PREFIXES
