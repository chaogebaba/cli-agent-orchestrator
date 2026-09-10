"""Rendering a brief / ledger / verdict header from gate rows (WP-ARCH A, 2a).

The whole of slice 2a's rendering side: a builder/ledger/adjudicate brief, and a
four-line callback envelope, PROJECTED from the rows a :class:`RoundProjection`
carries.  Two properties are load-bearing and both are tested by AC-A1/A3/A11:

* **Every sha in a rendered brief is DERIVED, never typed (AC-A1).**  The artifact
  sha comes from :func:`core.gate.compute_artifact_sha` over the round's frozen
  manifest; there is no free-text sha slot for a caller to paste a stale value
  into.  ``render_brief`` takes a projection and nothing else, so the
  ``f829-a2-ledger-r5.md:12`` class — a head sha carried by hand from a previous
  round — is unrepresentable.
* **Scratch paths are GENERATED from the execution target's host (AC-A3).**  A
  box host renders ``~/box-scratch/<round>/``; the laptop renders
  ``/data/cao-scratch/<round>/``; :func:`core.gate.render_scratch_root` is the one
  function that decides, and hard-coding a root is the mutant it kills.

The callback envelope (AC-A11) is FOUR physical lines: an identity line, at most
two summary lines the sender authored, and a pointer line carrying a computed
``[pins ok: N @ 8-hex]``.  The pin digest is DERIVED from the round's pins — the
attestation block AC-A11's mutant re-injects is never rendered here — and an
envelope over the line budget degrades to identity plus pointer (§10.2 A5, P4:
"over budget → pointer only").

Nothing here reads a markdown file; the projection is the only input.  That is
what makes AC-A1's restart arm true — a round re-rendered after a server bounce
is byte-identical because it is a pure function of the stored rows.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cli_agent_orchestrator.app.gate.ports import RoundProjection
from cli_agent_orchestrator.core.gate import (
    DispatchRole,
    GateError,
    OpenFinding,
    compute_artifact_sha,
    render_scratch_root,
)

__all__ = [
    "CallbackEnvelope",
    "EnvelopeKind",
    "compute_pin_digest",
    "render_brief",
    "render_callback",
    "render_run_show",
    "run_show_payload",
]


class EnvelopeKind(StrEnum):
    """The four typed callback variants (§10.2 A5)."""

    RESULT = "result"
    QUESTION = "question"
    VIOLATION = "violation"
    CONDITION = "condition"


class CallbackEnvelope(BaseModel):
    """A rendered four-line callback (§10.2 ``CallbackEnvelope``, AC-A11).

    ``lines`` is the physical rendering: exactly the identity line, up to two
    authored summary lines, and one pointer line — never more than four.
    ``visible_bytes`` is the byte length so the surface can be audited (the
    findings §C1 un-auditable envelope becomes auditable).  ``pins_ok`` and
    ``pin_count`` are the computed attestation; there is no free ``build_attestation``
    block, which is the mutant AC-A11 kills.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnvelopeKind
    lines: tuple[str, ...] = Field(max_length=4)
    visible_bytes: int = Field(ge=0)
    pin_count: int = Field(ge=0)
    pin_digest: str
    renderer_version: str = "2a"


_MAX_SUMMARY_LINES = 2
_RENDERER_VERSION = "2a"


def compute_pin_digest(pins: tuple[str, ...]) -> str:
    """The 8-hex ``[pins ok]`` digest, DERIVED from the round's pins (AC-A11).

    A sha-256 over the ordered pin refs, truncated to eight hex characters — the
    ``@ 8-hex`` the envelope's pointer carries.  Derived rather than attested by a
    hand-written block: the mutant AC-A11 kills re-injects ``build_attestation``,
    and there is no code path here that renders one.  An empty pin set has its own
    stable digest, so "no pins" is distinguishable from "pins not computed".
    """
    hasher = hashlib.sha256()
    for pin in pins:
        hasher.update(pin.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:8]


