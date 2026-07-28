"""Three-valued observation core (F84).

The defect this exists to remove: a predicate that FAILED TO EVALUATE returns
the same value as one that evaluated to FALSE. Two-valued answers to
three-valued questions.

An ``Observation`` answers "is X true of this subject?" with one of three:

- ``Proven``       -- true, and here is the proof that established it
- ``Disproven``    -- false, settled; it will never become true
- ``Unobservable`` -- the question could not be asked; retry per the trigger

``Unobservable`` is the state today's ``bool`` predicates collapse into
``False``, which is why a barrier whose member's terminal vanished reads
identically to one whose member answered "no".

Placement is fork decision **D8**: this module lives under ``models/`` so that
``clients/database.py`` can import it without a client importing a service.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, EnumType
from typing import Any, Generic, Iterable, Literal, NoReturn, Protocol, TypeVar, runtime_checkable

__all__ = [
    "ProofClass",
    "TaggedProofMeta",
    "ProofMember",
    "TaggedProof",
    "CoverageProof",
    "CoverageNegative",
    "CoverageUnobs",
    "CoverageReason",
    "Covered",
    "Deadline",
    "RetryTrigger",
    "Proven",
    "Disproven",
    "Unobservable",
    "Observation",
    "AggregateSpec",
    "fold",
    "SettlementOutcome",
]


class ProofClass(str, Enum):
    """What a proof member actually witnesses.

    ARRIVAL proves the payload reached the receiver. LIVENESS proves only that
    the receiver was alive -- which is NOT evidence of arrival, and is the
    conflation F84's admission gates exist to refuse.
    """

    ARRIVAL = "arrival"
    LIVENESS = "liveness"


@runtime_checkable
class ProofMember(Protocol):
    """The bound on ``P``: a proof member must declare what class it witnesses.

    R12-E75: the tag used to be a COMMENT beside a bare string, so the
    constructor guard had nothing to read. A bound makes an untagged enum a
    type error rather than a silent admission.
    """

    @property
    def proof_class(self) -> ProofClass: ...


class TaggedProofMeta(EnumType):
    """Validate proof declarations BEFORE the members exist, and seal the tags.

    EMPIRICAL r2 killed the previous scheme, which keyed the sealed tags on
    ``id(member)``. Five routes admitted a LIVENESS member, and the worst
    needed no adversary at all: a duplicate-value alias leaves a STALE ``id()``
    in the registry, CPython recycles that address, and the reviewer's probe hit
    it with an ordinary string in ONE allocation -- an object that never passed
    through ``__new__`` inheriting a sealed ARRIVAL tag. **An integer that the
    allocator may reuse cannot be an authorization key.**

    Two of those five routes are not admission bugs at all -- they are
    DECLARATIONS that should never have compiled, so they are refused here,
    where the class is being built:

    - a tag that is not a ``ProofClass`` (the R09 mutant's opening: a guard
      written as "refuse LIVENESS" admits a malformed tag, whereas one written
      as "admit only ARRIVAL" refuses it -- but neither should have to, because
      the declaration itself is wrong)
    - a duplicate value, which makes one member an ALIAS of another and hides
      its declared tag behind the first member's

    What remains is sealed into ``_SEALED_PROOF_CLASS`` keyed by ``(class,
    member name)``: a name is not recycled while its class is alive, and it
    cannot be reached by overriding a property or assigning to ``_proof_class``.
    """

    def __new__(
        mcls, name: str, bases: tuple[type, ...], namespace: Any, **kwargs: Any
    ) -> "TaggedProofMeta":
        declared = {
            key: value
            for key, value in list(namespace.items())
            if not key.startswith("_") and isinstance(value, tuple) and len(value) == 2
        }
        cls = super().__new__(mcls, name, bases, namespace, **kwargs)
        seen: dict[str, str] = {}
        for member_name, (value, tag) in declared.items():
            if not isinstance(tag, ProofClass):
                raise TypeError(f"{name}.{member_name}: proof class {tag!r} is not a ProofClass")
            if value in seen:
                raise ValueError(
                    f"{name}.{member_name}: duplicate value {value!r} aliases "
                    f"{name}.{seen[value]}; an alias hides its own proof class"
                )
            seen[value] = member_name
            _SEALED_PROOF_CLASS[(cls, member_name)] = tag
        return cls


class TaggedProof(str, Enum, metaclass=TaggedProofMeta):
    """Base for every adopter's proof enum: a str-enum whose members carry a tag.

    R13v28-E79: the first attempt wrote ``proof_class = ProofClass.ARRIVAL`` in
    the class body, which creates a FAKE ENUM MEMBER -- ``list(ProofKind)``
    returned the tag as a member and its type was the adopter enum, not
    ``ProofClass``. Verified at the interpreter. The tuple + ``__new__`` form
    below stores the tag as instance state, so members keep their string values
    and the member list stays clean.

    R14-E88: declared ONCE here; every adopter enum inherits it rather than
    re-spelling the implementation.

    **Why `(str, Enum)` and not `StrEnum`** (EMPIRICAL r1-code, re-ruled r1
    follow-up): `StrEnum` is legal on the current `>=3.14` floor, so the
    original version reason is gone. The mixin stays for two live reasons --
    every other str-enum in `src/` uses this form, and M6 pins the durable
    contract to `==`/`.value`, which this form already satisfies. A revert
    would buy no correctness and would change `str()`/format behaviour at
    every call site. Adopting `StrEnum` is a deliberate repo-wide migration
    with persistence review, not an F84-local edit.

    **Note the mixin is not a drop-in for `StrEnum` under interpolation:**
    `f"{member}"` yields `"Class.MEMBER"` here, where `StrEnum` yields the
    value. `==` against the string and `.value` are stable under both, so
    durable comparisons use those and never interpolation.

    Historical note, kept because it is the reason the floor moved at all: the
    first draft used `StrEnum` under a `>=3.10` floor and was the ONLY such use
    in `src/`. Local mypy missed it because `mypy.ini` pinned 3.11 over
    `pyproject.toml`'s 3.10 while the dev interpreter was 3.13 -- every check
    ran ABOVE the floor it was meant to defend. A tool configured above the
    floor cannot see the floor.
    """

    _proof_class: ProofClass

    def __new__(cls, value: str, proof_class: ProofClass) -> "TaggedProof":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj._proof_class = proof_class
        # The ADMISSION tag is NOT recorded here. `__new__` runs per member and
        # can be reached by a subclass through the `_new_member_` hook EnumType
        # retains (EMPIRICAL r2 route 2), so anything sealed here is sealed by
        # the caller. `TaggedProofMeta` seals from the DECLARATION instead.
        # `_proof_class` and the property below are the READABLE tag only.
        return obj

    @property
    def proof_class(self) -> ProofClass:
        return self._proof_class


# The sealed admission tags, keyed by ``(owning class, member name)``.
#
# EMPIRICAL r1-code B2 established WHY a separate record is needed:
# `Proven.__post_init__` used to authorize on the `proof_class` PROPERTY, which
# a subclass can override, and on `_proof_class`, which ordinary assignment can
# rewrite. Both produced a `Proven` from a LIVENESS member.
#
# EMPIRICAL r2 established what the record may be KEYED ON. `id()` was wrong:
# it is recycled, so a stale entry from an aliased member transferred a sealed
# ARRIVAL tag to an unrelated object in one allocation, with no attacker. The
# key is now the member's NAME under its class, which the allocator never
# reuses.
_SEALED_PROOF_CLASS: dict[tuple[type, str], ProofClass] = {}


class CoverageProof(TaggedProof):
    """Arrival-class evidence that an artifact's entries match the delta."""

    ENTRIES_MATCH = ("entries_match", ProofClass.ARRIVAL)


