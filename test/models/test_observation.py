"""Acceptance criteria for the F84 observation core.

Every test here corresponds to a ruling in blueprints/f84-observation-core.md.
Where a ruling was earned by a gate finding, the finding id is named -- these
are the assertions that would have died on the defects the gates found.
"""

from __future__ import annotations

import copy
import os
import pathlib
import pickle
import textwrap
from enum import Enum
from typing import Literal

import pytest

from cli_agent_orchestrator.models import observation
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
    TaggedProofMeta,
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


# EMPIRICAL r2 + user ruling 2026-07-27: the ARRIVAL-only TYPE for the one
# adopter whose vocabulary is mixed. Measured (probes/f84-typebound-2026-07-27):
# mypy types an enum member CONSTANT as `Literal[Enum.MEMBER]`, so a union of
# the arrival members rejects `STATUS_GEN` as an `arg-type` error at every
# ANNOTATED site -- including through the real `Proven[T, P, R]` generic.
#
# This is X2's ruling ("a bad value must fail to type-check, not fail review")
# applied to PROOFS, where it had only ever been applied to reasons. It does
# NOT replace the runtime seal: measured type-ONLY, since a member arriving
# through `Any`, `cast`, or deserialization builds a liveness `Proven` with
# mypy silent. Two layers, two different boundaries.
ArrivalProofKind = Literal[ProofKind.TRANSCRIPT_USER_TURN]


class BarrierProof(TaggedProof):
    MEMBER_ARRIVED = ("member_arrived", ProofClass.ARRIVAL)
    # A SECOND arrival-class member exists so a component's proof can DIFFER
    # from the aggregate spec's. Without it the two are the same value and the
    # "aggregate proof never escapes from a component" test cannot fail --
    # B2's vacuity pattern, which I reproduced in the test written to fix it.
    MEMBER_ARRIVED_LATE = ("member_arrived_late", ProofClass.ARRIVAL)


class CoverageProof(TaggedProof):
    ENTRIES_MATCH = ("entries_match", ProofClass.ARRIVAL)


# B1 (DESIGN r20) -- X2 is the fork the USER ruled: `R` must be CLOSED, so that
# "a reason outside the adopter's enum fails to TYPE-CHECK rather than failing
# review". It was the one ruling that did not survive transcription: `R` was
# instantiated at `str` in both fixtures, which is D10's free-string defect
# demonstrated inside the type D10 exists to protect. `R` carries no TypeVar
# bound BY DESIGN (a `Literal` union cannot be a bound); closure is enforced
# HERE, at instantiation, which is the only place it can be.
#
# Component and aggregate reasons are DIFFERENT types because X3 says a fold
# changes the SUBJECT: a member is not a roster, an entry is not a delta.
# `fold()`'s CR/AR type parameters were already separate; these give that split
# a representation. It also makes B2's "borrow the component's reason" mutant a
# TYPE error as well as a test failure.
MemberReason = Literal["member_terminal_gone", "member_unobservable"]
BarrierReason = Literal["roster_member_gone", "roster_unobservable"]
EntryReason = Literal["entry_mismatch", "entry_unobservable"]
CoverageReason = Literal["delta_entry_mismatch", "delta_unobservable"]


class Complete:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Complete)


class Covered:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Covered)


class MemberAnswer:
    pass


# S3 (DESIGN r20): the deadlines are DISTINCT and NON-ZERO. `Deadline(0.0)` is
# the epoch -- "retry immediately" -- while §5.3 rules the freshness deadline IS
# `now`. These fixtures are the worked example a PV2 builder copies, so a
# semantically wrong value here propagates. Distinct values also let a test
# prove `fold` forwards the SPEC's trigger rather than fabricating one.
BARRIER_RETRY = Deadline(120.0)
COVERAGE_RETRY = Deadline(300.0)

