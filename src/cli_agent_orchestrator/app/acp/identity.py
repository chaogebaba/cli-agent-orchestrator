"""D22 / AC-S1.12 — identity is a token CAO issued, bound to an ACP session id.

The decision is a change of AUTHORITY, not a new check.  Before it, "is this
terminal who it claims to be" was answered by detecting what kind of agent
occupied a pane; after it, the answer is a token CAO minted for that terminal and
bound, on the terminal's first call, to the ACP ``sessionId`` its subprocess
reported.  A pane-shaped marker becomes evidence about the MARKER.

The bug this exists to remove is named in the AC: ``unavailable`` behaves as a
permanent veto.  A terminal whose identity cannot be established YET is not a
terminal that has failed — it is one whose first call has not arrived — and
treating the two the same means a seat that is merely slow to bind never receives
anything, forever, with no deadline and nothing to look at.  So the vocabulary
here has THREE outcomes, not two:

``BIND``    the token matches and the generation is current.
``VETO``    the token or the generation is provably wrong.  Loud, and terminal:
            a wake addressed to a dead incarnation must never be delivered to the
            live one, because the two are different conversations.
``DEFER``   nothing is known yet.  Bounded by ``identity_bound_by``, and when
            that passes the terminal EXPIRES — which is a veto with a reason and
            a timestamp, not a veto with neither.

This module holds the decision and no I/O.  The caller supplies what it observed
and applies what it is told; the ordering of those two is the caller's problem,
and keeping the decision pure is what lets every arm of AC-S1.12 be a table row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from cli_agent_orchestrator.core.findings import FindingCode

__all__ = [
    "IdentityBinding",
    "IdentityDecision",
    "IdentityOutcome",
    "IdentityObservation",
    "decide_identity",
]

# The defer bound is NOT a constant here.  It is passed in, from the terminal's
# own launch-health budget, at the one call site that has it: a second
# independent number in this module would be a second opinion about how long a
# seat may take to come up, and the two would drift.  (It would also be a
# duration literal outside ``core/timing.py``, which §4c forbids and a test
# enforces.)


class IdentityOutcome(StrEnum):
    """Three outcomes.  Two would re-create the bug."""

    BIND = "bind"
    VETO = "veto"
    DEFER = "defer"


@dataclass(frozen=True)
class IdentityBinding:
    """What CAO issued and, once the first call lands, what it is bound TO.

    ``session_id`` is ``None`` until the terminal's first MCP call reports the
    ACP session its subprocess opened.  That is the BINDING, and it happens once:
    a second call reporting a different session on the same generation is a
    different subprocess wearing the same token, which is a veto.
    """

    terminal_id: str
    token: str
    lifecycle_generation: int
    session_id: str | None = None
    bound_at: datetime | None = None

    @property
    def is_bound(self) -> bool:
        return self.session_id is not None


@dataclass(frozen=True)
class IdentityObservation:
    """What arrived, and when.

    ``marker_says`` is whatever a pane-shaped detector reports about this
    terminal, carried for COUNTING only.  Under D22 it has no vote: a marker that
    disagrees with a token CAO issued is evidence about the marker, and acting on
    it would put the old authority back in charge through a side door.
    """

    presented_token: str | None
    presented_generation: int | None
    presented_session_id: str | None
    now: datetime
    marker_says: str | None = None


@dataclass(frozen=True)
class IdentityDecision:
    """The outcome, the reason, and the finding to count (if any)."""

    outcome: IdentityOutcome
    reason: str
    finding: FindingCode | None = None
    expires_at: datetime | None = None

    @property
    def is_permanent(self) -> bool:
        """A VETO is final for this generation; a DEFER never is."""
        return self.outcome is IdentityOutcome.VETO


def decide_identity(
    binding: IdentityBinding,
    observation: IdentityObservation,
    *,
    defer_bound_s: float,
) -> IdentityDecision:
    """Bind, veto or defer — and never veto for the absence of evidence.

    The order of the checks is the design.  A PROVABLY WRONG claim is vetoed
    first, so an attacker or a stale incarnation cannot reach the defer path by
    withholding a field.  Only then does absence mean "not yet".

    ``defer_bound_s`` is passed in, from the terminal's launch-health budget, so
    this module holds no opinion about how long a seat may take to come up and
    cannot drift from the one that does.
    """
    # 1. A stale GENERATION is the loud case, and the most important one: a wake
    #    addressed to a dead incarnation delivered to the live one puts one
    #    conversation's work into another's context, which nothing downstream can
    #    undo or even notice.
    if (
        observation.presented_generation is not None
        and observation.presented_generation != binding.lifecycle_generation
    ):
        return IdentityDecision(
            outcome=IdentityOutcome.VETO,
            reason=(
                f"stale generation: presented {observation.presented_generation}, "
                f"terminal is at {binding.lifecycle_generation}"
            ),
        )

    # 2. A WRONG token, when one was presented at all.  Distinguished from an
    #    absent token below, because "claimed something false" and "claimed
    #    nothing yet" are different events with different correct answers.
    if observation.presented_token is not None and observation.presented_token != binding.token:
        return IdentityDecision(
            outcome=IdentityOutcome.VETO, reason="token mismatch for this terminal"
        )

    # 3. A second, DIFFERENT session id on an already-bound generation: another
    #    subprocess wearing the same token.  The binding is once per generation,
    #    and a re-bind would silently hand the token to whichever process spoke
    #    last.
    if (
        binding.is_bound
        and observation.presented_session_id is not None
        and observation.presented_session_id != binding.session_id
    ):
        return IdentityDecision(
            outcome=IdentityOutcome.VETO,
            reason=(
                f"session rebind refused: bound to {binding.session_id}, "
                f"presented {observation.presented_session_id}"
            ),
        )

    # 4. Nothing presented and nothing bound: the first call has not arrived.
    #    DEFER, with a deadline — the clause that removes the measured bug.
    if not binding.is_bound and observation.presented_session_id is None:
        expires_at = (binding.bound_at or observation.now) + timedelta(seconds=defer_bound_s)
        if observation.now >= expires_at:
            return IdentityDecision(
                outcome=IdentityOutcome.VETO,
                reason="unbound past the identity deadline; the terminal is expired",
                expires_at=expires_at,
            )
        return IdentityDecision(
            outcome=IdentityOutcome.DEFER,
            reason="identity not bound yet; waiting for the terminal's first call",
            expires_at=expires_at,
        )

    # 5. Everything that is present agrees.  Bind (or stay bound), and COUNT a
    #    marker disagreement without acting on it — D22's posture in one line.
    finding = None
    if observation.marker_says is not None and observation.marker_says != binding.terminal_id:
        finding = FindingCode.DIAG_ACP_IDENTITY_DISAGREE
    return IdentityDecision(
        outcome=IdentityOutcome.BIND,
        reason="token and generation agree",
        finding=finding,
    )
