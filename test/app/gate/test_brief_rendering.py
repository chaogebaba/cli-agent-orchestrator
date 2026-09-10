"""Brief and callback rendering from fixture rows (WP-ARCH A, slice 2a).

Golden render over a ``RoundProjection`` built in memory — no database, no Click.
The 2a arms: A1 (every sha in a rendered brief equals the derived value; no
free-text sha slot), A3 (scratch root generated from the host), A11 (four-line
callback with a computed pin digest and no attestation block).  Each has a MUTANT
comment naming the edit that must turn it red.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cli_agent_orchestrator.app.gate import render as r
from cli_agent_orchestrator.core import gate as g


def _projection(
    *,
    host: str = "laptop",
    open_findings: tuple[g.OpenFinding, ...] = (),
    pins: tuple[str, ...] = (),
) -> g.RoundProjection:
    manifest = g.ArtifactManifest(
        manifest_version=1,
        repo_bindings=(g.RepoBinding(name="fork", commit="deadbeefcafe"),),
        base_sha="b" * 40,
        head_sha="h" * 40,
        branch="cao/x",
        worktree_path="/w",
        entries=(g.DiffEntry(post_path="a.py", post_object_id="o1"),),
        blueprint_sha="bp" * 20,
        ac_list_sha="ac" * 20,
    )
    run = g.GateRun(
        run_id="RUN1",
        wp="F791",
        lane="dev",
        owner_conversation="c1",
        owner_epoch=1,
        max_rounds=3,
        row_version=1,
    )
    round_ = g.GateRound(
        round_id="R1",
        run_id="RUN1",
        round_no=1,
        build_inputs=manifest,
        execution_target=g.ExecutionTarget(host=host),
        test_command="pytest -q",
        evidence_tier="HIGH@rev1",
        state=g.RoundState.OPEN,
        generation=0,
        row_version=1,
        created_at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    dispatch = g.Dispatch(
        dispatch_id="D1",
        round_id="R1",
        role=g.DispatchRole.BUILDER,
        position="dev",
        request_id="req1",
        pins=pins,
    )
    return g.RoundProjection(
        run=run,
        round=round_,
        dispatches=(dispatch,),
        open_findings=open_findings,
    )


# -- AC-A1: derived sha in the brief, no free-text slot ---------------------


def test_ac_a1_brief_carries_derived_artifact_sha() -> None:
    proj = _projection()
    expected = g.compute_artifact_sha(proj.round.build_inputs)
    brief = r.render_brief(proj, g.DispatchRole.BUILDER)
    assert f"artifact_sha={expected}" in brief
    # MUTANT: a template that accepts a caller-supplied head_sha would let a value
    # other than `expected` appear; the equality above pins it to the derivation.


def test_ac_a1_brief_is_pure_function_of_rows() -> None:
    # AC-A1 restart arm: re-rendering the same projection is byte-identical (no
    # markdown read, no clock).
    proj = _projection()
    assert r.render_brief(proj, g.DispatchRole.LEDGER) == r.render_brief(
        proj, g.DispatchRole.LEDGER
    )


# -- AC-A3: scratch root generated from host --------------------------------


def test_ac_a3_brief_renders_box_scratch() -> None:
    brief = r.render_brief(_projection(host="grok-box-002"), g.DispatchRole.BUILDER)
    assert "scratch=~/box-scratch/R1/" in brief


def test_ac_a3_brief_renders_laptop_scratch() -> None:
    brief = r.render_brief(_projection(host="laptop"), g.DispatchRole.BUILDER)
    assert "scratch=/data/cao-scratch/R1/" in brief
    # MUTANT: hard-code one scratch root -> the box arm above renders the laptop
    # path and fails.


# -- AC-A2: open findings render verbatim in the brief ----------------------


def test_ac_a2_open_findings_render_in_brief() -> None:
    findings = (
        g.OpenFinding(
            finding_id="F1",
            raised_in_round="R0",
            severity=g.Severity.BLOCKER,
            statement="consumer X not updated",
        ),
        g.OpenFinding(
            finding_id="F2",
            raised_in_round="R0",
            severity=g.Severity.SHOULD,
            statement="doc drift",
        ),
    )
    brief = r.render_brief(_projection(open_findings=findings), g.DispatchRole.BUILDER)
    assert "Open findings carried in: 2" in brief
    assert "consumer X not updated" in brief
    assert "doc drift" in brief


def test_ac_a2_no_findings_renders_none() -> None:
    brief = r.render_brief(_projection(), g.DispatchRole.BUILDER)
    assert "Open findings carried in: none" in brief


# -- AC-A11: four-line callback, computed pins, no attestation block --------


def test_ac_a11_callback_is_four_lines() -> None:
    proj = _projection(pins=("pin-a", "pin-b"))
    env = r.render_callback(
        proj,
        kind=r.EnvelopeKind.RESULT,
        summary=("built ok", "2 tests green"),
        artifact_ref="cas://abc",
    )
    assert len(env.lines) == 4  # identity + 2 summary + pointer
    assert env.pin_count == 2
    assert f"[pins ok: 2 @ {env.pin_digest}]" in env.lines[-1]
    # The digest is DERIVED from the pins, not a hand-written attestation block.
    assert env.pin_digest == r.compute_pin_digest(("pin-a", "pin-b"))


def test_ac_a11_callback_refuses_over_two_summary_lines() -> None:
    proj = _projection()
    with pytest.raises(g.GateError):
        r.render_callback(
            proj,
            kind=r.EnvelopeKind.RESULT,
            summary=("a", "b", "c"),  # over budget
            artifact_ref="cas://abc",
        )


def test_ac_a11_pin_digest_stable_and_empty_distinct() -> None:
    assert r.compute_pin_digest(("x",)) == r.compute_pin_digest(("x",))
    assert r.compute_pin_digest(()) != r.compute_pin_digest(("x",))


def test_ac_a11_callback_visible_bytes_audited() -> None:
    proj = _projection(pins=("p",))
    env = r.render_callback(
        proj, kind=r.EnvelopeKind.RESULT, summary=("ok",), artifact_ref="cas://x"
    )
    assert env.visible_bytes == sum(len(line) for line in env.lines) + len(env.lines)
    assert env.renderer_version == "2a"