BARRIER_AGGREGATE: AggregateSpec[Complete, BarrierProof, BarrierReason] = AggregateSpec(
    value=Complete(),
    proven_by=BarrierProof.MEMBER_ARRIVED,
    # B2 (DESIGN r20): these MUST differ from every component reason. They were
    # byte-identical, so `fold`'s constructors were graded only on TYPE and a
    # mutant returning `seen[0].reason` -- X3's exact prohibition -- survived all
    # 23 tests. Verified: the mutant passed 23/23 before this change.
    disproven_reason="roster_member_gone",
    unobserved_reason="roster_unobservable",
    retry_after=BARRIER_RETRY,
    # DESIGN r18 P1 + R16-E102 + R17-E106: an empty roster is UNREACHABLE on
    # every path in today's source (nothing deletes barrier members;
    # delete_mailbox refuses on an open barrier; the creating transaction
    # attaches the first member). Unobservable is the safe disposition for a
    # state no writer produces -- and it preserves database.py:3690's
    # `if states and all(...)` guard, where Proven(Complete) would have
    # fired an empty barrier.
    empty=Unobservable("roster_unobservable", retry_after=BARRIER_RETRY),
)

# ⚠ EMPIRICAL r2: the digest ADOPTER no longer folds -- it selects over an
# ordered cause list, because one `unobserved_reason` cannot carry both
# `apparatus_unavailable` and `unhashable_entry`. This spec stays because it is
# a SECOND spec for grading `fold()` itself: it proves the `empty` disposition
# is per-adopter and not a property of the function. Do not read it as the
# digest seat's contract.
COVERAGE_AGGREGATE: AggregateSpec[Covered, CoverageProof, CoverageReason] = AggregateSpec(
    value=Covered(),
    proven_by=CoverageProof.ENTRIES_MATCH,
    disproven_reason="delta_entry_mismatch",
    unobserved_reason="delta_unobservable",
    retry_after=COVERAGE_RETRY,
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
        Unobservable("member_unobservable", retry_after=Deadline(60.0)),
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
    obs: Observation[MemberAnswer, BarrierProof, MemberReason] = Unobservable(
        "member_unobservable", retry_after=Deadline(60.0)
    )
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
    """Members ARE their strings, so no durable migration is needed.

    EMPIRICAL r1-code: this asserted `f"{CoverageProof.ENTRIES_MATCH}"`, which
    is NOT stable across the two spellings of a str-enum -- interpolation
    yields the value under `StrEnum` and `"CoverageProof.ENTRIES_MATCH"` under
    the `(str, Enum)` mixin this module uses. (It was found as a version skew:
    the same split runs along 3.10 vs 3.11+ for the mixin, which is how the
    assertion passed on the then-floor and failed above it.) `==` and `.value`
    are stable under both spellings, so durable comparisons use those; the
    interpolation form is asserted NOT to be relied on.
    """
    assert ProofKind.TRANSCRIPT_USER_TURN == "transcript_user_turn"
    assert BarrierProof.MEMBER_ARRIVED == "member_arrived"
    assert CoverageProof.ENTRIES_MATCH == "entries_match"
    assert CoverageProof.ENTRIES_MATCH.value == "entries_match"
    # What actually gets persisted/compared: the value, never the repr.
    assert {m.value for m in ProofKind} == {"transcript_user_turn", "status_gen"}


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


def test_ac9b_a_property_override_cannot_forge_admission() -> None:
    """EMPIRICAL r1-code B2: the guard read the OVERRIDABLE property, so a
    subclass returning ARRIVAL from `proof_class` produced a Proven from a
    LIVENESS member. Admission now reads the tag SEALED at member creation.
    """

    class ForgedProof(TaggedProof):
        MEMBER = ("forged", ProofClass.LIVENESS)

        @property
        def proof_class(self) -> ProofClass:
            return ProofClass.ARRIVAL

    assert ForgedProof.MEMBER.proof_class is ProofClass.ARRIVAL  # the lie reads true
    with pytest.raises(ValueError, match="only ARRIVAL-class"):
        Proven(MemberAnswer(), by=ForgedProof.MEMBER)


def test_ac9b_mutating_the_stored_tag_cannot_forge_admission() -> None:
    """EMPIRICAL r1-code B2, second route: `_proof_class` is ordinary instance
    state, so plain assignment rewrote it before construction.
    """

    class MutableProof(TaggedProof):
        MEMBER = ("mutable", ProofClass.LIVENESS)

    MutableProof.MEMBER._proof_class = ProofClass.ARRIVAL
    # r3 B1: the seal still reads LIVENESS, so the DISAGREEMENT is now the
    # finding -- a better message than "wrong class", because it names the
    # tampering rather than only its effect.
    with pytest.raises(ValueError, match="disagrees with its declaration"):
        Proven(MemberAnswer(), by=MutableProof.MEMBER)


def test_ac9b_a_member_with_no_sealed_tag_is_refused() -> None:
    """A hand-rolled object that satisfies the PROTOCOL has no admission
    record, so it cannot establish Proven -- structural typing is not
    authorization."""

    class Impostor:
        @property
        def proof_class(self) -> ProofClass:
            return ProofClass.ARRIVAL

    assert isinstance(Impostor(), ProofMember)  # satisfies the bound
    # EMPIRICAL r2: refused one check EARLIER than it used to be. It is not a
    # member of any proof enum, so admission stops before any tag is read --
    # reading a tag off an object of unknown provenance is the mistake.
    # r3 B1: refused a step earlier still -- its metaclass is `type`, so it was
    # never declared by TaggedProofMeta at all.
    with pytest.raises(ValueError, match="metaclass is type"):
        Proven(MemberAnswer(), by=Impostor())


def test_ac9c_an_alias_cannot_be_declared() -> None:
    """EMPIRICAL r2 route 3, now refused where it belongs -- at DECLARATION.

    A duplicate value makes the second name an ALIAS of the first member, so
    the second name's declared proof class is silently discarded and reading
    `AliasProof.LIVENESS_NAME.proof_class` returns the FIRST member's tag. The
    r2 probe used exactly this to admit a liveness name. It also left a stale
    `id()` in the old registry, which is what made accidental reuse possible.
    """
    with pytest.raises(ValueError, match="aliases"):

        class AliasProof(TaggedProof):
            LIVENESS_NAME = ("same-value", ProofClass.LIVENESS)
            ARRIVAL_NAME = ("same-value", ProofClass.ARRIVAL)


def test_ac9c_a_malformed_tag_cannot_be_declared() -> None:
    """The R09 mutant's opening, closed at declaration.

    R09 survived all 34 tests: the guard asked "is the tag LIVENESS?" and so
    admitted a tag that was NEITHER class. Rewriting it as "is the tag
    ARRIVAL?" closes it at admission -- but the declaration was already wrong,
    and refusing it here means no admission-time spelling can matter.
    """
    with pytest.raises(TypeError, match="is not a ProofClass"):

        class MalformedProof(TaggedProof):
            BAD = ("bad", "not-a-proof-class")


def test_ac9c_a_property_override_cannot_reach_the_sealed_tag() -> None:
    """EMPIRICAL r2 route 1 against the NAME-keyed seal.

    The tester's proposed backstop still admitted this, because it read the tag
    back through the member's own class. The seal is keyed by (class, name) and
    written from the DECLARATION, so an override changes what a reader sees and
    not what admission consults.
    """

    class OverriddenProof(TaggedProof):
        LIVE = ("overridden-live", ProofClass.LIVENESS)

        @property
        def proof_class(self) -> ProofClass:
            return ProofClass.ARRIVAL

    assert OverriddenProof.LIVE.proof_class is ProofClass.ARRIVAL  # the LIE
    with pytest.raises(ValueError, match="only ARRIVAL-class"):
        Proven(MemberAnswer(), by=OverriddenProof.LIVE)


def test_ac9c_mutating_proof_class_cannot_reach_the_sealed_tag() -> None:
    """EMPIRICAL r1-code B2's route, re-verified against the new key."""

    class MutableProof(TaggedProof):
        LIVE = ("mutable-live", ProofClass.LIVENESS)

    MutableProof.LIVE._proof_class = ProofClass.ARRIVAL  # type: ignore[misc]
    assert MutableProof.LIVE.proof_class is ProofClass.ARRIVAL  # the LIE
    # r3 B1: now refused as TAMPER-EVIDENT rather than merely out-classed --
    # the seal still says LIVENESS, so the disagreement itself is the finding.
    with pytest.raises(ValueError, match="disagrees with its declaration"):
        Proven(MemberAnswer(), by=MutableProof.LIVE)


def test_ac9c_a_planted_sealed_value_is_refused() -> None:
    """The one route left once the KEY cannot be forged: forge the VALUE.

    The metaclass refuses a malformed tag at declaration, so the only way to
    reach a non-ProofClass sealed value is to write the module-private dict
    directly. Verified reachable at the interpreter, hence checked rather than
    trusted. This is R09's shape one layer down: a value that is NEITHER class
    passes any guard that merely asks whether it is the wrong one.
    """

    class PlantedProof(TaggedProof):
        LIVE = ("planted", ProofClass.LIVENESS)

    # Declaration rewritten to match, so the isinstance branch decides rather
    # than tamper-evidence (r3 B1).
    observation._SEALED_PROOF_CLASS[(PlantedProof, "LIVE")] = "forged"  # type: ignore[index]
    object.__setattr__(PlantedProof.LIVE, "_proof_class", "forged")
    try:
        with pytest.raises(ValueError, match="not a ProofClass"):
            Proven(MemberAnswer(), by=PlantedProof.LIVE)
    finally:
        observation._SEALED_PROOF_CLASS.pop((PlantedProof, "LIVE"), None)


def test_ac9c_admission_admits_only_arrival_it_does_not_refuse_liveness() -> None:
    """Mutant R09, which survived all 34 tests of the previous suite.

    Writing the guard as `if sealed is ProofClass.LIVENESS: raise` is right for
    every tag that IS one of the two classes, so no ordinary fixture separates
    it from `if sealed is not ProofClass.ARRIVAL: raise`. They differ only on a
    tag that is NEITHER -- and the previous suite had no way to produce one.

    This drives `Proven` DIRECTLY (not `_sealed_proof_class`, which now raises
    first and would mask the branch under test) with a sealed value that is a
    valid `ProofClass`-typed object of neither class. Under the mutant the
    admission passes and a `Proven` is BUILT; under the correct spelling it is
    refused.

    **A guard that enumerates what to REFUSE is open by default. Only one that
    enumerates what to ADMIT is closed.**
    """

    class R09Proof(TaggedProof):
        LIVE = ("r09-live", ProofClass.LIVENESS)

    # A genuine ProofClass INSTANCE that is neither member: subclassing an enum
    # with members is forbidden, but `__new__` on the mixin type produces an
    # object `isinstance`-compatible with ProofClass and identical to neither
    # member. `isinstance` therefore CANNOT be what refuses this -- only the
    # admit-only-ARRIVAL comparison can. That is what makes this test separate
    # the two spellings rather than pass for an unrelated reason.
    third = str.__new__(ProofClass, "hearsay")
    object.__setattr__(third, "_name_", "HEARSAY")
    object.__setattr__(third, "_value_", "hearsay")
    assert isinstance(third, ProofClass)
    assert third is not ProofClass.ARRIVAL and third is not ProofClass.LIVENESS
    # And the real enum is untouched -- this constructs, it does not mutate.
    assert [m.name for m in ProofClass] == ["ARRIVAL", "LIVENESS"]

    # Both the seal AND the declaration are set, so the tamper-evidence check
    # (r3 B1) agrees and the ADMISSION branch is what decides -- otherwise this
    # test passes on tamper-evidence and R09 goes unreachable for the third
    # time. Models a member whose declared class is genuinely the third one.
    observation._SEALED_PROOF_CLASS[(R09Proof, "LIVE")] = third
    object.__setattr__(R09Proof.LIVE, "_proof_class", third)
    try:
        with pytest.raises(ValueError, match="only ARRIVAL-class"):
            Proven(MemberAnswer(), by=R09Proof.LIVE)
    finally:
        observation._SEALED_PROOF_CLASS.pop((R09Proof, "LIVE"), None)


def test_ac9c_a_non_canonical_member_is_refused() -> None:
    """The canonical-membership check, exercised by something that reaches it.

    The metaclass refuses a declared alias, so an alias cannot arrive by the
    front door. It can still arrive by `_value2member_map_`, which is how the
    Enum machinery resolves a lookup -- and an object bound there is NOT the
    member its own name publishes. Admission must compare identity against
    `__members__`, not merely find A tag.
    """

    class CanonProof(TaggedProof):
        REAL = ("canon-real", ProofClass.ARRIVAL)

    # The object must be of a type that HAS `__members__` and publishes this
    # name -- otherwise the earlier "not a member of a proof enum" check
    # refuses it and the identity comparison is never reached. A second
    # instance of the member's own class satisfies the lookup and fails only
    # the identity test, which is precisely the branch under test.
    impostor = str.__new__(CanonProof, "canon-real")
    object.__setattr__(impostor, "_name_", "REAL")
    object.__setattr__(impostor, "_value_", "canon-real")

    assert type(impostor).__members__["REAL"] is not impostor  # lookup succeeds
    assert impostor == CanonProof.REAL  # equal by value, NOT the same object
    with pytest.raises(ValueError, match="canonical"):
        Proven(MemberAnswer(), by=impostor)  # type: ignore[arg-type]


def test_ac9c_the_seal_is_not_keyed_on_a_recyclable_identity() -> None:
    """The r2 finding that needed no adversary: `id()` is REUSED.

    An aliased member left a stale `id()` key, and an ordinary allocation
    landed on that freed address in ONE iteration -- inheriting a sealed
    ARRIVAL tag without anything malicious happening. This asserts the property
    that makes that impossible: every key names a live class and one of its
    own member names, so nothing the allocator does can forge a key.
    """
    for owner, member_name in observation._SEALED_PROOF_CLASS:
        assert isinstance(owner, type)
        assert member_name in owner.__members__  # type: ignore[attr-defined]
        assert owner.__members__[member_name].name == member_name  # type: ignore[attr-defined]


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
    components: list[Observation[MemberAnswer, BarrierProof, MemberReason]] = [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED),
        Unobservable("member_unobservable", retry_after=Deadline(60.0)),
        Disproven("member_terminal_gone"),
    ]
    result = fold(components, BARRIER_AGGREGATE)
    assert isinstance(result, Unobservable)
    # The aggregate's OWN reason -- never a component's. Both component reasons
    # present here ("member_terminal_gone", "member_unobservable") are distinct
    # from it, so a `fold` that borrowed `seen[0].reason` dies on this line.
    assert result.reason == "roster_unobservable"
    assert result.retry_after == BARRIER_RETRY


