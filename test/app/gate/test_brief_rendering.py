"""Brief and callback rendering from fixture rows (WP-ARCH A, slice 2a).

Golden render over a ``RoundProjection`` built in memory — no database, no Click.
The 2a arms: A1 (every sha in a rendered brief equals the derived value; no
free-text sha slot), A3 (scratch root generated from the host), A11 (four-line
callback with a computed pin digest and no attestation block).  Each has a MUTANT
comment naming the edit that must turn it red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cli_agent_orchestrator.app.gate import render as r
from cli_agent_orchestrator.core import gate as g

#: The fixed "now" the slice-B1 envelope arms are written against.
_QT0 = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


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
    assert env.renderer_version == "2b"


# -- slice B1: the question and anomaly envelopes ---------------------------


def _question(**overrides: object) -> g.RoundQuestion:
    base: dict[str, object] = {
        "question_id": "Q1",
        "dispatch_id": "d1",
        "client_request_id": "cr1",
        "owner_conversation": "c1",
        "owner_epoch": 1,
        "continuation_kind": g.ContinuationKind.ASSIGNMENT,
        "continuation_ref": "d1",
        "asked_at": _QT0,
        "expires_at": _QT0 + timedelta(hours=1),
        "question": "accept, re-round or override?",
        "options": ("accept", "re-round", "override"),
        "row_version": 1,
    }
    base.update(overrides)
    return g.RoundQuestion(**base)  # type: ignore[arg-type]


def test_a_question_envelope_is_four_lines_with_at_most_two_authored() -> None:
    """AC-A11's budget, on the envelope a suspended lane raises."""
    envelope = r.render_question_envelope(
        _question(), identity=r.IdentityRef(who="dev", context="WP-ARCH r3", ref="Q1")
    )
    assert envelope.kind is r.EnvelopeKind.QUESTION
    assert len(envelope.lines) == 4
    assert envelope.lines[0] == "dev · WP-ARCH r3 · Q1"
    assert envelope.lines[1] == "accept, re-round or override?"
    assert envelope.lines[2].startswith("options: ")
    assert envelope.lines[3].startswith("→ ")


def test_a_question_with_no_options_spends_only_one_authored_line() -> None:
    envelope = r.render_question_envelope(
        _question(options=()), identity=r.IdentityRef(who="dev", ref="Q1")
    )
    assert len(envelope.lines) == 3
    assert envelope.lines[0] == "dev · Q1"  # no round: the context field collapses


def test_a_long_question_is_clipped_rather_than_wrapped() -> None:
    """A wrapped line would silently grow the envelope past its four-line budget.

    MUTANT: wrap instead of clip and this goes red on the line count.
    """
    envelope = r.render_question_envelope(
        _question(question="x" * 400, options=()), identity=r.IdentityRef(who="dev", ref="Q1")
    )
    assert len(envelope.lines) == 3
    assert all("\n" not in line for line in envelope.lines)
    assert envelope.lines[1].endswith("…")


def test_an_anomaly_envelope_is_four_lines_and_names_the_deadline() -> None:
    """AC-A7's expiry surface: one envelope, four lines, the id to look at."""
    envelope = r.render_anomaly_envelope(
        _question(state=g.QuestionState.EXPIRED), identity=r.IdentityRef(who="dev", ref="Q1")
    )
    assert len(envelope.lines) == 4
    assert "UNANSWERED" in envelope.lines[1]
    assert envelope.lines[3].startswith("→ cao gate question Q1")


def test_neither_question_envelope_renders_a_build_attestation() -> None:
    """The mutant AC-A11 kills: an attestation BLOCK instead of a derived digest."""
    for envelope in (
        r.render_question_envelope(_question(), identity=r.IdentityRef(who="dev", ref="Q1")),
        r.render_anomaly_envelope(_question(), identity=r.IdentityRef(who="dev", ref="Q1")),
    ):
        body = "\n".join(envelope.lines)
        assert "build_attestation" not in body
        assert "[pins ok: 0 @ " in envelope.lines[-1]
        assert envelope.pin_digest == r.compute_pin_digest(())
        assert envelope.renderer_version == "2b"


def test_an_expiry_is_carried_as_a_condition_not_a_fifth_envelope_kind() -> None:
    """§10.2 A5 fixes the wire at four kinds; "anomaly" is the function's name only."""
    envelope = r.render_anomaly_envelope(_question(), identity=r.IdentityRef(who="dev", ref="Q1"))
    assert envelope.kind is r.EnvelopeKind.CONDITION
    assert {k.value for k in r.EnvelopeKind} == {"result", "question", "violation", "condition"}


def test_question_payload_is_the_one_serialiser_both_surfaces_use() -> None:
    """A ``--db`` transcript must be evidence about the served surface."""
    payload = r.question_payload(_question())
    assert payload["question_id"] == "Q1"
    assert payload["state"] == "PENDING"
    assert payload["is_open"] is True
    assert payload["options"] == ["accept", "re-round", "override"]
    assert payload["expires_at"] == (_QT0 + timedelta(hours=1)).isoformat()
    settled = r.question_payload(_question(state=g.QuestionState.ANSWERED))
    assert settled["is_open"] is False


# -- B1 r2: A5's typed classification ---------------------------------------


def test_the_expiry_envelope_is_a_condition_classified_anomaly() -> None:
    """A5 carries BOTH: four kinds on the wire, and a typed EXPECTED/ANOMALY.

    Asserted on the FIELD, never on ``lines``. The blueprint's own comment says
    "typed, not a prefix match" precisely because a consumer that decided what an
    envelope meant by reading the wording of a summary line would be silently
    wrong the first time that wording changed.
    """
    envelope = r.render_anomaly_envelope(
        _question(state=g.QuestionState.EXPIRED), identity=r.IdentityRef(who="dev", ref="Q1")
    )
    assert envelope.kind is r.EnvelopeKind.CONDITION
    assert envelope.classification is g.NoticeClass.ANOMALY


def test_an_ordinary_envelope_is_classified_expected() -> None:
    """The default, so only the sites that MEAN anomaly say so."""
    question_envelope = r.render_question_envelope(
        _question(), identity=r.IdentityRef(who="dev", ref="Q1")
    )
    assert question_envelope.classification is g.NoticeClass.EXPECTED
    callback = r.render_callback(
        _projection(), kind=r.EnvelopeKind.RESULT, summary=("ok",), artifact_ref="cas://x"
    )
    assert callback.classification is g.NoticeClass.EXPECTED


def test_the_classification_is_typed_not_a_string_prefix() -> None:
    """The mutant: drop the field and discriminate on ``lines[1]`` again.

    If ``classification`` were removed, the only separator left between an expiry
    and any other CONDITION would be the prose, which is what A5 forbids.
    """
    anomaly = r.render_anomaly_envelope(_question(), identity=r.IdentityRef(who="dev", ref="Q1"))
    expected = r.render_question_envelope(_question(), identity=r.IdentityRef(who="dev", ref="Q1"))
    assert isinstance(anomaly.classification, g.NoticeClass)
    assert anomaly.classification is not expected.classification
    assert set(g.NoticeClass) == {g.NoticeClass.EXPECTED, g.NoticeClass.ANOMALY}
    # And still no fifth kind: the wire vocabulary is unchanged.
    assert {k.value for k in r.EnvelopeKind} == {"result", "question", "violation", "condition"}