CoverageNegative = Literal["entry_mismatch"]
CoverageUnobs = Literal["apparatus_unavailable", "unhashable_entry"]
CoverageReason = CoverageNegative | CoverageUnobs


@dataclass(frozen=True)
class Covered:
    """The artifact covers the delta; evidence is carried by ``Proven.by``."""


def _sealed_proof_class(member: ProofMember) -> ProofClass:
    """Return the tag sealed at DECLARATION, never a live read.

    **THE THREAT MODEL IS ACCIDENT, NOT ADVERSARY** (supervisor, after r3;
    measured in `probes/f84-typebound-2026-07-27/SUPERVISOR-agreeing-lie.md`).
    What these checks stop is an adopter that declares a liveness proof and uses
    it where arrival is required -- a mistake made by code trying to be correct,
    which is the F84 defect at this seat.

    They do NOT stop a caller that writes `_SEALED_PROOF_CLASS` and the member's
    `_proof_class` TOGETHER: two consistent writes agree on a lie and admission
    has nothing left to compare. Verified, along with the alternative shape
    (splitting arrival and liveness into separate enum classes), which trades
    this two-write attack for `__class__` reassignment plus a `_member_map_`
    fix-up -- differently shaped, not stronger.

    Code that can write module-private state can also rebind `Proven` itself, so
    no check here could survive it. **The honest claim is that a liveness proof
    cannot reach `Proven` by ACCIDENT or by ORDINARY EXTENSION -- not that it
    cannot reach `Proven`.** Stating the boundary is the point; a guard that
    implies more than it defends is the overclaim R14-E89 already cost us.

    Two things are checked before the tag is read, because a tag read from the
    wrong object is worse than no tag:

    1. the member must BE the canonical member its own class publishes under
       that name -- this refuses an impostor object that merely has a `name`,
       and refuses an alias, which is a different name for someone else's member
    2. a tag must have been sealed for that (class, name) at declaration time

    A member failing either did not come from a `TaggedProof` declaration and
    has no admission record, so it cannot establish `Proven`.
    """
    owner = type(member)
    # EMPIRICAL r3 B1: the owner's metaclass must be EXACTLY `TaggedProofMeta`.
    # A metaclass SUBCLASS can call `super().__new__` -- letting the seal be
    # written -- and then rewrite it, which r3 demonstrated. It can also shadow
    # `__members__` so the canonical-membership check below reads a map the
    # attacker supplies. Neither is reachable without declaring a metaclass, so
    # requiring the exact one closes both, and closes every future variation on
    # "extend the metaclass" without needing to enumerate them.
    if type(owner) is not TaggedProofMeta:  # type: ignore[comparison-overlap]
        raise ValueError(
            f"{member!r} belongs to {owner.__name__}, whose metaclass is "
            f"{type(owner).__name__} and not TaggedProofMeta; only enums declared "
            "by TaggedProofMeta itself can establish Proven"
        )
    try:
        canonical = owner.__members__[member.name]
    except (AttributeError, KeyError, TypeError):
        raise ValueError(
            f"{member!r} is not a member of a proof enum; only members declared "
            "by a TaggedProof subclass can establish Proven"
        ) from None
    if canonical is not member:
        raise ValueError(
            f"{member!r} is not the canonical member of {owner.__name__} "
            "under its own name; an alias cannot establish Proven"
        )
    # EMPIRICAL r3 B1: the seal must still AGREE with the declaration it was
    # taken from. A direct write to the module-private dict is the one route
    # that needs no metaclass -- but it can only change the RECORD, not the
    # member's own declared tag, so comparing the two makes the write
    # tamper-EVIDENT. This is deliberately not a claim that in-process memory is
    # a security boundary; it is the F84 claim, that a guard must not authorize
    # on a single mutable reading of the thing it is guarding.
    declared = getattr(member, "_proof_class", None)
    sealed_now = _SEALED_PROOF_CLASS.get((owner, member.name))
    if sealed_now is not None and declared is not None and sealed_now is not declared:
        raise ValueError(
            f"{member!r} has a sealed proof class ({sealed_now}) that disagrees "
            f"with its declaration ({declared}); the admission record was "
            "rewritten after the class was created"
        )
    try:
        sealed = _SEALED_PROOF_CLASS[(type(member), member.name)]
    except KeyError:
        raise ValueError(
            f"{member!r} carries no sealed proof class; only members declared by "
            "TaggedProof can establish Proven"
        ) from None
    if not isinstance(sealed, ProofClass):
        # The metaclass refuses a malformed tag at declaration, so reaching
        # this means the dict itself was written to -- the one route left once
        # the key is no longer forgeable. Checked rather than trusted because
        # the alternative is the R09 shape: a value that is NEITHER class
        # slipping through a guard that only asks whether it is the wrong one.
        raise ValueError(
            f"{member!r} has a sealed value that is not a ProofClass; the "
            "admission record was written outside TaggedProof"
        )
    return sealed


