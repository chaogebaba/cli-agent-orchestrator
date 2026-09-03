"""Provider-neutral replay authorization and per-kind attempt caps."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, cast

ReplayKind = Literal["ordinary", "tagged_replay", "inject", "suppress", "stop"]
OpenReplayKind = Literal["ordinary", "tagged_replay", "inject"]
CapCounter = Literal["ambiguous", "exhausted_boundary"]


@dataclass(frozen=True)
class ObservedFact:
    value: bool
    evidence_ref: str | None = None


@dataclass(frozen=True)
class AuthorizationFacts:
    """Generic core plus the optional binding-authority extension."""

    prior_ambiguous_eligible: ObservedFact
    prior_batch_hit: ObservedFact
    post_paste_successor_exists: bool
    receiver_alive: bool
    composer_empty: bool
    binding_authority: bool = False
    boundary_observation: object | None = None
    continuity_cursor: object | None = None
    permanently_busy_initial_protected: bool = False


@dataclass(frozen=True)
class ReplayDecision:
    kind: ReplayKind
    evidence: dict[str, object]


@dataclass(frozen=True)
class CapRule:
    counter: CapCounter
    limit: int


CAP_TABLE: Mapping[OpenReplayKind, CapRule] = MappingProxyType(
    {
        "ordinary": CapRule("ambiguous", 3),
        "tagged_replay": CapRule("ambiguous", 3),
        "inject": CapRule("exhausted_boundary", 3),
    }
)


class ReplayPolicy:
    """The only replay-kind decision site; intentionally provider-blind."""

    @staticmethod
    def decide(facts: AuthorizationFacts) -> ReplayDecision:
        if not facts.receiver_alive:
            return ReplayDecision("stop", {"reason": "receiver_gone"})
        if facts.prior_batch_hit.value:
            return ReplayDecision(
                "suppress",
                {"reason": "prior_batch_hit", "evidence_ref": facts.prior_batch_hit.evidence_ref},
            )
        if facts.permanently_busy_initial_protected:
            return ReplayDecision("suppress", {"reason": "permanently_busy_initial"})
        if not facts.prior_ambiguous_eligible.value:
            return ReplayDecision("ordinary", {})
        if facts.post_paste_successor_exists:
            return ReplayDecision("stop", {"reason": "post_paste_successor"})
        prior = facts.prior_ambiguous_eligible.evidence_ref
        if facts.binding_authority:
            if not facts.composer_empty or facts.boundary_observation is None:
                return ReplayDecision("stop", {"reason": "binding_boundary_closed"})
            return ReplayDecision("inject", {"prior_attempt_uuid": prior})
        return ReplayDecision("tagged_replay", {"prior_attempt_uuid": prior})


def run_post_auth_engine(
    facts: AuthorizationFacts,
    *,
    ambiguous_count: int,
    exhausted_boundary_count: int,
) -> ReplayDecision:
    """Apply policy, cap the selected kind, and emit shared lineage evidence."""
    decision = ReplayPolicy.decide(facts)
    if decision.kind not in CAP_TABLE:
        return decision
    open_kind = cast(OpenReplayKind, decision.kind)
    rule = CAP_TABLE[open_kind]
    counter = (
        ambiguous_count if rule.counter == "ambiguous" else exhausted_boundary_count
    )
    if counter >= rule.limit:
        return ReplayDecision(
            "stop",
            {"reason": "attempt_cap", "kind": decision.kind, "counter": rule.counter},
        )
    if decision.kind in {"tagged_replay", "inject"}:
        prior = decision.evidence.get("prior_attempt_uuid")
        if not isinstance(prior, str) or not prior:
            return ReplayDecision("stop", {"reason": "lineage_missing"})
        return ReplayDecision(
            decision.kind,
            {
                "prior_attempt_uuid": prior,
                "redelivery_tag": {"version": 1, "prior_attempt_uuid": prior},
            },
        )
    return decision