def test_ac1b_fold_accepts_a_ONE_SHOT_iterable() -> None:
    """EMPIRICAL r1-code B3: `seen = list(components)` -> `seen = components`
    passed 23/23, but a GENERATOR is consumed by the first `any()`, so
    [Proven, Disproven] returned Proven -- the meet silently inverted. The
    signature says `Iterable`, so a one-shot iterator must be honoured.
    """
    components = (
        c
        for c in (
            Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED),
            Disproven("member_terminal_gone"),
        )
    )
    result = fold(components, BARRIER_AGGREGATE)
    assert isinstance(result, Disproven)
    assert result.reason == "roster_member_gone"


def test_ac1b_aggregate_proof_never_escapes_from_a_component() -> None:
    """EMPIRICAL r1-code B3: returning the COMPONENT's proof for the aggregate
    passed 23/23. X3 says the aggregate is a different subject, so its proof
    must come from the spec -- a component's proof is about a member.
    """
    other = BarrierProof.MEMBER_ARRIVED_LATE
    assert other is not BARRIER_AGGREGATE.proven_by  # or this test is vacuous
    components: list[Observation[MemberAnswer, BarrierProof, MemberReason]] = [
        Proven(MemberAnswer(), by=other),
        Proven(MemberAnswer(), by=other),
    ]
    result = fold(components, BARRIER_AGGREGATE)
    assert isinstance(result, Proven)
    assert result.by is BARRIER_AGGREGATE.proven_by
    assert result.value is BARRIER_AGGREGATE.value