@dataclass(frozen=True)
class Deadline:
    """Retry when wall-clock passes ``at``."""

    at: float


# The only retry trigger with a constructor. v1's Event(name) and v3's
# TurnBoundary(terminal_id) were both dropped (N3): the D11 probe found no
# receiver-side pre-transcript observable on any provider, so the variant
# would have had no way to be built, and an unused variant in a core type
# invites invention.
RetryTrigger = Deadline

T = TypeVar("T")
R = TypeVar("R")
P = TypeVar("P", bound=ProofMember)


class _ObservationBase:
    """Carrier for X1's truthiness refusal.

    R13-E81: ``__bool__`` was declared on the union ALIAS, which cannot own a
    method -- so the rule had no representation and AC0 graded a probe that had
    invented a home for it. Every variant inherits this instead.

    Why it RAISES rather than returning False: all three adopters replace a
    bool-returning predicate, so ``if not predicate(...)`` would silently stop
    firing the moment the return type became an object -- the guard inverts and
    the suite stays green.
    """

    __slots__ = ()

    def __bool__(self) -> NoReturn:
        raise TypeError(
            "Observation has no truth value; match the variant "
            "(Proven / Disproven / Unobservable)"
        )


@dataclass(frozen=True)
class Proven(_ObservationBase, Generic[T, P, R]):
    """True, with the proof that established it."""

    value: T
    by: P

    def __post_init__(self) -> None:
        # A liveness-class proof can never establish arrival. Refusing at
        # construction means the bad observation cannot be built, which is
        # stronger than a reader that has to remember to check.
        sealed = _sealed_proof_class(self.by)
        if sealed is not ProofClass.ARRIVAL:
            raise ValueError(
                f"{self.by!r} is {sealed}; only ARRIVAL-class " "proof can establish Proven"
            )


