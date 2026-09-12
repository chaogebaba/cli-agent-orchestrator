"""Completion decision from the conversation GET (D6) — the binary done-gate.

Pure logic over a parsed conversation-GET JSON body (the shape recorded in
findings §3). No browser import: the transport module fetches the JSON in-page
and hands the dict here, so AC-5/AC-6/AC-7 are testable against recorded
fixtures with secrets stripped.

A candidate is COMPLETE only when it is the current, new, final assistant
message descended from the uniquely matching submitted user turn, with
``message.end_turn is True`` AND ``message.status == 'finished_successfully'``,
valid content, no error/refusal/truncation, ``metadata.model_slug ==
gpt-5-6-thinking``, ``metadata.thinking_effort == extended``, and exactly one
terminal ``END_REVIEW:<run-id>:<bundle-sha>`` sentinel (stripped as framing
before schema validation).

The extraction rule (D6, r1 §4): take the node's content parts IN ORDER and
preserve text/code/table bytes; an unsupported non-text part is a typed error,
never stringified into the answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

#: The model/effort this lane is certified to read (findings §1, D6).
REQUIRED_MODEL_SLUG = "gpt-5-6-thinking"
REQUIRED_THINKING_EFFORT = "extended"


_VOLATILE_FIELDS = {
    "create_time",
    "update_time",
    "request_id",
    "request_time",
    "telemetry",
    "timing",
}


def canonical_conversation_digest(body: dict[str, Any]) -> str:
    """Hash the authoritative conversation while ignoring presentation noise.

    Branch identity is retained: conversation/current node, mapping ids and
    parent/child edges, role/content parts, status/end-turn, and tool result
    payloads (including their result digests) all survive canonicalisation.
    Volatile request timestamps/telemetry are omitted recursively. Deterministic
    key and mapping ordering makes page-reload parity comparable to detached GET.
    """

    def clean(value: Any, *, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                str(k): clean(v, key=str(k))
                for k, v in sorted(value.items(), key=lambda item: str(item[0]))
                if str(k).lower() not in _VOLATILE_FIELDS
            }
        if isinstance(value, list):
            # Mapping order is semantically irrelevant; all other arrays (parts,
            # children) retain order because it affects rendered branch meaning.
            if key == "mapping":
                return [clean(value_item) for value_item in value]
            return [clean(item) for item in value]
        return value

    canonical = clean(body)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: Terminal sentinel framing the model must emit exactly once (D6).
#: ``END_REVIEW:<run-id>:<bundle-sha>``.
_END_REVIEW_RE_TMPL = r"END_REVIEW:{run_id}:{bundle_sha}"


class GateOutcome(str):
    """Marker for the three gate results; kept as plain strings for clarity."""


@dataclass(frozen=True)
class AcceptedAnswer:
    """A validated, accepted assistant answer with framing stripped."""

    text: str
    model_slug: str
    thinking_effort: str
    conversation_id: str
    assistant_node_id: str


@dataclass(frozen=True)
class GatePending:
    """Not yet complete — keep polling. Carries a diagnostic partial, if any."""

    partial_text: Optional[str] = None


def _node_message(conv: dict[str, Any], node_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not node_id:
        return None
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return None
    node = mapping.get(node_id)
    if not isinstance(node, dict):
        return None
    msg = node.get("message")
    return msg if isinstance(msg, dict) else None


def _parent_chain_contains(
    conv: dict[str, Any], start_node_id: Optional[str], target_user_msg_id: str
) -> bool:
    """Walk parent links from ``start_node_id`` up; True if it descends from the
    node whose message id is ``target_user_msg_id`` (turn-identity check, D6)."""
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return False
    seen: set[str] = set()
    cur = start_node_id
    while cur and cur not in seen:
        seen.add(cur)
        node = mapping.get(cur)
        if not isinstance(node, dict):
            return False
        msg = node.get("message")
        if isinstance(msg, dict) and msg.get("id") == target_user_msg_id:
            return True
        cur = node.get("parent")
    return False


def extract_answer_text(message: dict[str, Any]) -> str:
    """Extract the ordered content-part bytes (D6 extraction rule).

    Accepts text, code and table parts and preserves their bytes in order. A
    non-text part (an image, an attachment reference, a tool payload) is a typed
    ``ui_changed`` error — NEVER stringified into the answer.
    """
    content = message.get("content")
    if not isinstance(content, dict):
        raise RunnerError(
            RunnerErrorCode.UI_CHANGED,
            "assistant node has no content object",
            delivery_state=DeliveryState.DELIVERED,
        )
    content_type = content.get("content_type")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise RunnerError(
            RunnerErrorCode.UI_CHANGED,
            f"content.parts is not a list (content_type={content_type!r})",
            delivery_state=DeliveryState.DELIVERED,
        )
    pieces: list[str] = []
    for part in parts:
        if isinstance(part, str):
            pieces.append(part)
        else:
            # An unsupported non-text part (multimodal / structured). Never
            # coerce it — a review body must be text/code/table bytes only.
            raise RunnerError(
                RunnerErrorCode.UI_CHANGED,
                "assistant answer contains a non-text content part",
                delivery_state=DeliveryState.DELIVERED,
            )
    return "".join(pieces)


def strip_sentinel(text: str, run_id: str, bundle_sha: str) -> str:
    """Verify exactly ONE terminal END_REVIEW sentinel and strip it (D6).

    Raises ``truncated_answer`` when the sentinel is absent (the answer did not
    finish the protocol) and ``invalid_verdict`` when more than one appears.
    """
    pattern = re.compile(
        _END_REVIEW_RE_TMPL.format(run_id=re.escape(run_id), bundle_sha=re.escape(bundle_sha))
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise RunnerError(
            RunnerErrorCode.TRUNCATED_ANSWER,
            "terminal END_REVIEW sentinel missing",
            delivery_state=DeliveryState.DELIVERED,
        )
    if len(matches) > 1:
        raise RunnerError(
            RunnerErrorCode.INVALID_VERDICT,
            "more than one END_REVIEW sentinel present",
            delivery_state=DeliveryState.DELIVERED,
        )
    last = matches[-1]
    # The sentinel must be terminal: only whitespace may follow it.
    if text[last.end() :].strip():
        raise RunnerError(
            RunnerErrorCode.INVALID_VERDICT,
            "content follows the terminal END_REVIEW sentinel",
            delivery_state=DeliveryState.DELIVERED,
        )
    return text[: last.start()].rstrip()


def evaluate_gate(
    conv: dict[str, Any],
    *,
    submitted_user_msg_id: str,
    run_id: str,
    bundle_sha: str,
) -> "AcceptedAnswer | GatePending":
    """Evaluate the binary done-gate over a conversation-GET body (D6).

    Returns :class:`AcceptedAnswer` when the current node is a complete, valid,
    correctly-attributed assistant answer; :class:`GatePending` while still
    streaming/incomplete; and raises a typed :class:`RunnerError` for a
    definitively bad terminal state (AC-6: stale historical node, sibling
    branch, end_turn/status disagreement, wrong model/effort).
    """
    conversation_id = str(conv.get("conversation_id") or "")
    current = conv.get("current_node")
    msg = _node_message(conv, current if isinstance(current, str) else None)
    if msg is None:
        return GatePending()

    role = (msg.get("author") or {}).get("role") if isinstance(msg.get("author"), dict) else None
    status = msg.get("status")
    end_turn = msg.get("end_turn")
    raw_meta = msg.get("metadata")
    metadata: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}

    if role != "assistant":
        # The current node is still the user turn or a tool node — keep polling.
        return GatePending()

    # Turn identity: the accepted node MUST descend from the submitted user turn
    # (D6: "turn identity, not finishedness"). A finished historical node or a
    # sibling branch that does NOT descend from our submit is rejected.
    if not _parent_chain_contains(
        conv, current if isinstance(current, str) else None, submitted_user_msg_id
    ):
        raise RunnerError(
            RunnerErrorCode.UI_CHANGED,
            "current assistant node does not descend from the submitted user turn",
            delivery_state=DeliveryState.DELIVERED,
        )

    # Extract the answer bytes first. An assistant node with NO content yet is a
    # mid-stream state (the node exists but the model is still thinking/emitting),
    # regardless of a transient status flag — treat it as still-working (D6:
    # "turn identity, not finishedness"), NOT a disagreement.
    try:
        raw_text = extract_answer_text(msg)
    except RunnerError:
        # A non-text part on a not-yet-complete node is a transient stream frame,
        # not a final answer — keep polling rather than failing on a partial.
        if end_turn is not True:
            return GatePending()
        raise

    finished_status = status == "finished_successfully"
    # Completion is gated on end_turn (D6: "turn identity, not finishedness").
    # While end_turn is not yet True the turn is STILL STREAMING — regardless of
    # a transient status flag or partial content (F862 r2 live: status flips to
    # finished_successfully on a growing node before end_turn is set). Carry the
    # partial and keep polling; never a disagreement error in this direction.
    if end_turn is not True:
        return GatePending(partial_text=raw_text or None)

    # end_turn IS True. Now the status MUST also be finished_successfully; a
    # conflict here is the D6 typed error (claiming a finished turn the status
    # does not confirm — the dangerous direction).
    if not finished_status:
        raise RunnerError(
            RunnerErrorCode.TRUNCATED_ANSWER,
            f"end_turn/status disagreement (end_turn={end_turn!r}, status={status!r})",
            delivery_state=DeliveryState.DELIVERED,
        )

    # Refusal / error state on the node.
    if msg.get("error") or metadata.get("is_refusal") is True:
        raise RunnerError(
            RunnerErrorCode.TRUNCATED_ANSWER,
            "assistant node carries an error or refusal state",
            delivery_state=DeliveryState.DELIVERED,
        )

    # A finished (end_turn True) node with no text is a truncated answer.
    if not raw_text.strip():
        raise RunnerError(
            RunnerErrorCode.TRUNCATED_ANSWER,
            "accepted node produced empty answer text",
            delivery_state=DeliveryState.DELIVERED,
        )

    # Model / effort verification from AUTHORITATIVE node metadata (D6).
    model_slug = metadata.get("model_slug")
    resolved_model = metadata.get("resolved_model_slug")
    effort = metadata.get("thinking_effort")
    if not model_slug:
        raise RunnerError(
            RunnerErrorCode.MODEL_DRIFT,
            "assistant node metadata carries no model_slug",
            delivery_state=DeliveryState.DELIVERED,
        )
    if model_slug != REQUIRED_MODEL_SLUG or (
        resolved_model and resolved_model != REQUIRED_MODEL_SLUG
    ):
        raise RunnerError(
            RunnerErrorCode.MODEL_DRIFT,
            f"model drift: got {model_slug!r}/{resolved_model!r}, want {REQUIRED_MODEL_SLUG!r}",
            delivery_state=DeliveryState.DELIVERED,
        )
    if effort != REQUIRED_THINKING_EFFORT:
        raise RunnerError(
            RunnerErrorCode.MODEL_DRIFT,
            f"effort drift: got {effort!r}, want {REQUIRED_THINKING_EFFORT!r}",
            delivery_state=DeliveryState.DELIVERED,
        )

    stripped = strip_sentinel(raw_text, run_id, bundle_sha)

    return AcceptedAnswer(
        text=stripped,
        model_slug=str(model_slug),
        thinking_effort=str(effort),
        conversation_id=conversation_id,
        assistant_node_id=str(current),
    )
