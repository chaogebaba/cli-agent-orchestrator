"""WP-HERDR Seam B's own boot switch (H2-S1).

The entry audit's A3 records why this file exists: WP-ARCH 3c collapsed
``CAO_DELIVERY_QUEUE`` to a single position and deleted it, so "3c left no
switch to hang a rollback on" and H2 owes a new one.  What is asserted here is
the SWITCH's contract — the composition root's use of it is asserted by the
selection tests that land with the injector.

``CAO_HERDR_DELIVERY`` is deliberately a FOURTH variable rather than a position
on ``CAO_HERDR_RUNTIME``: that one arms Seam A (whom to BELIEVE about a
terminal's lifecycle) and this one arms Seam B (how to SUBMIT a prompt to it).
Running herdr's lifecycle truth while keeping the composer paste was H1's entire
shipping story, and one master flag would delete that position.
"""

from __future__ import annotations

from cli_agent_orchestrator import bootstrap


def test_the_switch_defaults_off() -> None:
    """OFF is pre-H2 behaviour, byte-identical: the paste injector, unconditionally."""
    assert bootstrap.herdr_delivery_enabled({}) is False


def test_the_switch_is_on_only_at_exactly_one() -> None:
    assert bootstrap.herdr_delivery_enabled({bootstrap.HERDR_DELIVERY_ENV_VAR: "1"}) is True
    for spelling in ("true", "yes", "on", "0", ""):
        assert bootstrap.herdr_delivery_enabled({bootstrap.HERDR_DELIVERY_ENV_VAR: spelling}) is (
            False
        )


def test_seam_a_and_seam_b_are_separate_variables() -> None:
    """Arming the lifecycle seam does not arm the submission seam, or the reverse."""
    from cli_agent_orchestrator.utils.herdr_runtime_gate import HERDR_RUNTIME_ENV_VAR

    assert bootstrap.HERDR_DELIVERY_ENV_VAR != HERDR_RUNTIME_ENV_VAR
    assert bootstrap.herdr_delivery_enabled({HERDR_RUNTIME_ENV_VAR: "1"}) is False


def test_the_switch_reads_the_mapping_it_is_given(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An explicit mapping wins over the process environment.

    Which is what lets the selection tests state a position without mutating
    ``os.environ`` for the rest of the session.
    """
    monkeypatch.setenv(bootstrap.HERDR_DELIVERY_ENV_VAR, "1")
    assert bootstrap.herdr_delivery_enabled({}) is False
    assert bootstrap.herdr_delivery_enabled() is True