@dataclass(frozen=True)
class Disproven(_ObservationBase, Generic[T, P, R]):
    """False, and settled: no retry can change it."""

    reason: R


@dataclass(frozen=True)
class Unobservable(_ObservationBase, Generic[T, P, R]):
    """The question could not be asked. NOT the same as 'no'."""

    reason: R
    retry_after: RetryTrigger


Observation = Proven[T, P, R] | Disproven[T, P, R] | Unobservable[T, P, R]


@dataclass(frozen=True)
class AggregateSpec(Generic[T, P, R]):
    """How to build the AGGREGATE observation a fold produces.

    X3: a fold changes the SUBJECT -- "did every member arrive?" is a different
    question from "did THIS member arrive?" -- so the aggregate's value and
    proof cannot be reduced from the components. They have to be supplied.

    R14-E90 declared the fields; R15v32-E94 forced both instances to be pinned
    per adopter rather than defaulted.
    """

    value: T
    proven_by: P
    disproven_reason: R
    unobserved_reason: R
    retry_after: RetryTrigger
    # The EMPTY-input disposition, ruled per adopter and never defaulted: a
    # barrier with no members and a digest with no entries are not the same
    # answer. (DESIGN r18 P1 + R16-E102 + R17-E106, three gates converging.)
    empty: Observation[T, P, R]


CT = TypeVar("CT")
CR = TypeVar("CR")
CP = TypeVar("CP", bound=ProofMember)
AT = TypeVar("AT")
AR = TypeVar("AR")
AP = TypeVar("AP", bound=ProofMember)


def fold(
    components: Iterable[Observation[CT, CP, CR]],
    aggregate: AggregateSpec[AT, AP, AR],
) -> Observation[AT, AP, AR]:
    """Combine component observations into ONE about a different subject.

    The law is the meet over ``Unobservable > Disproven > Proven``: the
    aggregate takes the weakest verdict present. The universal it enforces is
    that an ``Unobservable`` component NEVER yields a ``Disproven`` aggregate --
    "we could not look" must not become "we looked and it was false".

    DESIGN r18 P2 deleted the ``FoldLaw`` parameter: its two members
    (ABSORBING/DOMINATING) were the same function, so swapping them at the two
    seats was a mutant nothing could kill. What the seats actually differ in is
    which component states are observations at all -- a SEAT pre-filter, applied
    before calling this -- and their ``empty`` disposition.

    Note the component type parameters are separate from the aggregate's: they
    describe different subjects, and sharing them asserted the opposite of X3.
    """
    seen = list(components)
    if not seen:
        return aggregate.empty
    if any(isinstance(c, Unobservable) for c in seen):
        return Unobservable(aggregate.unobserved_reason, aggregate.retry_after)
    if any(isinstance(c, Disproven) for c in seen):
        return Disproven(aggregate.disproven_reason)
    return Proven(aggregate.value, aggregate.proven_by)


class SettlementOutcome(str, Enum):
    """The three-valued result of a conditional settlement CAS.

    R18-E110: these were declared as a ``Literal`` of three strings with a
    comment claiming two of them were falsy. All three are non-empty strings,
    so all three were TRUE -- a caller writing ``if not settle(...)`` would read
    a failed CAS requiring retry as success. That is a two-valued read of a
    three-valued answer, inside the fix for two-valued reads of three-valued
    answers. The ``__bool__`` below makes the falsiness a mechanism.
    """

    SETTLED = "settled"
    STALE = "stale"
    RECOVERY = "settlement_pending_recovery"

    def __bool__(self) -> bool:
        return self is SettlementOutcome.SETTLED
