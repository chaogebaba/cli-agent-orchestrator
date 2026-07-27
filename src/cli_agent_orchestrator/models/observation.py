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
from enum import Enum
from typing import Generic, Iterable, NoReturn, Protocol, TypeVar, runtime_checkable

__all__ = [
    "ProofClass",
    "ProofMember",
    "TaggedProof",
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


class TaggedProof(str, Enum):
    """Base for every adopter's proof enum: a str-enum whose members carry a tag.

    R13v28-E79: the first attempt wrote ``proof_class = ProofClass.ARRIVAL`` in
    the class body, which creates a FAKE ENUM MEMBER -- ``list(ProofKind)``
    returned the tag as a member and its type was the adopter enum, not
    ``ProofClass``. Verified at the interpreter. The tuple + ``__new__`` form
    below stores the tag as instance state, so members keep their string values
    and the member list stays clean.

    R14-E88: declared ONCE here; every adopter enum inherits it rather than
    re-spelling the implementation.

    **Why `(str, Enum)` and not `StrEnum`** (EMPIRICAL r1-code): `StrEnum` is
    3.11+, and this package's floor is `requires-python = ">=3.10"` with CI
    testing 3.10/3.11/3.12. The first draft used `StrEnum` and was the ONLY
    such use in `src/`; the other ten str-enums here all use this mixin form.
    Local mypy did not catch it because `mypy.ini:2` pins `python_version =
    3.11` (overriding `pyproject.toml:141`'s 3.10) and the dev interpreter is
    3.13 -- so every check ran above the floor it was supposed to defend.

    **Note the mixin is not a drop-in for `StrEnum` under interpolation:**
    `f"{member}"` yields the VALUE on 3.10 but `"Class.MEMBER"` on 3.11+, while
    `StrEnum` yields the value on all of them. `==` against the string and
    `.value` are stable everywhere, so durable comparisons use those and never
    interpolation.
    """

    _proof_class: ProofClass

    def __new__(cls, value: str, proof_class: ProofClass) -> "TaggedProof":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj._proof_class = proof_class
        return obj

    @property
    def proof_class(self) -> ProofClass:
        return self._proof_class


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
        if self.by.proof_class is not ProofClass.ARRIVAL:
            raise ValueError(
                f"{self.by!r} is {self.by.proof_class}; only ARRIVAL-class "
                "proof can establish Proven"
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
