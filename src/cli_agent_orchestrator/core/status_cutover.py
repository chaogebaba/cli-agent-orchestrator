"""The status cutover's switch, resolved at boot (WP-ARCH phase 2, D9).

``CAO_WORKER_TRUTH_STATUS ∈ {off, shadow, on}``, default ``off``, sitting beside
phase 1's ingestion gate and phase 3's delivery gate in ``bootstrap.py`` and read
ONCE per boot.  A THIRD switch rather than a third spelling of the first: one
master strangler flag would couple a phase-1 rollback to a phase-2 rollback, and
each phase has to be backable out on its own.

The positions:

===========  ==========================================================
``off``      exactly phase-1 behaviour
``shadow``   the new producers and the fold run; nothing is published
``on``       the projection publishes through the legacy egress
===========  ==========================================================

``shadow`` is a real position rather than a formality.  The claude_code source
has never run, so its disagreement profile against the pane classifier is unknown
in a way the codex source's no longer is, and ``shadow`` is where that evidence is
produced.  The kill switch at every point is to move the position DOWN; nothing
is deleted until sub-phase 2c, which is a separate commit for that reason.

Boot-time resolution and an ordered maturity ladder are Kubernetes' feature-gate
shape: gates are set by a process flag, and activating a change means restarting
the component rather than toggling it under load.  The per-provider allowlist is
the correction MetaMask made when a second provider arrived under a
single-provider flag layout — *"LaunchDarkly percentage rollouts operate per flag,
never per key. So the switch could never be progressively rolled out"* — which is
the shape this phase faces with codex and claude_code as its first two sources,
and the reason ``CAO_WORKER_TRUTH_STATUS_PROVIDERS`` is a LIST rather than a
boolean.

**Per-terminal source health is deliberately not a row of the table below.**  It
is evaluated continuously by the projector rather than once at boot, and an
unhealthy source falls back to the pane under I7 without changing the process
position.  A boot-time guard that tried to answer it would be answering a
question whose truth value changes every twenty seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from cli_agent_orchestrator.core.findings import FindingCode

__all__ = [
    "StatusGuardOutcome",
    "StatusPosition",
    "parse_providers",
    "parse_status_switch",
    "resolve_status_switch",
]


class StatusPosition(StrEnum):
    """``CAO_WORKER_TRUTH_STATUS``, three positions.

    Three and not phase 3's four.  Phase 3 needs a ``drain`` because a row
    enqueued while ``on`` exists in its queue and nowhere else, so demoting
    straight to ``shadow`` would strand it.  Status has no such row: the one
    durable artefact is the projection itself, which is rebuilt from the log
    rather than consumed, so there is nothing to orphan and no ``drain`` to reach.
    Carrying the fourth position anyway would be machinery for a hazard that does
    not exist here.
    """

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


def parse_status_switch(value: str | None) -> StatusPosition:
    """Read the environment variable.  Unknown or unset means ``off``.

    Permissive about case and surrounding whitespace, and deliberately not
    permissive about anything else: an operator who typed ``true`` gets ``off``,
    and the guard's finding is what tells them the cutover is not running.
    Guessing at an intended position would be a worse failure than the default,
    because the default is the safe one.
    """
    if value is None:
        return StatusPosition.OFF
    try:
        return StatusPosition(value.strip().lower())
    except ValueError:
        return StatusPosition.OFF


def parse_providers(value: str | None) -> frozenset[str]:
    """Read ``CAO_WORKER_TRUTH_STATUS_PROVIDERS`` — a comma-separated allowlist.

    Empty by default, which under ``on`` is what demotes the boot to ``shadow``:
    ``on`` with no provider to publish for is indistinguishable from a
    misconfiguration, and a phase that read it as "publish for everything" would
    turn a typo into a fleet-wide cutover.

    A provider is ELIGIBLE for this list when the phase ships an ``EventSource``
    for it — which at the end of phase 2 is exactly ``codex`` and ``claude_code``
    (D9c).  Nothing reads ``ProviderCapabilities`` for this: its
    ``native_status_source`` field is computed for every provider from a BACKEND
    test that is false for the tmux backend they all run on, so it cannot
    distinguish codex from claude_code from kiro, which is precisely what the
    allowlist needs.  The allowlist is the operator's control; the projector's
    source registry is the fact.
    """
    if not value:
        return frozenset()
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class StatusGuardOutcome:
    """The resolved position, and what the operator is told about it."""

    requested: StatusPosition
    position: StatusPosition
    providers: frozenset[str] = frozenset()
    finding: FindingCode | None = None
    detail: str = ""
    context: dict[str, str] = field(default_factory=dict)

    @property
    def demoted(self) -> bool:
        return self.position is not self.requested


def resolve_status_switch(
    requested: StatusPosition,
    *,
    ingest_enabled: bool,
    providers: frozenset[str] = frozenset(),
) -> StatusGuardOutcome:
    """D9's boot guard.  **Total** over requested position × condition.

    Total on purpose: no cell is left to a reader's inference, which is the
    property phase 3's D9 has and this phase's own r1 and r2 did not.

    ==========  ==========  ==============  ============  ====================
    Requested   Ingestion   Allowlist       Resolves to   Finding
    ==========  ==========  ==============  ============  ====================
    ``off``     either      either          ``off``       —
    ``shadow``  off         either          ``off``       ``DIAG-STATUS-GUARD``
    ``shadow``  on          either          ``shadow``    —
    ``on``      off         either          ``off``       ``DIAG-STATUS-GUARD``
    ``on``      on          empty           ``shadow``    ``DIAG-STATUS-GUARD``
    ``on``      on          non-empty       ``on``        —
    ==========  ==========  ==============  ============  ====================

    Ingestion-off DEMOTES rather than proceeds because a fold whose events reach
    no consumer is a silent status outage: the producers would run, the projection
    would move, and nothing would read it.  An empty allowlist under ``on``
    demotes for the different reason stated in :func:`parse_providers`.  In both
    cases the finding is the notice the operator gets — the guard never refuses
    the boot, because this ships into the server running the strangler work and a
    self-inflicted boot failure would be worse than the condition it reports.
    """
    if requested is StatusPosition.OFF:
        return StatusGuardOutcome(
            requested=requested, position=StatusPosition.OFF, providers=providers
        )

    if not ingest_enabled:
        return StatusGuardOutcome(
            requested=requested,
            position=StatusPosition.OFF,
            providers=providers,
            finding=FindingCode.DIAG_STATUS_GUARD,
            detail=(
                f"CAO_WORKER_TRUTH_STATUS={requested.value} needs worker-truth "
                "ingestion, which is off; a fold nothing reads is a silent status "
                "outage, so the cutover resolved to off"
            ),
            context={"requested": requested.value, "ingest": "off"},
        )

    if requested is StatusPosition.SHADOW:
        return StatusGuardOutcome(
            requested=requested, position=StatusPosition.SHADOW, providers=providers
        )

    if not providers:
        return StatusGuardOutcome(
            requested=requested,
            position=StatusPosition.SHADOW,
            providers=providers,
            finding=FindingCode.DIAG_STATUS_GUARD,
            detail=(
                "CAO_WORKER_TRUTH_STATUS=on with an empty "
                "CAO_WORKER_TRUTH_STATUS_PROVIDERS is indistinguishable from a "
                "misconfiguration; the cutover resolved to shadow"
            ),
            context={"requested": requested.value, "providers": ""},
        )

    return StatusGuardOutcome(
        requested=requested,
        position=StatusPosition.ON,
        providers=providers,
        context={"providers": ",".join(sorted(providers))},
    )
