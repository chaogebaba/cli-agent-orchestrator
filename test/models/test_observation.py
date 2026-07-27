"""Acceptance criteria for the F84 observation core.

Every test here corresponds to a ruling in blueprints/f84-observation-core.md.
Where a ruling was earned by a gate finding, the finding id is named -- these
are the assertions that would have died on the defects the gates found.
"""

from __future__ import annotations

import copy
import pickle

import pytest

from cli_agent_orchestrator.models.observation import (
    AggregateSpec,
    Deadline,
    Disproven,
    Observation,
    ProofClass,
    ProofMember,
    Proven,
    SettlementOutcome,
    TaggedProof,
    Unobservable,
    fold,
)

# --------------------------------------------------------------------------
# Adopter fixtures: the three proof enums, declared the way adopters declare
# them. BarrierProof/CoverageProof are the two fold adopters; ProofKind stands
# in for delivery, which BROADCASTS rather than folding (C1).
# --------------------------------------------------------------------------


class ProofKind(TaggedProof):
    TRANSCRIPT_USER_TURN = ("transcript_user_turn", ProofClass.ARRIVAL)
    STATUS_GEN = ("status_gen", ProofClass.LIVENESS)


class BarrierProof(TaggedProof):
    MEMBER_ARRIVED = ("member_arrived", ProofClass.ARRIVAL)


class CoverageProof(TaggedProof):
    ENTRIES_MATCH = ("entries_match", ProofClass.ARRIVAL)


class Complete:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Complete)


class Covered:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Covered)


class MemberAnswer:
    pass


BARRIER_AGGREGATE: AggregateSpec[Complete, BarrierProof, str] = AggregateSpec(
    value=Complete(),
    proven_by=BarrierProof.MEMBER_ARRIVED,
    disproven_reason="member_terminal_gone",
    unobserved_reason="apparatus_unavailable",
    retry_after=Deadline(0.0),
    # DESIGN r18 P1 + R16-E102 + R17-E106: an empty roster is UNREACHABLE on
    # every path in today's source (nothing deletes barrier members;
    # delete_mailbox refuses on an open barrier; the creating transaction
    # attaches the first member). Unobservable is the safe disposition for a
    # state no writer produces -- and it preserves database.py:3690's
    # `if states and all(...)` guard, where Proven(Complete) would have
    # fired an empty barrier.
    empty=Unobservable("apparatus_unavailable", retry_after=Deadline(0.0)),
)

COVERAGE_AGGREGATE: AggregateSpec[Covered, CoverageProof, str] = AggregateSpec(
    value=Covered(),
    proven_by=CoverageProof.ENTRIES_MATCH,
    disproven_reason="entry_mismatch",
    unobserved_reason="apparatus_unavailable",
    retry_after=Deadline(0.0),
    # REACHABLE and different from the barrier's: _same_entries((), ()) is True
    # at source, so an empty delta really is covered. The two adopters do NOT
    # share a verdict -- ruling them separately is what surfaced that.
    empty=Proven(Covered(), by=CoverageProof.ENTRIES_MATCH),
)


# --------------------------------------------------------------------------
# AC0 -- X1: an Observation has no truth value (R13-E81 gave it a carrier)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "observation",
    [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED),
        Disproven("member_terminal_gone"),
        Unobservable("apparatus_unavailable", retry_after=Deadline(0.0)),
    ],
    ids=["proven", "disproven", "unobservable"],
)
def test_ac0_observation_has_no_truth_value(observation: object) -> None:
    """All THREE variants raise -- the rule is general, not adopter-local.

    Mutant: return False instead of raising -> `if not obs` silently takes the
    wrong branch forever and this dies.
    """
    with pytest.raises(TypeError, match="no truth value"):
        bool(observation)


def test_ac0_covers_the_idiom_that_actually_bites() -> None:
    """`if not predicate(...)` is the real-world form of the trap."""
    obs = Unobservable("apparatus_unavailable", retry_after=Deadline(0.0))
    with pytest.raises(TypeError):
        if not obs:  # noqa: SIM103 - this IS the assertion
            pass


# --------------------------------------------------------------------------
# AC9 -- proof representation (R12-E75 bound, R13v28-E79 fake member,
#        R14-E88 single declaration, R14-E89 exact mapping)
# --------------------------------------------------------------------------


