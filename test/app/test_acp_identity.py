"""AC-S1.12 — D22's identity binding, and the permanent-veto bug it removes.

The AC's fails-if is one sentence: "``unavailable`` behaves as a permanent veto
(today's measured bug)".  Every arm below exists to pin the difference between
"provably wrong" and "not known yet", because collapsing those two is the bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cli_agent_orchestrator.app.acp.identity import (
    IdentityBinding,
    IdentityObservation,
    IdentityOutcome,
    decide_identity,
)
from cli_agent_orchestrator.core.findings import FindingCode

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
BOUND_S = 30.0


def _binding(**overrides: object) -> IdentityBinding:
    fields: dict[str, object] = {
        "terminal_id": "term-a",
        "token": "tok-secret",
        "lifecycle_generation": 4,
        "session_id": None,
        "bound_at": T0,
    }
    fields.update(overrides)
    return IdentityBinding(**fields)  # type: ignore[arg-type]


def _observation(**overrides: object) -> IdentityObservation:
    fields: dict[str, object] = {
        "presented_token": "tok-secret",
        "presented_generation": 4,
        "presented_session_id": "acp-session-1",
        "now": T0,
    }
    fields.update(overrides)
    return IdentityObservation(**fields)  # type: ignore[arg-type]


def _decide(binding: IdentityBinding, observation: IdentityObservation):
    return decide_identity(binding, observation, defer_bound_s=BOUND_S)


# ------------------------------------------------------------------- bind


def test_a_first_call_with_a_matching_token_binds() -> None:
    decision = _decide(_binding(), _observation())
    assert decision.outcome is IdentityOutcome.BIND
    assert decision.finding is None


def test_an_already_bound_terminal_presenting_the_same_session_stays_bound() -> None:
    binding = _binding(session_id="acp-session-1")
    assert _decide(binding, _observation()).outcome is IdentityOutcome.BIND


# ------------------------------------------------------------------- veto


def test_a_wake_to_a_stale_generation_vetoes_loudly() -> None:
    """The most important arm.

    A wake addressed to a dead incarnation delivered to the live one puts one
    conversation's work into another's context, and nothing downstream can undo
    or even notice that.
    """
    decision = _decide(_binding(), _observation(presented_generation=3))
    assert decision.outcome is IdentityOutcome.VETO
    assert decision.is_permanent
    assert "stale generation" in decision.reason
    assert "3" in decision.reason and "4" in decision.reason


def test_a_wrong_token_vetoes() -> None:
    decision = _decide(_binding(), _observation(presented_token="tok-forged"))
    assert decision.outcome is IdentityOutcome.VETO


def test_a_second_different_session_on_one_generation_is_refused() -> None:
    """Another subprocess wearing the same token.

    The binding is once per generation; a re-bind would hand the token to
    whichever process spoke last, silently.
    """
    binding = _binding(session_id="acp-session-1")
    decision = _decide(binding, _observation(presented_session_id="acp-session-2"))
    assert decision.outcome is IdentityOutcome.VETO
    assert "rebind refused" in decision.reason


def test_a_provably_wrong_claim_is_vetoed_before_absence_is_considered() -> None:
    """Order of checks: withholding a field must not reach the DEFER path.

    The observation below presents a stale generation AND no session id.  If
    absence were considered first it would defer, and a stale incarnation could
    hold a terminal open by simply saying less.
    """
    decision = _decide(
        _binding(), _observation(presented_generation=3, presented_session_id=None)
    )
    assert decision.outcome is IdentityOutcome.VETO


# ------------------------------------------------------------------ defer


def test_an_unbound_terminal_defers_rather_than_vetoing() -> None:
    """The measured bug, inverted.

    A terminal whose identity is not established YET has not failed; its first
    call has not arrived.
    """
    decision = _decide(
        _binding(), _observation(presented_token=None, presented_generation=None, presented_session_id=None)
    )
    assert decision.outcome is IdentityOutcome.DEFER
    assert decision.is_permanent is False
    assert decision.expires_at == T0 + timedelta(seconds=BOUND_S)


def test_the_defer_expires_at_the_bound_and_never_vetoes_forever() -> None:
    """A veto with a reason and a timestamp, not a veto with neither."""
    late = T0 + timedelta(seconds=BOUND_S + 1)
    decision = _decide(
        _binding(),
        _observation(
            presented_token=None,
            presented_generation=None,
            presented_session_id=None,
            now=late,
        ),
    )
    assert decision.outcome is IdentityOutcome.VETO
    assert "past the identity deadline" in decision.reason
    assert decision.expires_at == T0 + timedelta(seconds=BOUND_S)


@pytest.mark.parametrize("elapsed", [0.0, 1.0, BOUND_S - 0.001])
def test_every_instant_before_the_bound_still_defers(elapsed: float) -> None:
    decision = _decide(
        _binding(),
        _observation(
            presented_token=None,
            presented_generation=None,
            presented_session_id=None,
            now=T0 + timedelta(seconds=elapsed),
        ),
    )
    assert decision.outcome is IdentityOutcome.DEFER


def test_the_bound_is_supplied_by_the_caller_not_owned_here() -> None:
    """One opinion about how long a seat may take to come up, and it is not this
    module's."""
    import inspect

    assert "defer_bound_s" in inspect.signature(decide_identity).parameters
    short = decide_identity(
        _binding(),
        _observation(presented_token=None, presented_generation=None, presented_session_id=None),
        defer_bound_s=0.0,
    )
    assert short.outcome is IdentityOutcome.VETO


# -------------------------------------------------- the marker has no vote


def test_a_marker_disagreement_is_counted_not_acted_on() -> None:
    """D22's posture in one assertion.

    Identity is a token CAO issued and bound to an ACP session id, not a third
    party's detection of what kind of agent occupies a pane.  A marker that
    disagrees is evidence ABOUT THE MARKER, so the decision still binds and the
    disagreement is counted.
    """
    decision = _decide(_binding(), _observation(marker_says="some-other-terminal"))
    assert decision.outcome is IdentityOutcome.BIND
    assert decision.finding is FindingCode.DIAG_ACP_IDENTITY_DISAGREE


def test_an_agreeing_marker_counts_nothing() -> None:
    decision = _decide(_binding(), _observation(marker_says="term-a"))
    assert decision.outcome is IdentityOutcome.BIND
    assert decision.finding is None


def test_a_marker_can_never_turn_a_bind_into_a_veto() -> None:
    """The control for the arm above: if a marker could veto, the old authority
    would be back in charge through a side door."""
    for marker in ("something-else", "", "unknown"):
        decision = _decide(_binding(), _observation(marker_says=marker))
        assert decision.outcome is IdentityOutcome.BIND


def test_there_are_exactly_three_outcomes() -> None:
    """Two would re-create the bug: "wrong" and "not yet" need different answers."""
    assert {member.value for member in IdentityOutcome} == {"bind", "veto", "defer"}