def test_ac1b_meet_takes_the_weakest_verdict_present() -> None:
    proven: Observation[MemberAnswer, BarrierProof, MemberReason] = Proven(
        MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED
    )
    disproven: Observation[MemberAnswer, BarrierProof, MemberReason] = Disproven(
        "member_terminal_gone"
    )

    won = fold([proven, proven], BARRIER_AGGREGATE)
    assert isinstance(won, Proven)
    assert won.by is BarrierProof.MEMBER_ARRIVED

    lost = fold([proven, disproven], BARRIER_AGGREGATE)
    assert isinstance(lost, Disproven)
    # Disproven borrows too, if allowed to: assert the aggregate's reason.
    assert lost.reason == "roster_member_gone"


def test_ac1b_fold_changes_the_subject() -> None:
    """X3: components answer about MEMBERS, the aggregate about the BARRIER.

    R15v32-E94: the signature used to share one type triple across both, which
    asserted the subjects are the same -- the exact claim X3 denies.
    """
    components: list[Observation[MemberAnswer, BarrierProof, MemberReason]] = [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED)
    ]
    result = fold(components, BARRIER_AGGREGATE)
    assert isinstance(result, Proven)
    assert result.value == Complete()  # not MemberAnswer
    # The PROOF changes subject too: the aggregate is proven by the spec's
    # member, not by whatever proved any single component.
    assert result.by is BarrierProof.MEMBER_ARRIVED