def test_ac9a_tag_is_real_state_not_a_fake_enum_member() -> None:
    """R13v28-E79: `proof_class = X` in the body became a MEMBER of the enum.

    Mutant: declare the tag as a class attribute -> it appears in list(Enum)
    and its type is the adopter enum rather than ProofClass; both die here.
    """
    assert list(ProofKind) == [ProofKind.TRANSCRIPT_USER_TURN, ProofKind.STATUS_GEN]
    assert all(isinstance(m.proof_class, ProofClass) for m in ProofKind)
    assert "proof_class" not in {m.name.lower() for m in ProofKind}


def test_ac9a_string_values_are_preserved() -> None:
    """StrEnum members ARE their strings, so no durable migration is needed."""
    assert ProofKind.TRANSCRIPT_USER_TURN == "transcript_user_turn"
    assert BarrierProof.MEMBER_ARRIVED == "member_arrived"
    assert f"{CoverageProof.ENTRIES_MATCH}" == "entries_match"


def test_ac9a_one_enum_can_carry_mixed_tags() -> None:
    """The tag is per-MEMBER, not per-enum -- delivery has both classes."""
    assert ProofKind.TRANSCRIPT_USER_TURN.proof_class is ProofClass.ARRIVAL
    assert ProofKind.STATUS_GEN.proof_class is ProofClass.LIVENESS


def test_ac9a_all_three_adopter_enums_satisfy_the_bound() -> None:
    for member in (
        ProofKind.TRANSCRIPT_USER_TURN,
        BarrierProof.MEMBER_ARRIVED,
        CoverageProof.ENTRIES_MATCH,
    ):
        assert isinstance(member, ProofMember)


def test_ac9b_liveness_proof_cannot_establish_proven() -> None:
    """A liveness signal is not evidence of arrival -- refuse at construction.

    R14-E89: the BOUND cannot catch this (a mis-tagged member satisfies the
    protocol), so the constructor must.
    """
    with pytest.raises(ValueError, match="only ARRIVAL-class"):
        Proven(MemberAnswer(), by=ProofKind.STATUS_GEN)


def test_ac9b_inv_exact_member_to_class_map() -> None:
    """R14-E89's amendment: assert the exact map AND member-set equality.

    Mutant A: add a member -> the member-set assertion dies.
    Mutant B: mis-tag an existing member -> the mapping assertion dies.
    They must fail for DIFFERENT reasons, or one masks the other.
    """
    expected = {
        "transcript_user_turn": ProofClass.ARRIVAL,
        "status_gen": ProofClass.LIVENESS,
    }
    assert {m.value for m in ProofKind} == set(expected)
    assert {m.value: m.proof_class for m in ProofKind} == expected


# --------------------------------------------------------------------------
# AC1b -- the fold law, graded INSIDE fold() (R12-E76, R13-E81's scope bound)
# --------------------------------------------------------------------------


def test_ac1b_unobservable_component_never_yields_disproven() -> None:
    """THE core universal. 'We could not look' must not become 'it was false'.

    Mutant: return the aggregate's disproven_reason when a component is
    Unobservable -> this dies. That mutant is a real product change; the old
    'add a third FoldLaw member' mutant is unwritable now that the enum is gone.
    """
    components: list[Observation[MemberAnswer, BarrierProof, str]] = [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED),
        Unobservable("apparatus_unavailable", retry_after=Deadline(0.0)),
        Disproven("member_terminal_gone"),
    ]
    result = fold(components, BARRIER_AGGREGATE)
    assert isinstance(result, Unobservable)
    assert result.reason == "apparatus_unavailable"


def test_ac1b_meet_takes_the_weakest_verdict_present() -> None:
    proven: Observation[MemberAnswer, BarrierProof, str] = Proven(
        MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED
    )
    disproven: Observation[MemberAnswer, BarrierProof, str] = Disproven("member_terminal_gone")

    assert isinstance(fold([proven, proven], BARRIER_AGGREGATE), Proven)
    assert isinstance(fold([proven, disproven], BARRIER_AGGREGATE), Disproven)


def test_ac1b_fold_changes_the_subject() -> None:
    """X3: components answer about MEMBERS, the aggregate about the BARRIER.

    R15v32-E94: the signature used to share one type triple across both, which
    asserted the subjects are the same -- the exact claim X3 denies.
    """
    components: list[Observation[MemberAnswer, BarrierProof, str]] = [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED)
    ]
    result = fold(components, BARRIER_AGGREGATE)
    assert isinstance(result, Proven)
    assert result.value == Complete()  # not MemberAnswer