def _round_pins(projection: RoundProjection) -> tuple[str, ...]:
    """The pins carried by the round's dispatches, de-duplicated in first-seen order."""
    seen: dict[str, None] = {}
    for dispatch in projection.dispatches:
        for pin in dispatch.pins:
            seen.setdefault(pin, None)
    return tuple(seen)


def _open_findings_block(findings: tuple[OpenFinding, ...]) -> list[str]:
    """Render the still-open findings verbatim, for the next brief (AC-A2).

    A round closing with N open findings renders all N in the successor's brief;
    the statement is reproduced verbatim because it is immutable once raised.
    """
    if not findings:
        return ["Open findings carried in: none"]
    lines = [f"Open findings carried in: {len(findings)}"]
    for finding in findings:
        lines.append(f"  - [{finding.severity.value.upper()}] {finding.statement}")
    return lines


def render_brief(projection: RoundProjection, role: DispatchRole) -> str:
    """Render a role's brief from the round's rows — no markdown read (AC-A1/A2/A3).

    The header carries the derived artifact sha of the build inputs, the generated
    scratch root for the round's execution target, the test command and evidence
    tier bound to the round, and the open findings carried in.  Every value is a
    projection of a stored row; nothing is typed by a caller, so a stale-sha class
    is unrepresentable.
    """
    round_ = projection.round
    run = projection.run
    manifest = round_.build_inputs
    artifact_sha = compute_artifact_sha(manifest)
    scratch = render_scratch_root(round_.execution_target, round_.round_id)

    repos = ", ".join(f"{b.name}@{b.commit[:8]}" for b in manifest.repo_bindings)
    lines = [
        f"# GATE BRIEF · {run.wp} · {run.lane} · {role.value.upper()}",
        f"run={run.run_id} round={round_.round_no} ({round_.round_id})",
        f"artifact_sha={artifact_sha}",
        f"base={manifest.base_sha[:8]}..head={manifest.head_sha[:8]} branch={manifest.branch}",
        f"repos: {repos}",
        f"blueprint_sha={manifest.blueprint_sha[:8]} ac_list_sha={manifest.ac_list_sha[:8]}",
        f"execution_target: host={round_.execution_target.host} scratch={scratch}",
        f"test_command={round_.test_command or '(none)'}",
        f"evidence_tier={round_.evidence_tier or '(none)'}",
    ]
    lines.extend(_open_findings_block(projection.open_findings))
    return "\n".join(lines) + "\n"


def render_callback(
    projection: RoundProjection,
    *,
    kind: EnvelopeKind,
    summary: tuple[str, ...],
    artifact_ref: str,
) -> CallbackEnvelope:
    """Render the four-line callback envelope (AC-A11).

    Line 1 identity (``who · wp rN · id``), then at most two AUTHORED summary
    lines, then a pointer line with the computed ``[pins ok: N @ 8-hex]``.  More
    than two summary lines is refused; an envelope over the four-line budget is
    impossible by construction because only up to two summary lines are admitted
    and the identity and pointer are one each.
    """
    if len(summary) > _MAX_SUMMARY_LINES:
        raise GateError(
            f"a callback carries at most {_MAX_SUMMARY_LINES} authored summary lines, "
            f"got {len(summary)}"
        )
    round_ = projection.round
    run = projection.run
    pins = _round_pins(projection)
    digest = compute_pin_digest(pins)

    identity = f"{run.lane} · {run.wp} r{round_.round_no} · {round_.round_id}"
    pointer = f"→ {artifact_ref} [pins ok: {len(pins)} @ {digest}]"
    lines = (identity, *summary, pointer)
    visible = sum(len(line) for line in lines) + len(lines)  # + newlines
    return CallbackEnvelope(
        kind=kind,
        lines=lines,
        visible_bytes=visible,
        pin_count=len(pins),
        pin_digest=digest,
        renderer_version=_RENDERER_VERSION,
    )