def test_ac7a_failed_member_yields_unobservable_not_disproven() -> None:
    """R13v28-E80: FIRES is not PROVEN.

    A roster with no AWAITING closes, whatever mix it holds -- but a FAILED
    member maps to Unobservable, so the barrier's COMPLETENESS is honestly
    unproven. close_reason already records `complete_partial` for this case;
    the durable vocabulary was ahead of the law.
    """
    roster: list[Observation[MemberAnswer, BarrierProof, MemberReason]] = [
        Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED),
        Unobservable("member_unobservable", retry_after=Deadline(60.0)),
    ]
    verdict = fold(roster, BARRIER_AGGREGATE)
    assert isinstance(verdict, Unobservable)


def test_ac8b_coverage_precedence_both_directions() -> None:
    """An acquisition failure AND a genuine mismatch settles Unobservable."""
    components: list[Observation[MemberAnswer, CoverageProof, EntryReason]] = [
        Disproven("entry_mismatch"),
        Unobservable("entry_unobservable", retry_after=Deadline(60.0)),
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
    assert result.reason == "roster_unobservable"
    assert result.retry_after == BARRIER_RETRY


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
    """Falsy, and still a real str -- the durable values are unchanged.

    EMPIRICAL r1-code B4: this asserted only STALE and RECOVERY, so changing
    SETTLED's DURABLE value from "settled" to "done" passed 23/23. A persisted
    token is a durable contract; assert the exact member->value map and the
    member SET, so both a changed spelling and an added member die -- and for
    different reasons, or one masks the other.
    """
    assert {m.name: m.value for m in SettlementOutcome} == {
        "SETTLED": "settled",
        "STALE": "stale",
        "RECOVERY": "settlement_pending_recovery",
    }
    assert isinstance(SettlementOutcome.SETTLED, str)


def test_proof_class_is_a_closed_set() -> None:
    """EMPIRICAL r1-code B4: adding `ProofClass.HEARSAY` passed all 23 tests.

    ProofClass is an ADMISSION vocabulary -- `Proven.__post_init__` authorizes
    on it -- so a new member silently widens what can establish proof. Closed
    sets are pinned by their whole membership, never by sampling.
    """
    assert {m.name: m.value for m in ProofClass} == {
        "ARRIVAL": "arrival",
        "LIVENESS": "liveness",
    }


def test_stale_and_recovery_are_distinct_answers() -> None:
    """R17-E103: collapsing them loses the retry-required signal, and breaks
    test_f44_probable_delivered.py:432-464 which asserts exactly "stale"."""
    assert SettlementOutcome.STALE is not SettlementOutcome.RECOVERY
    assert not SettlementOutcome.STALE and not SettlementOutcome.RECOVERY


# --------------------------------------------------------------------------
# Structural properties the core relies on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "field"),
    [
        (Proven(MemberAnswer(), by=BarrierProof.MEMBER_ARRIVED), "value"),
        (Disproven("member_terminal_gone"), "reason"),
        (Unobservable("member_unobservable", retry_after=Deadline(60.0)), "reason"),
        (BARRIER_AGGREGATE, "value"),
        (Deadline(60.0), "at"),
    ],
    ids=["proven", "disproven", "unobservable", "aggregate_spec", "deadline"],
)
def test_every_carrier_is_frozen(observation: object, field: str) -> None:
    """EMPIRICAL r1-code S1: only `Disproven` was checked, despite the plural
    name -- so removing `frozen=True` from Proven, Unobservable, AggregateSpec
    or Deadline INDEPENDENTLY left all 23 green and allowed runtime mutation.
    """
    with pytest.raises(Exception):
        setattr(observation, field, "mutated")


