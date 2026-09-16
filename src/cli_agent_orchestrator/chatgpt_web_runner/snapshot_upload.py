"""Digest helpers and same-origin read containment (D3/AC-11b).

Amendment D deleted this module's original subject. The pushed review bundle is
gone (D10): there is no upload, so there is no attachment envelope to bound
(AC-10), no attachment identity tuple to compute or verify (AC-9) and no
upload-chip readiness to calibrate. The model reads the reviewed source through
the read-only connector instead (see ``source_pull``).

What survives is what Amendment D retains: the two digest helpers and the
same-origin read-containment predicates that keep the page from reading anything
but its own conversation, plus the request-time platform-API denial. No browser
import.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

#: The only origin the runner may read from (D3/AC-11b).
ALLOWED_ORIGIN = "chatgpt.com"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Same-origin read containment (D3 / AC-11b) ---------------------------------


def is_same_origin_read_allowed(url: str, owned_conversation_id: str) -> bool:
    """AC-11b: the read exception is bound to the conversation THIS run created
    and to the chatgpt.com origin. A GET for any other conversation id, an
    off-origin fetch, or an enumeration (conversations-list) endpoint is refused.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host != ALLOWED_ORIGIN and not host.endswith("." + ALLOWED_ORIGIN):
        return False
    path = parsed.path or ""
    # The single permitted structured read: GET /backend-api/conversation/<owned>
    expected = f"/backend-api/conversation/{owned_conversation_id}"
    if path == expected:
        return True
    # The auth-session read solely to authorize the conversation GET (D3).
    if path == "/api/auth/session":
        return True
    return False


def is_enumeration_endpoint(url: str) -> bool:
    """AC-11b: the conversation-LISTING endpoint must be unreachable from the
    runner (no cookie export, no conversation enumeration, D3)."""
    parsed = urlparse(url or "")
    path = parsed.path or ""
    return path.rstrip("/").endswith("/backend-api/conversations")


def enforce_read_allowed(url: str, owned_conversation_id: str) -> None:
    """Raise ``read_forbidden`` unless ``url`` is the bounded permitted read."""
    if is_enumeration_endpoint(url) or not is_same_origin_read_allowed(url, owned_conversation_id):
        raise RunnerError(
            RunnerErrorCode.READ_FORBIDDEN,
            "read outside the bounded same-origin conversation exception",
            delivery_state=DeliveryState.DELIVERED,
        )


def enforce_no_api_egress(url: str) -> None:
    """AC-11: refuse any egress to the OpenAI API host (dynamically constructed).

    Enforced at request time, not as a startup string scan (D3) — a scan cannot
    see a dynamically built host.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host == "api.openai.com" or host.endswith(".api.openai.com"):
        raise RunnerError(
            RunnerErrorCode.EGRESS_FORBIDDEN,
            "egress to the OpenAI API is forbidden — web session only",
            delivery_state=DeliveryState.NOTHING_SENT,
        )