# ---------------------------------------------------------------------------
# ``cao gate show <run>`` rendering (read-only, projected from rows).
#
# The judgement about what an operator sees lives HERE, in ``app``, so it is
# testable without a Click runner.  ``run_show_payload`` builds a plain dict a
# ``--json`` flag emits verbatim; ``render_run_show`` turns the same data into the
# human text.  Both take the run, its rounds and (per round) a projection —
# everything the store already returns — and read no markdown.
# ---------------------------------------------------------------------------


def run_show_payload(run: object, projections: tuple[RoundProjection, ...]) -> dict[str, object]:
    """A JSON-ready view of a run and its rounds (``cao gate show --json``).

    ``run`` is a ``core.gate.GateRun``; it is annotated ``object`` only so this
    module imposes no import on a caller that already holds one.  Each round
    carries its DERIVED artifact sha and generated scratch root, never a stored
    or typed sha, so the ``--json`` surface has the same AC-A1/AC-A3 guarantee the
    text brief does.
    """
    from cli_agent_orchestrator.core.gate import GateRun

    assert isinstance(run, GateRun)
    rounds: list[dict[str, object]] = []
    for projection in projections:
        round_ = projection.round
        rounds.append(
            {
                "round_id": round_.round_id,
                "round_no": round_.round_no,
                "state": round_.state.value,
                "artifact_sha": compute_artifact_sha(round_.build_inputs),
                "review_snapshot_sha": (
                    None
                    if round_.review_snapshot is None
                    else compute_artifact_sha(round_.review_snapshot)
                ),
                "subject_sha": round_.subject_sha,
                "report_bytes_sha": round_.report_bytes_sha,
                "verdict_report_sha": round_.verdict_report_sha,
                "scratch_root": render_scratch_root(round_.execution_target, round_.round_id),
                "host": round_.execution_target.host,
                "dispatches": [
                    {"role": d.role.value, "position": d.position, "state": d.state.value}
                    for d in projection.dispatches
                ],
                "open_findings": [
                    {"severity": f.severity.value, "statement": f.statement}
                    for f in projection.open_findings
                ],
            }
        )
    return {
        "run_id": run.run_id,
        "wp": run.wp,
        "lane": run.lane,
        "state": run.state.value,
        "owner_conversation": run.owner_conversation,
        "owner_epoch": run.owner_epoch,
        "max_rounds": run.max_rounds,
        "rounds": rounds,
    }


def render_run_show(run: object, projections: tuple[RoundProjection, ...]) -> str:
    """The human text for ``cao gate show <run>`` (read-only, no markdown read)."""
    payload = run_show_payload(run, projections)
    lines = [
        f"run {payload['run_id']} · {payload['wp']} · {payload['lane']} " f"[{payload['state']}]",
        f"owner={payload['owner_conversation']}@{payload['owner_epoch']} "
        f"max_rounds={payload['max_rounds']}",
    ]
    rounds = payload["rounds"]
    assert isinstance(rounds, list)
    for entry in rounds:
        assert isinstance(entry, dict)
        lines.append(
            f"  round {entry['round_no']} [{entry['state']}] "
            f"artifact_sha={str(entry['artifact_sha'])[:12]} "
            f"host={entry['host']}"
        )
        dispatches = entry["dispatches"]
        assert isinstance(dispatches, list)
        for dispatch in dispatches:
            assert isinstance(dispatch, dict)
            lines.append(
                f"    dispatch {dispatch['role']} -> {dispatch['position']} "
                f"[{dispatch['state']}]"
            )
        findings = entry["open_findings"]
        assert isinstance(findings, list)
        for finding in findings:
            assert isinstance(finding, dict)
            lines.append(f"    OPEN [{str(finding['severity']).upper()}] {finding['statement']}")
    return "\n".join(lines) + "\n"
