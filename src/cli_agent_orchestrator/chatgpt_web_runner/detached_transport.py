"""Detached authoritative conversation GET and ack-unknown recovery (F862 D6).

This module never lists conversations and never sends a conversation POST. It
uses only the owned conversation id captured before/at the mint, polls no faster
than the inherited 3s→10s cadence (honouring Retry-After), and treats absence as
an observation rather than proof of non-delivery.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
    AcceptedAnswer,
    GatePending,
    canonical_conversation_digest,
    evaluate_gate,
)


@dataclass(frozen=True)
class DetachedGetResult:
    status_code: int
    body: Optional[dict[str, Any]]
    retry_after: Optional[float] = None


@dataclass(frozen=True)
class AckReconciliation:
    delivery_seen: bool
    absence_observed: bool
    irreconcilable: bool
    user_message_id: Optional[str] = None
    conversation_digest: Optional[str] = None


GetConversation = Callable[[str], Awaitable[DetachedGetResult]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def _matching_user_id(
    body: dict[str, Any], *, attempt_nonce: str, user_message_id: Optional[str]
) -> Optional[str]:
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        return None
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        if not isinstance(author, dict) or author.get("role") != "user":
            continue
        candidate = str(message.get("id") or "")
        content = message.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "".join(part for part in parts or [] if isinstance(part, str))
        if (user_message_id and candidate == user_message_id) or (
            attempt_nonce and attempt_nonce in text
        ):
            return candidate
    return None


async def reconcile_ack_unknown(
    *,
    conversation_id: Optional[str],
    pre_send_current_node: Optional[str],
    attempt_nonce: str,
    user_message_id: Optional[str],
    deadline: float,
    get_conversation: GetConversation,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> AckReconciliation:
    """Read-only recovery for every ambiguous pre/post-invocation cut."""
    if not conversation_id:
        return AckReconciliation(False, False, True)

    first = True
    last_current: Optional[str] = None
    while first or clock() < deadline:
        first = False
        result = await get_conversation(conversation_id)
        interval = 3.0
        if result.status_code == 429 and result.retry_after is not None:
            interval = max(3.0, result.retry_after)
        elif result.status_code == 200 and isinstance(result.body, dict):
            body = result.body
            last_current = str(body.get("current_node") or "") or None
            found = _matching_user_id(
                body,
                attempt_nonce=attempt_nonce,
                user_message_id=user_message_id,
            )
            if found:
                return AckReconciliation(
                    True,
                    False,
                    False,
                    user_message_id=found,
                    conversation_digest=canonical_conversation_digest(body),
                )
            interval = 10.0
        remaining = deadline - clock()
        if remaining <= 0:
            break
        await sleep(min(interval, remaining))

    return AckReconciliation(
        False,
        last_current == pre_send_current_node,
        False,
    )


async def poll_authoritative_get(
    *,
    conversation_id: str,
    submitted_user_msg_id: str,
    run_id: str,
    bundle_sha: str,
    deadline: float,
    get_conversation: GetConversation,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> tuple[AcceptedAnswer, str]:
    """Return only a GET-accepted answer and its canonical branch digest."""
    while clock() < deadline:
        result = await get_conversation(conversation_id)
        interval = 3.0
        if result.status_code == 429 and result.retry_after is not None:
            interval = max(3.0, result.retry_after)
        elif result.status_code == 200 and isinstance(result.body, dict):
            outcome = evaluate_gate(
                result.body,
                submitted_user_msg_id=submitted_user_msg_id,
                run_id=run_id,
                bundle_sha=bundle_sha,
            )
            if isinstance(outcome, AcceptedAnswer):
                return outcome, canonical_conversation_digest(result.body)
            assert isinstance(outcome, GatePending)
            interval = 10.0
        await sleep(min(interval, max(0.0, deadline - clock())))
    raise TimeoutError("authoritative conversation GET deadline exhausted")
