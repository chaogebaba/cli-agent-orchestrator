"""The shared two-position boot-switch idiom (WP-HERDR H2-S1).

``core/switches.py`` already owned what a switch says about a position it once
accepted and no longer does (#738).  H2 adds the other half: what a switch that
never had a third position does with the two it has.  It is here rather than
hand-rolled in ``bootstrap.py`` because the fourth phase to want one would
otherwise be the fourth hand-rolled parser, and the three properties below are
each a decision an earlier phase made the hard way.

The ACP plane's ``CAO_SEAT_TRANSPORT`` is the next caller (the entry audit's A3:
the post-3c idiom "does not exist yet", and ``grep CAO_SEAT_TRANSPORT`` over
``src/`` returned zero at ``ad4339e9``).  So the contract is pinned by tests
here, not only exercised through H2's own switch.
"""

from __future__ import annotations

from cli_agent_orchestrator.core.switches import (
    BOOT_SWITCH_ON,
    Rejected,
    boot_switch_enabled,
    retired_position,
)


def test_an_absent_variable_is_off() -> None:
    """Default OFF is what makes merging a strangler phase inert (#738)."""
    assert boot_switch_enabled("CAO_ANY_PHASE", {}) is False


def test_the_on_position_is_exactly_one() -> None:
    assert BOOT_SWITCH_ON == "1"
    assert boot_switch_enabled("CAO_ANY_PHASE", {"CAO_ANY_PHASE": "1"}) is True


def test_the_friendly_spellings_of_true_are_not_accepted() -> None:
    """A switch nobody can read off a process listing is not a switch.

    ``CAO_WORKER_TRUTH_INGEST`` made this choice first; stating it as a test is
    what stops the next phase quietly widening it back out.
    """
    for spelling in ("true", "TRUE", "yes", "on", "On", "0", " 1", "1 ", ""):
        assert boot_switch_enabled("CAO_ANY_PHASE", {"CAO_ANY_PHASE": spelling}) is False


def test_the_switch_reads_the_mapping_it_is_given() -> None:
    """PURE: no ``os.environ`` read, which is what lets it live in ``core``.

    A test states a position by passing one, never by mutating process state —
    and that is also why ``core-is-pure`` stays satisfied.
    """
    assert boot_switch_enabled("A", {"A": "1", "B": "1"}) is True
    assert boot_switch_enabled("B", {"A": "1"}) is False


def test_a_two_position_switch_still_has_no_retired_position() -> None:
    """The two halves of this module do not overlap.

    ``retired_position`` answers for a value that USED to work; a switch that
    only ever had two positions has no such value, so the idiom above never
    produces a :class:`Rejected`.  Asserted so that a later reader does not wire
    the rejection into a switch that cannot have earned one.
    """
    rejected = retired_position(env_var="CAO_ANY_PHASE", value="shadow", accepted="1")
    assert isinstance(rejected, Rejected)
    assert boot_switch_enabled("CAO_ANY_PHASE", {"CAO_ANY_PHASE": "shadow"}) is False
