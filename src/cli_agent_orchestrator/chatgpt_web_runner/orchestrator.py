"""Run orchestration: the anchor sequence, with injected seams (D2).

Ties the runner modules together into ONE review run and enforces the D2 anchor
sequence: verify_pin (start) -> browser turn -> validate -> publish -> verify_pin
(again, immediately before publication) -> worker-scoped send_message. The
browser turn and the pin/publish effects are INJECTED callables so the sequence
is unit-testable offline (grok-web's injectable pattern) and the module carries
no browser import.

This is where AC-1 (verify_pin VALID at start and before publication), AC-2 (a pin
that drifts between the two checks cancels publication with a typed error), and
AC-7 (GET-decided completion; a partial rides partial_source with no accepted
report) are enforced as sequencing, independent of which browser executed the
turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.output import (
    PartialSource,
    RunnerOutcome,
    Submitted,
)
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import AcceptedAnswer
from cli_agent_orchestrator.chatgpt_web_runner.publication import PublishedReport, build_report

#: A pin check returns True (VALID) or False (drifted/superseded). The runner
#: verifies via the worker-scoped verify_pin MCP tool; the seam is injected so
#: tests supply a fake that flips between the two calls (AC-2).
PinCheck = Callable[[], bool]

#: The browser turn seam: returns an AcceptedAnswer on success, or raises a
#: typed RunnerError. Injected so the offline tests supply a canned answer or a
#: canned failure without a browser (AC-7).
BrowserTurn = Callable[[], AcceptedAnswer]

#: The publish seam: write the report body to disk and return its path. Injected
#: so tests capture the bytes without touching a real artifact tree.
PublishSink = Callable[[str], str]


@dataclass(frozen=True)
class ReviewRequest:
    artifact_path: str
    artifact_sha256: str
    bundle_sha256: str
    run_id: str


def run_review(
    request: ReviewRequest,
    *,
    verify_pin_start: PinCheck,
    verify_pin_before_publish: PinCheck,
    browser_turn: BrowserTurn,
    publish: PublishSink,
    now: Callable[[], float] = time.monotonic,
) -> RunnerOutcome:
    """Execute one review run under the D2 anchor sequence.

    Ordering, enforced here:
      1. verify_pin (start) — a drift here refuses BEFORE any browser turn.
      2. browser turn — returns an AcceptedAnswer or raises a typed RunnerError
         (a partial is surfaced as a RunnerError carrying partial_source; see
         the transport layer). No accepted report is produced on a raise.
      3. build the FINDINGS-READY report (validates no-verdict, computes digest).
      4. verify_pin (again) — a drift here CANCELS publication with pin_drift,
         even though the report bytes are already built (AC-2).
      5. publish, then the caller performs the worker-scoped send_message.
    """
    started = now()

    if not verify_pin_start():
        return _fail(
            RunnerErrorCode.PIN_DRIFT,
            "pin invalid at task start",
            started,
            now,
            request,
            delivery_state=DeliveryState.NOTHING_SENT,
            submitted=Submitted.FALSE,
        )

    try:
        answer = browser_turn()
    except RunnerError as exc:
        return _fail(
            exc.code,
            exc.hint,
            started,
            now,
            request,
            delivery_state=exc.delivery_state,
            submitted=_submitted_for(exc.delivery_state),
            partial_source=_partial_for(exc),
            conversation_url=_conv_url_for(exc),
        )

    try:
        report: PublishedReport = build_report(
            body_markdown=answer.text,
            artifact_path=request.artifact_path,
            artifact_sha256=request.artifact_sha256,
            bundle_sha256=request.bundle_sha256,
            model_slug=answer.model_slug,
            thinking_effort=answer.thinking_effort,
            run_id=request.run_id,
        )
    except RunnerError as exc:
        # A gate token in the answer (AC-4) or an empty body: no accepted report.
        return _fail(
            exc.code,
            exc.hint,
            started,
            now,
            request,
            delivery_state=DeliveryState.DELIVERED,
            submitted=Submitted.TRUE,
        )

    # AC-2: the second pin check happens AFTER the report is built but BEFORE it
    # is published. A drift here cancels publication with a typed error.
    if not verify_pin_before_publish():
        return _fail(
            RunnerErrorCode.PIN_DRIFT,
            "pin drifted or was superseded before publication",
            started,
            now,
            request,
            delivery_state=DeliveryState.DELIVERED,
            submitted=Submitted.TRUE,
        )

    report_path = publish(report.body)
    conv_url = f"https://chatgpt.com/c/{answer.conversation_id}" if answer.conversation_id else None
    return RunnerOutcome(
        ok=True,
        delivery_state=DeliveryState.DELIVERED,
        submitted=Submitted.TRUE,
        conversation_url=conv_url,
        elapsed_ms=int((now() - started) * 1000),
        model_slug=answer.model_slug,
        thinking_effort=answer.thinking_effort,
        bundle_sha256=request.bundle_sha256,
        answer=answer.text,
        report_path=report_path,
        report_body_sha256=report.body_sha256,
    )


def _submitted_for(state: DeliveryState) -> Submitted:
    if state is DeliveryState.DELIVERED:
        return Submitted.TRUE
    if state is DeliveryState.NOTHING_SENT:
        return Submitted.FALSE
    return Submitted.UNKNOWN


def _partial_for(exc: RunnerError) -> Optional[PartialSource]:
    src = getattr(exc, "partial_source", None)
    return src if isinstance(src, PartialSource) else None


def _conv_url_for(exc: RunnerError) -> Optional[str]:
    return getattr(exc, "conversation_url", None)


def _fail(
    code: RunnerErrorCode,
    hint: str,
    started: float,
    now: Callable[[], float],
    request: ReviewRequest,
    *,
    delivery_state: DeliveryState,
    submitted: Submitted,
    partial_source: Optional[PartialSource] = None,
    conversation_url: Optional[str] = None,
) -> RunnerOutcome:
    return RunnerOutcome(
        ok=False,
        error_code=code,
        hint=hint,
        delivery_state=delivery_state,
        submitted=submitted,
        partial_source=partial_source,
        conversation_url=conversation_url,
        elapsed_ms=int((now() - started) * 1000),
        bundle_sha256=request.bundle_sha256,
    )
