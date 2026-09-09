"""F862 (#718) — offline unit + fixture tests for the ChatGPT-web findings lane.

Every Acceptance Criterion from the blueprint that can be settled without a live
browser has a NAMED test here (``test_ac<N>_...``). Conversation-GET fixtures are
built from the shapes recorded in ``chatgpt-api-behavior.md`` §3, with all
secrets stripped (no cookie/bearer/sentinel value ever appears). No test touches
a live browser — the browser-driving code is imported but never executed; the
pure logic (poll-gate, submit classification, bounds, identity, publication,
read containment, condition mapping, the provider backstop) is exercised
directly, following grok-web's fake-clock / injectable pattern.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import (
    poll_gate,
    publication,
    snapshot_upload,
    submit_ids,
)
from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
    _scrub_hint,
    delivery_state_may_retry,
)
from cli_agent_orchestrator.chatgpt_web_runner.output import PartialSource, RunnerOutcome, Submitted
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
    REQUIRED_MODEL_SLUG,
    REQUIRED_THINKING_EFFORT,
    AcceptedAnswer,
    GatePending,
    evaluate_gate,
)
from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_LINES,
    AttachmentIdentity,
    build_attachment_identity,
    enforce_bundle_bounds,
    enforce_no_api_egress,
    enforce_read_allowed,
    is_enumeration_endpoint,
    is_same_origin_read_allowed,
    readiness_reached,
    verify_attachment_on_turn,
)
from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import (
    SubmitObservation,
    classify_delivery,
    extract_conversation_id,
    is_send_response_url,
    new_run_id,
)
from cli_agent_orchestrator.providers.condition import ConditionKind, classify_condition

pytestmark = pytest.mark.unit

RUN_ID = "run0abcdef"
BUNDLE_SHA = "deadbeef" * 8  # 64-hex placeholder
USER_MSG_ID = "user-msg-1"


# --------------------------------------------------------------------------
# Conversation-GET fixtures (secrets stripped; shape per findings §3)
# --------------------------------------------------------------------------
def _conv(
    *,
    current: str,
    mapping: dict,
    conversation_id: str = "11111111-2222-3333-4444-555555555555",
) -> dict:
    return {"conversation_id": conversation_id, "current_node": current, "mapping": mapping}


def _assistant_node(
    node_id: str,
    parent: str,
    text: str,
    *,
    end_turn=True,
    status="finished_successfully",
    model_slug=REQUIRED_MODEL_SLUG,
    effort=REQUIRED_THINKING_EFFORT,
    resolved=None,
    error=None,
    refusal=False,
) -> dict:
    meta = {"model_slug": model_slug, "thinking_effort": effort}
    if resolved is not None:
        meta["resolved_model_slug"] = resolved
    if refusal:
        meta["is_refusal"] = True
    return {
        node_id: {
            "parent": parent,
            "message": {
                "id": node_id,
                "author": {"role": "assistant"},
                "status": status,
                "end_turn": end_turn,
                "error": error,
                "metadata": meta,
                "content": {"content_type": "text", "parts": [text]},
            },
        }
    }


def _user_node(node_id: str = USER_MSG_ID, parent: str = "root") -> dict:
    return {
        node_id: {
            "parent": parent,
            "message": {
                "id": node_id,
                "author": {"role": "user"},
                "status": "finished_successfully",
                "end_turn": True,
                "content": {"content_type": "text", "parts": ["review this"]},
            },
        }
    }


def _accepted_body(text: str) -> str:
    return f"{text}\nEND_REVIEW:{RUN_ID}:{BUNDLE_SHA}"


# ==========================================================================
# AC-5 — a completed run's accepted node satisfies all conjuncts
# ==========================================================================
def test_ac5_accepts_current_new_final_assistant_node() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(_assistant_node("asst-1", USER_MSG_ID, _accepted_body("finding one")))
    conv = _conv(current="asst-1", mapping=mapping)
    result = evaluate_gate(
        conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA
    )
    assert isinstance(result, AcceptedAnswer)
    assert result.text == "finding one"
    assert result.model_slug == REQUIRED_MODEL_SLUG
    assert result.thinking_effort == REQUIRED_THINKING_EFFORT


def test_ac5_missing_sentinel_is_truncated() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(_assistant_node("asst-1", USER_MSG_ID, "finding without sentinel"))
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.TRUNCATED_ANSWER


def test_ac5_two_sentinels_is_invalid_verdict() -> None:
    body = f"a\nEND_REVIEW:{RUN_ID}:{BUNDLE_SHA}\nb\nEND_REVIEW:{RUN_ID}:{BUNDLE_SHA}"
    mapping = {}
    mapping.update(_user_node())
    mapping.update(_assistant_node("asst-1", USER_MSG_ID, body))
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.INVALID_VERDICT


# ==========================================================================
# AC-6 — stale historical node, sibling branch, end_turn/status disagreement
# ==========================================================================
def test_ac6_node_not_descending_from_submit_is_error() -> None:
    # Current assistant node descends from a DIFFERENT user turn (sibling branch).
    mapping = {}
    mapping.update(_user_node("other-user", parent="root"))
    mapping.update(_assistant_node("asst-x", "other-user", _accepted_body("stale")))
    conv = _conv(current="asst-x", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.UI_CHANGED


def test_ac6_end_turn_status_disagreement_is_typed_error() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(
        _assistant_node(
            "asst-1", USER_MSG_ID, _accepted_body("x"), end_turn=True, status="in_progress"
        )
    )
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.TRUNCATED_ANSWER


def test_ac6_still_working_returns_pending() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(
        _assistant_node("asst-1", USER_MSG_ID, "partial", end_turn=False, status="in_progress")
    )
    conv = _conv(current="asst-1", mapping=mapping)
    result = evaluate_gate(
        conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA
    )
    assert isinstance(result, GatePending)
    assert result.partial_text == "partial"


def test_ac6_empty_assistant_node_finished_status_is_pending_not_disagreement() -> None:
    # F862 r2 live finding: mid-stream, an assistant node appears with
    # status=finished_successfully but end_turn=False AND EMPTY content (the node
    # is armed, the model is still emitting). This is still-working, NOT the D6
    # disagreement error (which is only for a CONTENT-bearing node whose two
    # terminal flags conflict).
    mapping = {}
    mapping.update(_user_node())
    mapping.update(
        _assistant_node("asst-1", USER_MSG_ID, "", end_turn=False, status="finished_successfully")
    )
    conv = _conv(current="asst-1", mapping=mapping)
    result = evaluate_gate(
        conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA
    )
    assert isinstance(result, GatePending)


def test_ac6_content_bearing_disagreement_still_errors() -> None:
    # The D6 typed error is the DANGEROUS direction: end_turn claims the turn is
    # done but status does NOT confirm success. (end_turn=False is just still-
    # streaming — F862 r2 — and is pending, not a disagreement.)
    mapping = {}
    mapping.update(_user_node())
    mapping.update(
        _assistant_node(
            "asst-1",
            USER_MSG_ID,
            _accepted_body("real content"),
            end_turn=True,
            status="in_progress",
        )
    )
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.TRUNCATED_ANSWER


def test_ac6_end_turn_false_finished_status_content_is_pending() -> None:
    # F862 r2: status flips to finished_successfully on a still-GROWING node
    # before end_turn is set. With end_turn False this is streaming -> pending,
    # NOT a disagreement (the earlier over-strict check failed run c live).
    mapping = {}
    mapping.update(_user_node())
    mapping.update(
        _assistant_node(
            "asst-1",
            USER_MSG_ID,
            "partial content so far",
            end_turn=False,
            status="finished_successfully",
        )
    )
    conv = _conv(current="asst-1", mapping=mapping)
    result = evaluate_gate(
        conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA
    )
    assert isinstance(result, GatePending)
    assert result.partial_text == "partial content so far"


def test_ac5_model_drift_rejected() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(
        _assistant_node("asst-1", USER_MSG_ID, _accepted_body("x"), model_slug="gpt-5-6-instant")
    )
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.MODEL_DRIFT


def test_ac5_effort_drift_rejected() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(_assistant_node("asst-1", USER_MSG_ID, _accepted_body("x"), effort="standard"))
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.MODEL_DRIFT


def test_ac6_non_text_content_part_is_error() -> None:
    mapping = {}
    mapping.update(_user_node())
    node = _assistant_node("asst-1", USER_MSG_ID, "x")
    node["asst-1"]["message"]["content"]["parts"] = ["ok text", {"image": "..."}]
    node["asst-1"]["message"]["content"]["parts"].append(_accepted_body(""))
    mapping.update(node)
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.UI_CHANGED


def test_ac6_refusal_state_rejected() -> None:
    mapping = {}
    mapping.update(_user_node())
    mapping.update(_assistant_node("asst-1", USER_MSG_ID, _accepted_body("x"), refusal=True))
    conv = _conv(current="asst-1", mapping=mapping)
    with pytest.raises(RunnerError) as ei:
        evaluate_gate(conv, submitted_user_msg_id=USER_MSG_ID, run_id=RUN_ID, bundle_sha=BUNDLE_SHA)
    assert ei.value.code is RunnerErrorCode.TRUNCATED_ANSWER


# ==========================================================================
# AC-4 — a gate ruling token is an invalid findings artifact
# ==========================================================================
@pytest.mark.parametrize("token", ["GATE-YES", "GATE-NO", "Ruling: PASS", "Verdict: FAIL"])
def test_ac4_gate_token_rejected(token: str) -> None:
    body = f"Some findings.\n{token}\nmore text"
    with pytest.raises(RunnerError) as ei:
        publication.assert_no_gate_tokens(body)
    assert ei.value.code is RunnerErrorCode.INVALID_VERDICT


def test_ac4_clean_findings_body_accepted() -> None:
    publication.assert_no_gate_tokens("Finding 1: section 3 should say X instead of Y.")


def test_ac1_report_carries_findings_status_and_body_digest() -> None:
    # AC-1: FINDINGS-READY report whose body digest matches its published trailer.
    report = publication.build_report(
        body_markdown="Finding 1: blueprint D3 line 4 should read 'foo'.",
        artifact_path="/data/cao-scratch/briefs/x.md",
        artifact_sha256="a" * 64,
        bundle_sha256=BUNDLE_SHA,
        model_slug=REQUIRED_MODEL_SLUG,
        thinking_effort=REQUIRED_THINKING_EFFORT,
        run_id=RUN_ID,
    )
    assert report.body.startswith("Status: FINDINGS-READY")
    assert report.body.rstrip().endswith(report.body_sha256)
    # The digest is stable: recomputing over the body (trailer elided) matches.
    assert publication.canonical_body_digest(report.body) == report.body_sha256


def test_ac4_build_report_rejects_verdict_body() -> None:
    with pytest.raises(RunnerError) as ei:
        publication.build_report(
            body_markdown="Ruling: GATE-NO",
            artifact_path="/x",
            artifact_sha256="a" * 64,
            bundle_sha256=BUNDLE_SHA,
            model_slug=REQUIRED_MODEL_SLUG,
            thinking_effort=REQUIRED_THINKING_EFFORT,
            run_id=RUN_ID,
        )
    assert ei.value.code is RunnerErrorCode.INVALID_VERDICT


# ==========================================================================
# AC-8 — the four delivery states and their resend contract
# ==========================================================================
def test_ac8_nothing_sent_before_dispatch_is_retryable() -> None:
    obs = SubmitObservation(enter_dispatched=False)
    assert classify_delivery(obs) is DeliveryState.NOTHING_SENT
    assert delivery_state_may_retry(DeliveryState.NOTHING_SENT) is True


def test_ac8_pre_validation_failure_is_nothing_sent() -> None:
    obs = SubmitObservation(enter_dispatched=True, pre_validation_failed=True)
    assert classify_delivery(obs) is DeliveryState.NOTHING_SENT


def test_ac8_delivered_never_resends() -> None:
    obs = SubmitObservation(enter_dispatched=True, new_user_turn=True)
    assert classify_delivery(obs) is DeliveryState.DELIVERED
    assert delivery_state_may_retry(DeliveryState.DELIVERED) is False


def test_ac8_ack_unknown_never_resends() -> None:
    obs = SubmitObservation(enter_dispatched=True, composer_cleared=True)
    assert classify_delivery(obs) is DeliveryState.ACK_UNKNOWN
    assert delivery_state_may_retry(DeliveryState.ACK_UNKNOWN) is False


def test_ac8_timeout_carries_last_state() -> None:
    obs = SubmitObservation(enter_dispatched=True, deadline_exhausted=True)
    assert classify_delivery(obs) is DeliveryState.TIMEOUT
    assert delivery_state_may_retry(DeliveryState.TIMEOUT) is False


def test_ac8_delivered_by_send_response_or_conv_id() -> None:
    assert (
        classify_delivery(SubmitObservation(enter_dispatched=True, send_response_seen=True))
        is DeliveryState.DELIVERED
    )
    assert (
        classify_delivery(
            SubmitObservation(
                enter_dispatched=True, conversation_id="11111111-2222-3333-4444-555555555555"
            )
        )
        is DeliveryState.DELIVERED
    )


def test_send_response_url_excludes_prepare() -> None:
    assert is_send_response_url("https://chatgpt.com/backend-api/f/conversation")
    assert not is_send_response_url("https://chatgpt.com/backend-api/f/conversation/prepare")


def test_conversation_id_regex() -> None:
    assert (
        extract_conversation_id("https://chatgpt.com/c/11111111-2222-3333-4444-555555555555")
        == "11111111-2222-3333-4444-555555555555"
    )
    # F862 live finding: the FRONT-END route prefixes the id with "WEB:", but the
    # BACKEND path keys on the BARE uuid — a prefixed id 429s on the backend
    # (F862 r2 probe). So extraction returns the bare uuid.
    assert (
        extract_conversation_id("https://chatgpt.com/c/WEB:57998290-2fb2-4223-85bf-948330bc94ab")
        == "57998290-2fb2-4223-85bf-948330bc94ab"
    )
    assert extract_conversation_id("https://chatgpt.com/") is None
    assert len(new_run_id()) == 32


# ==========================================================================
# AC-10 — over-limit bundle refused, no split/truncate
# ==========================================================================
def test_ac10_over_byte_limit_refused() -> None:
    data = b"x" * (MAX_BUNDLE_BYTES + 1)
    with pytest.raises(RunnerError) as ei:
        enforce_bundle_bounds(data)
    assert ei.value.code is RunnerErrorCode.CONTEXT_TOO_LARGE


def test_ac10_over_line_limit_refused() -> None:
    data = ("a\n" * (MAX_BUNDLE_LINES + 1)).encode("utf-8")
    with pytest.raises(RunnerError) as ei:
        enforce_bundle_bounds(data)
    assert ei.value.code is RunnerErrorCode.CONTEXT_TOO_LARGE


def test_ac10_at_limit_accepted() -> None:
    data = b"a" * MAX_BUNDLE_BYTES
    enforce_bundle_bounds(data)  # no raise


# ==========================================================================
# AC-9 — attachment identity + readiness
# ==========================================================================
def test_ac9_readiness_needs_both_calibrated_signals() -> None:
    assert readiness_reached({"filename_chip", "document_label"}) is True
    assert readiness_reached({"filename_chip"}) is False
    # A spinner/percentage is NOT a permitted signal.
    assert readiness_reached({"filename_chip", "spinner"}) is False


def test_ac9_attachment_identity_mismatch_rejected() -> None:
    manifest = build_attachment_identity(b"hello\nworld\n", "bundle.txt")
    tampered = build_attachment_identity(b"hello\nWORLD\n", "bundle.txt")
    with pytest.raises(RunnerError) as ei:
        verify_attachment_on_turn(manifest, tampered, attachment_count=1)
    assert ei.value.code is RunnerErrorCode.ATTACHMENT_IDENTITY


def test_ac9_more_than_one_attachment_rejected() -> None:
    manifest = build_attachment_identity(b"hello\n", "bundle.txt")
    with pytest.raises(RunnerError) as ei:
        verify_attachment_on_turn(manifest, manifest, attachment_count=2)
    assert ei.value.code is RunnerErrorCode.ATTACHMENT_IDENTITY


def test_ac9_matching_identity_single_attachment_ok() -> None:
    manifest = build_attachment_identity(b"hello\nworld\n", "bundle.txt")
    verify_attachment_on_turn(manifest, manifest, attachment_count=1)  # no raise


# ==========================================================================
# AC-11 / AC-11b — egress + read containment
# ==========================================================================
def test_ac11_api_egress_refused() -> None:
    with pytest.raises(RunnerError) as ei:
        enforce_no_api_egress("https://api.openai.com/v1/chat/completions")
    assert ei.value.code is RunnerErrorCode.EGRESS_FORBIDDEN


def test_ac11b_foreign_conversation_id_refused() -> None:
    owned = "11111111-2222-3333-4444-555555555555"
    foreign = "https://chatgpt.com/backend-api/conversation/99999999-0000-0000-0000-000000000000"
    assert is_same_origin_read_allowed(foreign, owned) is False
    with pytest.raises(RunnerError) as ei:
        enforce_read_allowed(foreign, owned)
    assert ei.value.code is RunnerErrorCode.READ_FORBIDDEN


def test_ac11b_owned_conversation_and_auth_session_allowed() -> None:
    owned = "11111111-2222-3333-4444-555555555555"
    assert is_same_origin_read_allowed(
        f"https://chatgpt.com/backend-api/conversation/{owned}", owned
    )
    assert is_same_origin_read_allowed("https://chatgpt.com/api/auth/session", owned)


def test_ac11b_bare_uuid_owned_conversation_allowed() -> None:
    # F862 r2: the runner keys on the BARE uuid (front-end WEB: prefix is stripped
    # at extraction), and the backend GET path uses that bare id.
    owned = "57998290-2fb2-4223-85bf-948330bc94ab"
    assert is_same_origin_read_allowed(
        f"https://chatgpt.com/backend-api/conversation/{owned}", owned
    )
    # A DIFFERENT conversation id is still refused.
    other = "https://chatgpt.com/backend-api/conversation/00000000-0000-0000-0000-000000000000"
    assert is_same_origin_read_allowed(other, owned) is False


def test_ac11b_off_origin_refused() -> None:
    owned = "11111111-2222-3333-4444-555555555555"
    assert (
        is_same_origin_read_allowed(f"https://evil.example/backend-api/conversation/{owned}", owned)
        is False
    )


def test_ac11b_enumeration_endpoint_refused() -> None:
    owned = "11111111-2222-3333-4444-555555555555"
    assert is_enumeration_endpoint("https://chatgpt.com/backend-api/conversations")
    with pytest.raises(RunnerError):
        enforce_read_allowed("https://chatgpt.com/backend-api/conversations?offset=0", owned)


# ==========================================================================
# AC-11 — no secret ever appears in a hint / envelope
# ==========================================================================
@pytest.mark.parametrize(
    "raw",
    [
        "authorization: Bearer eyJhbGciOi.something",
        "Cookie: __Secure-next-auth.session-token=abc",
        "accessToken=eyJ0eXAiOiJKV1Q",
    ],
)
def test_ac11_secret_hint_scrubbed(raw: str) -> None:
    scrubbed = _scrub_hint(raw)
    assert "redacted" in scrubbed
    assert "eyJ" not in scrubbed and "Bearer " not in scrubbed


def test_ac11_envelope_has_no_secret_fields() -> None:
    outcome = RunnerOutcome(
        ok=False,
        error_code=RunnerErrorCode.AUTH_WALL,
        hint="auth wall",
        delivery_state=DeliveryState.NOTHING_SENT,
        submitted=Submitted.FALSE,
        conversation_url="https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
        bundle_sha256=BUNDLE_SHA,
    )
    env = outcome.to_envelope()
    keys = set(env)
    for forbidden in ("authorization", "cookie", "bearer", "sentinel", "accessToken"):
        assert forbidden not in keys
    assert env["error_code"] == "auth_wall"
    assert env["submitted"] == "false"


# ==========================================================================
# AC-7 — partial-source semantics on the envelope (blinded SSE / GET-fail)
# ==========================================================================
def test_ac7_partial_source_dom_carries_no_accepted_answer() -> None:
    outcome = RunnerOutcome(
        ok=False,
        error_code=RunnerErrorCode.SUBMIT_UNKNOWN,
        delivery_state=DeliveryState.DELIVERED,
        partial_source=PartialSource.DOM,
        answer=None,
    )
    env = outcome.to_envelope()
    assert env["partial_source"] == "dom"
    assert env["answer"] is None
    assert env["ok"] is False


# ==========================================================================
# AC-14 — three-way discrimination of 401 / unclassifiable 403 / bot flag
# (via the condition-marker classifier — keeps auth expiry from being a bot flag)
# ==========================================================================
def test_ac14_auth_wall_maps_to_auth_expired() -> None:
    cond = classify_condition("[chatgpt_web] CONDITION auth_wall", "chatgpt_web")
    assert cond is not None and cond.kind is ConditionKind.AUTH_EXPIRED


def test_ac14_bot_flagged_maps_to_dialog_blocked_subtype() -> None:
    cond = classify_condition("[chatgpt_web] CONDITION bot_flagged", "chatgpt_web")
    assert cond is not None
    assert cond.kind is ConditionKind.DIALOG_BLOCKED
    assert cond.subtype == "bot_flagged"


def test_ac14_unclassifiable_403_is_access_denied_dialog_blocked() -> None:
    cond = classify_condition("[chatgpt_web] CONDITION access_denied", "chatgpt_web")
    assert cond is not None
    assert cond.kind is ConditionKind.DIALOG_BLOCKED
    assert cond.subtype == "access_denied"


def test_ac14_quota_maps_to_capped_with_reset_hint() -> None:
    cond = classify_condition(
        "[chatgpt_web] CONDITION quota reset=2026-09-10T00:00Z", "chatgpt_web"
    )
    assert cond is not None
    assert cond.kind is ConditionKind.CAPPED
    assert cond.reset_hint == "2026-09-10T00:00Z"


def test_condition_ordinary_error_code_maps_to_no_condition() -> None:
    # ui_changed / model_drift / etc. are plain ERROR — no condition.
    for code in (
        "ui_changed",
        "model_drift",
        "truncated_answer",
        "invalid_verdict",
        "submit_unknown",
    ):
        assert classify_condition(f"[chatgpt_web] CONDITION {code}", "chatgpt_web") is None


def test_condition_net_interrupted_and_context_and_proc() -> None:
    assert (
        classify_condition("[chatgpt_web] CONDITION net_interrupted", "chatgpt_web").kind
        is ConditionKind.NET_INTERRUPTED
    )
    assert (
        classify_condition("[chatgpt_web] CONDITION context_too_large", "chatgpt_web").kind
        is ConditionKind.CONTEXT_EXHAUSTED
    )
    assert (
        classify_condition("[chatgpt_web] CONDITION proc_exited", "chatgpt_web").kind
        is ConditionKind.PROC_EXITED
    )


def test_condition_marker_only_for_chatgpt_web_provider() -> None:
    # The marker must not fire for another provider's pane.
    assert classify_condition("[chatgpt_web] CONDITION auth_wall", "codex") is None