def test_ac7a_barrier_fires_with_a_failed_member_but_is_not_proven() -> None:
    """R13v28-E80: FIRES is not PROVEN.

    A roster with no AWAITING closes, whatever mix it holds -- but a FAILED
    member maps to Unobservable, so the barrier's COMPLETENESS is honestly
    unproven. close_reason already records `complete_partial` for this case;
    the durable vocabulary was ahead of the law.
    """
    roster: list[Observation[MemberAnswer, BarrierProof, str]] = [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED),
        Unobservable("apparatus_unavailable", retry_after=Deadline(0.0)),
    ]
    verdict = fold(roster, BARRIER_AGGREGATE)
    assert isinstance(verdict, Unobservable)


def test_ac8b_coverage_precedence_both_directions() -> None:
    """An acquisition failure AND a genuine mismatch settles Unobservable."""
    components: list[Observation[MemberAnswer, CoverageProof, str]] = [
        Disproven("entry_mismatch"),
        Unobservable("apparatus_unavailable", retry_after=Deadline(0.0)),
    ]
    assert isinstance(fold(components, COVERAGE_AGGREGATE), Unobservable)
    assert isinstance(fold(list(reversed(components)), COVERAGE_AGGREGATE), Unobservable)


# --------------------------------------------------------------------------
# The empty dispositions -- ruled SEPARATELY per adopter, and they DIFFER
# --------------------------------------------------------------------------


def test_empty_barrier_is_unobservable_not_proven() -> None:
    """DESIGN r18 P1, converged on by three gates independently.

    Mutant: empty=Proven(Complete) -> this dies. That value would fire any
    barrier opened before its members were attached, and it contradicts the
    live `if states and all(...)` guard, under which an empty roster does NOT
    fire.
    """
    result = fold([], BARRIER_AGGREGATE)
    assert isinstance(result, Unobservable)
    assert result.reason == "apparatus_unavailable"


def test_empty_coverage_is_proven() -> None:
    """An empty delta is covered by any artifact -- nothing to be stale about.

    The opposite ruling would make every clean tree unobservable.
    """
    result = fold([], COVERAGE_AGGREGATE)
    assert isinstance(result, Proven)


def test_the_two_adopters_do_not_share_an_empty_verdict() -> None:
    """v35 claimed they happened to agree. They do not -- this pins that."""
    assert type(fold([], BARRIER_AGGREGATE)) is not type(fold([], COVERAGE_AGGREGATE))


# --------------------------------------------------------------------------
# SettlementOutcome -- R18-E110
# --------------------------------------------------------------------------


def test_settlement_outcome_falsiness_is_a_mechanism() -> None:
    """R18-E110: as three Literal strings, ALL THREE were truthy.

    A caller writing `if not settle(...)` would have read a failed CAS
    requiring retry as success.
    """
    assert bool(SettlementOutcome.SETTLED) is True
    assert bool(SettlementOutcome.STALE) is False
    assert bool(SettlementOutcome.RECOVERY) is False
    assert not SettlementOutcome.STALE


def test_settlement_outcome_keeps_its_persisted_spellings() -> None:
    """Falsy, and still a real str -- the durable values are unchanged."""
    assert SettlementOutcome.STALE == "stale"
    assert SettlementOutcome.RECOVERY == "settlement_pending_recovery"
    assert isinstance(SettlementOutcome.SETTLED, str)


def test_stale_and_recovery_are_distinct_answers() -> None:
    """R17-E103: collapsing them loses the retry-required signal, and breaks
    test_f44_probable_delivered.py:432-464 which asserts exactly "stale"."""
    assert SettlementOutcome.STALE is not SettlementOutcome.RECOVERY
    assert not SettlementOutcome.STALE and not SettlementOutcome.RECOVERY


# --------------------------------------------------------------------------
# Structural properties the core relies on
# --------------------------------------------------------------------------


def test_observations_are_frozen() -> None:
    obs = Disproven("member_terminal_gone")
    with pytest.raises(Exception):
        obs.reason = "something_else"  # type: ignore[misc]


def test_observations_survive_copy_and_pickle() -> None:
    """PV3 dropped the nominal carrier, so an Observation is an ordinary frozen
    dataclass -- copy and pickle are sound and nothing needs to police them."""
    obs = Disproven("member_terminal_gone")
    assert copy.copy(obs) == obs
    assert pickle.loads(pickle.dumps(obs)) == obs