def test_observations_are_frozen() -> None:
    # Annotated at the VARIANT, not the union: the assignment target has to be
    # a real attribute for the frozen-ness to be what fails.
    obs: Disproven[MemberAnswer, BarrierProof, MemberReason] = Disproven("member_terminal_gone")
    with pytest.raises(Exception):
        obs.reason = "member_unobservable"  # type: ignore[misc]


def test_observations_survive_copy_and_pickle() -> None:
    """PV3 dropped the nominal carrier, so an Observation is an ordinary frozen
    dataclass -- copy and pickle are sound and nothing needs to police them."""
    obs: Observation[MemberAnswer, BarrierProof, MemberReason] = Disproven("member_terminal_gone")
    assert copy.copy(obs) == obs
    assert pickle.loads(pickle.dumps(obs)) == obs


def test_ac9d_the_type_checker_rejects_a_liveness_member() -> None:
    """The STATIC half of admission (user ruling 2026-07-27).

    The runtime seal refuses a liveness member when the code RUNS. This asserts
    the other half: annotated against `ArrivalProofKind`, a liveness member is
    an error mypy reports without executing anything.

    Asserted by running mypy, because a type-prevention claim is empirical --
    GOLDEN-TIPS, ed1c0c1. A comment saying "the type checker prevents this" is
    a wish; this is the measurement.
    """
    import subprocess
    import sys
    import tempfile

    source = textwrap.dedent("""
        from typing import Literal
        from cli_agent_orchestrator.models.observation import (
            ProofClass, Proven, TaggedProof,
        )

        class Kind(TaggedProof):
            ARRIVED = ("arrived", ProofClass.ARRIVAL)
            LIVE = ("live", ProofClass.LIVENESS)

        ArrivalOnly = Literal[Kind.ARRIVED]

        class Subject: ...

        ok: Proven[Subject, ArrivalOnly, str] = Proven(Subject(), by=Kind.ARRIVED)
        bad: Proven[Subject, ArrivalOnly, str] = Proven(Subject(), by=Kind.LIVE)
        """)
    with tempfile.TemporaryDirectory() as tmp:
        probe = pathlib.Path(tmp) / "probe.py"
        probe.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--strict", "--no-site-packages", str(probe)],
            capture_output=True,
            text=True,
            env={**os.environ, "MYPYPATH": str(pathlib.Path(__file__).parents[2] / "src")},
        )

    out = result.stdout
    # Asserted on CONTENT, not line numbers: the probe is a dedented literal and
    # a line-number assertion breaks on any edit to it while still "passing" for
    # the wrong reasons if the offsets happen to line up.
    assert "arg-type" in out, f"expected an arg-type error, got:\n{out}"
    assert "Literal[Kind.LIVE]" in out, f"the LIVENESS member was not rejected:\n{out}"
    assert "Literal[Kind.ARRIVED]" in out, f"expected the arrival type as expected:\n{out}"
    assert "Found 1 error" in out, f"exactly one error expected; got:\n{out}"


