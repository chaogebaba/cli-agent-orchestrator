"""F862 (#718) — anchor-sequence orchestration tests (AC-1, AC-2, AC-7).

Drives ``run_review`` with injected seams (verify_pin, browser turn, publish) so
the D2 anchor sequence is proven without a browser.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.orchestrator import ReviewRequest, run_review
from cli_agent_orchestrator.chatgpt_web_runner.output import PartialSource, Submitted
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import AcceptedAnswer

pytestmark = pytest.mark.unit

REQ = ReviewRequest(
    artifact_path="/data/cao-scratch/briefs/x.md",
    artifact_sha256="a" * 64,
    bundle_sha256="b" * 64,
    run_id="run0",
)

_VALID_FINDINGS_BODY = (
    "Finding 1: Cite: D3 line 4. "
    "OLD: `permitted every private backend endpoint`. "
    "REPLACE: `permits exactly two reads`. "
    "Evidence: blueprint section D3 conflicts with the recon note.\n"
)

ANSWER = AcceptedAnswer(
    text=_VALID_FINDINGS_BODY,
    model_slug="gpt-5-6-thinking",
    thinking_effort="extended",
    conversation_id="11111111-2222-3333-4444-555555555555",
    assistant_node_id="asst-1",
)


def _ok_pin() -> bool:
    return True


def _bad_pin() -> bool:
    return False


def test_ac1_happy_path_publishes_with_both_pins_valid() -> None:
    published: list[str] = []

    def _publish(body: str) -> str:
        published.append(body)
        return "/data/cao-scratch/briefs/f862-findings.md"

    outcome = run_review(
        REQ,
        verify_pin_start=_ok_pin,
        verify_pin_before_publish=_ok_pin,
        browser_turn=lambda: ANSWER,
        publish=_publish,
    )
    assert outcome.ok is True
    assert outcome.report_path == "/data/cao-scratch/briefs/f862-findings.md"
    assert outcome.submitted is Submitted.TRUE
    assert published and published[0].startswith("Status: FINDINGS-READY")
    assert outcome.report_body_sha256 and published[0].rstrip().endswith(outcome.report_body_sha256)


def test_ac1_pin_invalid_at_start_refuses_before_browser_turn() -> None:
    calls = {"turn": 0}

    def _turn() -> AcceptedAnswer:
        calls["turn"] += 1
        return ANSWER

    outcome = run_review(
        REQ,
        verify_pin_start=_bad_pin,
        verify_pin_before_publish=_ok_pin,
        browser_turn=_turn,
        publish=lambda b: "/never",
    )
    assert outcome.ok is False
    assert outcome.error_code is RunnerErrorCode.PIN_DRIFT
    assert calls["turn"] == 0  # never reached the browser turn


def test_ac2_pin_drift_before_publish_cancels_even_with_valid_bytes() -> None:
    published: list[str] = []
    outcome = run_review(
        REQ,
        verify_pin_start=_ok_pin,
        verify_pin_before_publish=_bad_pin,  # drifts after the turn
        browser_turn=lambda: ANSWER,
        publish=lambda b: (published.append(b), "/path")[1],
    )
    assert outcome.ok is False
    assert outcome.error_code is RunnerErrorCode.PIN_DRIFT
    assert published == []  # publication cancelled despite a valid answer


def test_ac7_browser_turn_failure_surfaces_partial_source_no_report() -> None:
    published: list[str] = []

    def _turn() -> AcceptedAnswer:
        err = RunnerError(
            RunnerErrorCode.SUBMIT_UNKNOWN,
            "GET failed while DOM held a full answer",
            delivery_state=DeliveryState.DELIVERED,
        )
        # The transport tags a partial source on the error for the envelope.
        err.partial_source = PartialSource.DOM  # type: ignore[attr-defined]
        raise err

    outcome = run_review(
        REQ,
        verify_pin_start=_ok_pin,
        verify_pin_before_publish=_ok_pin,
        browser_turn=_turn,
        publish=lambda b: (published.append(b), "/path")[1],
    )
    assert outcome.ok is False
    assert outcome.partial_source is PartialSource.DOM
    assert outcome.answer is None
    assert outcome.report_path is None
    assert published == []


def test_ac4_verdict_in_answer_refuses_publication() -> None:
    bad = AcceptedAnswer(
        text="Ruling: GATE-NO — this is bad",
        model_slug="gpt-5-6-thinking",
        thinking_effort="extended",
        conversation_id="11111111-2222-3333-4444-555555555555",
        assistant_node_id="asst-1",
    )
    published: list[str] = []
    outcome = run_review(
        REQ,
        verify_pin_start=_ok_pin,
        verify_pin_before_publish=_ok_pin,
        browser_turn=lambda: bad,
        publish=lambda b: (published.append(b), "/path")[1],
    )
    assert outcome.ok is False
    assert outcome.error_code is RunnerErrorCode.INVALID_VERDICT
    assert outcome.report_path is None
    assert published == []
