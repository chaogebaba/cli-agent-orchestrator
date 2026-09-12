"""One-shot Python origin sender for F862 Amendment D.

The GUI mints and holds a real request; this module is the sole origin sender.
It validates the measured curl-cffi posture before any network-capable object is
created, fsyncs ``MINT_RESERVED``, invokes once, relays decoded bytes transiently,
and keeps draining after caller overflow/disconnect for GET reconciliation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, AsyncIterator, Callable, Optional, cast
from urllib.parse import urlsplit

from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    CapturedSend,
    RouteGenerations,
)
from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
    AttemptState,
    SendIntentLog,
)
from cli_agent_orchestrator.chatgpt_web_runner.sse_stream import SSEProjection
from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import AttemptRelay

API_DRIVE_IMPERSONATE = "chrome136"
_VALIDATED_POSTURES = frozenset({API_DRIVE_IMPERSONATE})


class ApiDriveError(RuntimeError):
    pass


@dataclass(frozen=True)
class OriginResult:
    status_code: int
    content_type: str
    bytes_drained: int
    relay_status: str
    projection: tuple[dict[str, Any], ...]


def validate_impersonation(posture: str) -> None:
    """Fail before network because curl-cffi 0.13 accepts unknown templates."""
    if posture not in _VALIDATED_POSTURES:
        raise ApiDriveError(
            f"unvalidated curl_cffi impersonation posture {posture!r}; network call refused"
        )


def rewrite_first_use_body(
    raw_body: bytes,
    *,
    prompt_text: str,
    user_message_id: str,
    conversation_id: Optional[str] = None,
    parent_message_id: Optional[str] = None,
) -> tuple[bytes, dict[str, Any]]:
    """Apply only D2's measured first-use prompt/id/parent rewrites."""
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiDriveError("captured conversation body is not JSON") from exc
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        raise ApiDriveError("captured body has no rewritable first user message")
    message = dict(messages[0])
    author = message.get("author")
    if not isinstance(author, dict) or author.get("role") != "user":
        raise ApiDriveError("first captured message is not the user message")
    content = message.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        raise ApiDriveError("captured user message has no content.parts")
    message["id"] = user_message_id
    message["content"] = {**content, "parts": [prompt_text]}
    body["messages"] = [message, *messages[1:]]
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    if parent_message_id is not None:
        body["parent_message_id"] = parent_message_id
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return encoded, message


def _headers_and_cookies(
    captured: CapturedSend,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]]]:
    """Separate Cookie into a scoped jar and drop recomputed Content-Length."""
    host = urlsplit(captured.url).hostname or ""
    headers: list[tuple[str, str]] = []
    cookies: list[tuple[str, str, str, str]] = []
    for name, value in captured.ordered_headers:
        low = name.lower()
        if low == "content-length":
            continue
        if low != "cookie":
            headers.append((name, value))
            continue
        parsed = SimpleCookie()
        parsed.load(value)
        cookies.extend((key, morsel.value, host, "/") for key, morsel in parsed.items())
    return headers, cookies


async def _response_chunks(response: Any) -> AsyncIterator[bytes]:
    iterator = response.aiter_content()
    async for chunk in iterator:
        if chunk:
            yield bytes(chunk)


async def send_once(
    captured: CapturedSend,
    *,
    body: bytes,
    live_generations: RouteGenerations,
    intent_log: SendIntentLog,
    relay: AttemptRelay,
    posture: str = API_DRIVE_IMPERSONATE,
    session_factory: Optional[Callable[..., Any]] = None,
) -> OriginResult:
    """Invoke the captured origin URL once and drain its stream to completion."""
    validate_impersonation(posture)
    await captured.route_holder.guard_for_python(live_generations)
    if captured.method.upper() != "POST":
        raise ApiDriveError("captured request is not POST")

    # The fsynced one-way reservation precedes every network-capable action.
    intent_log.reserve_mint()
    await captured.route_holder.mark_python_invoked(live_generations)
    intent_log.record_python_post_invoked()

    headers, cookies = _headers_and_cookies(captured)
    if session_factory is None:
        from curl_cffi.requests import AsyncSession

        session_factory = AsyncSession
    # curl-cffi's stubs expose a Literal union while the runtime posture is
    # validated against the same allow-list above.
    session = cast(Any, session_factory)(impersonate=posture)
    for name, value, domain, path in cookies:
        session.cookies.set(name, value, domain=domain, path=path)

    projection = SSEProjection()
    projected: list[dict[str, Any]] = []
    drained = 0
    try:
        response = await session.post(
            captured.url,
            headers=headers,
            data=body,
            stream=True,
        )
        async for chunk in _response_chunks(response):
            drained += len(chunk)
            # Ignore False: relay closure must not stop the origin drain.
            await relay.publish(chunk)
            projected.extend(projection.feed(chunk))
        await relay.finish()
        intent_log.transition(
            AttemptState.RAW_SSE_RELAY,
            relay_status=relay.status,
        )
        return OriginResult(
            status_code=int(response.status_code),
            content_type=str(response.headers.get("content-type", "")),
            bytes_drained=drained,
            relay_status=relay.status,
            projection=tuple(projected),
        )
    finally:
        close = getattr(session, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