def test_ac9e_a_metaclass_subclass_cannot_declare_a_proof_enum() -> None:
    """EMPIRICAL r3 B1, route 2: `super().__new__` then rewrite the seal.

    A `TaggedProofMeta` SUBCLASS gets the base metaclass to seal the correct
    tags and then overwrites them -- and, separately, can shadow `__members__`
    so the canonical-membership check reads a map it supplies. Both are ordinary
    Python, not exotic. Requiring the EXACT metaclass closes them together, and
    closes future variations without enumerating them.
    """

    class Extended(TaggedProofMeta):
        def __new__(
            mcls, name: str, bases: tuple[type, ...], namespace: object, **kwargs: object
        ) -> "Extended":
            cls = super().__new__(mcls, name, bases, namespace, **kwargs)  # type: ignore[arg-type]
            for key in list(observation._SEALED_PROOF_CLASS):
                if key[0] is cls:
                    observation._SEALED_PROOF_CLASS[key] = ProofClass.ARRIVAL
            return cls  # type: ignore[return-value]

    class Subverted(TaggedProof, metaclass=Extended):
        LIVE = ("subverted-live", ProofClass.LIVENESS)

    # The seal itself was successfully rewritten -- the defence is not that the
    # write failed, but that the declaring metaclass is no longer the trusted one.
    assert observation._SEALED_PROOF_CLASS[(Subverted, "LIVE")] is ProofClass.ARRIVAL
    with pytest.raises(ValueError, match="not TaggedProofMeta"):
        Proven(MemberAnswer(), by=Subverted.LIVE)


