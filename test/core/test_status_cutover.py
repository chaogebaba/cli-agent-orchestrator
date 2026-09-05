"""D9's boot guard for the status cutover, asserted cell by cell.

The resolution table is TOTAL over requested position × condition, which is the
property phase 3's D9 has and this phase's own r1 and r2 did not.  Totality is
only worth claiming if every cell is exercised, so every cell is a case here —
including the three that raise ``DIAG-STATUS-GUARD``, which §12 asks for as a
startup check precisely because no session-level acceptance criterion drove any
of them.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.status_cutover import (
    StatusPosition,
    parse_providers,
    parse_status_switch,
    resolve_status_switch,
)

# -- parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, StatusPosition.OFF),
        ("", StatusPosition.OFF),
        ("off", StatusPosition.OFF),
        ("shadow", StatusPosition.SHADOW),
        ("  SHADOW  ", StatusPosition.SHADOW),
        ("on", StatusPosition.ON),
        # An operator who typed something else gets the SAFE default and learns
        # from the guard's finding. Guessing at an intended position would be a
        # worse failure than the default, because the default is the safe one.
        ("true", StatusPosition.OFF),
        ("1", StatusPosition.OFF),
        ("drain", StatusPosition.OFF),
    ],
)
def test_parse_is_permissive_about_case_and_nothing_else(
    raw: str | None, expected: StatusPosition
) -> None:
    assert parse_status_switch(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("codex", frozenset({"codex"})),
        (" codex , claude_code ", frozenset({"codex", "claude_code"})),
        ("codex,,", frozenset({"codex"})),
        ("CODEX", frozenset({"codex"})),
    ],
)
def test_the_allowlist_is_a_list_not_a_boolean(raw: str | None, expected: frozenset) -> None:
    """MetaMask's correction: a single-provider flag cannot be rolled out per key.

    This phase ships two sources, so the switch has to advance one provider at a
    time or the first flip is a fleet-wide flip.
    """
    assert parse_providers(raw) == expected


# -- the resolution table, cell by cell -------------------------------------


@pytest.mark.parametrize("ingest", [True, False])
@pytest.mark.parametrize("providers", [frozenset(), frozenset({"codex"})])
def test_off_stays_off_under_every_condition(ingest: bool, providers: frozenset) -> None:
    """Row 1: ``off`` is exactly phase-1 behaviour and nothing can promote it."""
    outcome = resolve_status_switch(StatusPosition.OFF, ingest_enabled=ingest, providers=providers)
    assert outcome.position is StatusPosition.OFF
    assert outcome.finding is None
    assert not outcome.demoted


@pytest.mark.parametrize("requested", [StatusPosition.SHADOW, StatusPosition.ON])
@pytest.mark.parametrize("providers", [frozenset(), frozenset({"codex"})])
def test_ingestion_off_demotes_to_off_and_says_so(
    requested: StatusPosition, providers: frozenset
) -> None:
    """Rows 2 and 4.  A fold whose events reach no consumer is a silent status
    outage: the producers would run, the projection would move, and nothing would
    read it.  The finding is how the operator learns."""
    outcome = resolve_status_switch(requested, ingest_enabled=False, providers=providers)
    assert outcome.position is StatusPosition.OFF
    assert outcome.finding is FindingCode.DIAG_STATUS_GUARD
    assert outcome.demoted
    assert outcome.detail


@pytest.mark.parametrize("providers", [frozenset(), frozenset({"codex"})])
def test_shadow_with_ingestion_on_resolves_to_shadow_whatever_the_allowlist_says(
    providers: frozenset,
) -> None:
    """Row 3.  The allowlist gates the FEED, and shadow has no feed, so it is not
    a condition on this cell — writing it as one would make an operator set a
    variable to get a position that ignores it."""
    outcome = resolve_status_switch(StatusPosition.SHADOW, ingest_enabled=True, providers=providers)
    assert outcome.position is StatusPosition.SHADOW
    assert outcome.finding is None
    assert not outcome.demoted


def test_on_with_an_empty_allowlist_demotes_to_shadow() -> None:
    """Row 5.  ``on`` with no provider to publish for is indistinguishable from a
    misconfiguration, and reading it as "publish for everything" would turn a
    typo into a fleet-wide cutover."""
    outcome = resolve_status_switch(StatusPosition.ON, ingest_enabled=True, providers=frozenset())
    assert outcome.position is StatusPosition.SHADOW
    assert outcome.finding is FindingCode.DIAG_STATUS_GUARD
    assert outcome.demoted


def test_on_with_a_provider_named_resolves_to_on() -> None:
    """Row 6, the only cell that reaches the feed."""
    outcome = resolve_status_switch(
        StatusPosition.ON, ingest_enabled=True, providers=frozenset({"codex"})
    )
    assert outcome.position is StatusPosition.ON
    assert outcome.finding is None
    assert not outcome.demoted
    assert outcome.providers == frozenset({"codex"})


def test_the_table_is_total() -> None:
    """Every cell of position × ingestion × allowlist resolves, and none raises.

    Totality is the claim D9 makes; a case-by-case test proves each cell but not
    that the cells are all of them.  This one does, and it is also the guard
    against a future position being added to the enum with no rule: the loop would
    reach it and the resolution would be whatever the last branch happened to be.
    """
    for requested in StatusPosition:
        for ingest in (True, False):
            for providers in (frozenset(), frozenset({"codex", "claude_code"})):
                outcome = resolve_status_switch(
                    requested, ingest_enabled=ingest, providers=providers
                )
                assert outcome.position in set(StatusPosition)
                assert outcome.requested is requested
                # A demotion always explains itself; a non-demotion never invents
                # a finding for a position the operator got exactly as asked.
                assert (outcome.finding is not None) == (
                    outcome.demoted and requested is not StatusPosition.OFF
                )


def test_no_position_is_ever_promoted() -> None:
    """The guard demotes and never promotes.

    Phase 3's guard can promote ``drain`` to ``shadow`` over an empty queue, and
    that asymmetry is worth pinning here so a later reader does not import it: an
    operator who asked for ``off`` and got ``shadow`` would be running producers
    they did not ask for, and the whole point of the ladder is that the kill
    switch is always to move DOWN.
    """
    rank = {StatusPosition.OFF: 0, StatusPosition.SHADOW: 1, StatusPosition.ON: 2}
    for requested in StatusPosition:
        for ingest in (True, False):
            for providers in (frozenset(), frozenset({"codex"})):
                outcome = resolve_status_switch(
                    requested, ingest_enabled=ingest, providers=providers
                )
                assert rank[outcome.position] <= rank[requested]