def test_ac9e_a_rewritten_seal_is_tamper_evident() -> None:
    """EMPIRICAL r3 B1, route 1: a direct write to the module-private registry.

    This is the one route needing no metaclass. It cannot be PREVENTED -- a
    module-private dict is private by convention -- but it can be made
    detectable, because the write changes the RECORD while the member's own
    declaration still says what it was declared to say. Admission compares them.

    This is deliberately not a claim that in-process memory is a security
    boundary. It is F84's claim: a guard must not authorize on a single mutable
    reading of the thing it is guarding.
    """

    class Planted(TaggedProof):
        LIVE = ("planted-live", ProofClass.LIVENESS)

    observation._SEALED_PROOF_CLASS[(Planted, "LIVE")] = ProofClass.ARRIVAL
    try:
        with pytest.raises(ValueError, match="disagrees with its declaration"):
            Proven(MemberAnswer(), by=Planted.LIVE)
    finally:
        observation._SEALED_PROOF_CLASS[(Planted, "LIVE")] = ProofClass.LIVENESS


def test_ac9f_two_consistent_writes_defeat_admission_and_that_is_ACCEPTED() -> None:
    """The boundary, pinned as a test so it cannot be quietly forgotten.

    Tamper-evidence detects a SINGLE rewrite: the seal and the member's own
    declaration disagree. It cannot detect a consistent PAIR of writes -- they
    agree on a lie and there is nothing left to compare.

    This asserts the limit rather than a defence, because the alternative was
    measured and does not exist: splitting arrival and liveness into separate
    enum classes removes the tag field but trades this attack for `__class__`
    reassignment plus a `_member_map_` fix-up. Two writes beat both shapes.
    Evidence: `probes/f84-typebound-2026-07-27/SUPERVISOR-agreeing-lie.md`.

    **The threat model is ACCIDENT, not ADVERSARY.** Code that can write
    `_SEALED_PROOF_CLASS` can also rebind `Proven`. If this test ever starts
    FAILING, someone has found a cheaper defence than we could -- read the probe
    memo before assuming the new check is sound.
    """

    class Agreeing(TaggedProof):
        LIVE = ("agreeing-lie", ProofClass.LIVENESS)

    observation._SEALED_PROOF_CLASS[(Agreeing, "LIVE")] = ProofClass.ARRIVAL
    object.__setattr__(Agreeing.LIVE, "_proof_class", ProofClass.ARRIVAL)
    try:
        admitted = Proven(MemberAnswer(), by=Agreeing.LIVE)
        assert admitted.by is Agreeing.LIVE  # ADMITTED -- the accepted limit
    finally:
        observation._SEALED_PROOF_CLASS[(Agreeing, "LIVE")] = ProofClass.LIVENESS
        object.__setattr__(Agreeing.LIVE, "_proof_class", ProofClass.LIVENESS)

    # And the single-sided write it DOES catch, asserted alongside so the
    # difference between the two is the thing under test.
    observation._SEALED_PROOF_CLASS[(Agreeing, "LIVE")] = ProofClass.ARRIVAL
    try:
        with pytest.raises(ValueError, match="disagrees with its declaration"):
            Proven(MemberAnswer(), by=Agreeing.LIVE)
    finally:
        observation._SEALED_PROOF_CLASS[(Agreeing, "LIVE")] = ProofClass.LIVENESS
