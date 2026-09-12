"""Shared authenticated HTTP helpers for scoped CLI surfaces."""

from __future__ import annotations

from typing import Any

from cli_agent_orchestrator.security.auth import get_local_bearer


def bearer_headers() -> dict[str, str]:
    token = get_local_bearer()
    return {"Authorization": f"Bearer {token}"} if token else {}


def response_detail(response: Any) -> dict[str, Any] | None:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, dict) else None


def format_domain_detail(detail: dict[str, Any]) -> str:
    text = f"{detail.get('code', 'request_failed')}: {detail.get('message', '')}".rstrip()
    cause = detail.get("cause")
    if isinstance(cause, dict):
        text += f"; cause={cause.get('code', 'unknown')}: {cause.get('message', '')}".rstrip()
    return text


#: Longest served error body echoed verbatim when the server sent no ``detail``.
_SERVED_BODY_CHARS = 500


def served_error_message(exc: Any) -> str:
    """F241 (#64): render a SERVED HTTP error truthfully.

    A ``requests.exceptions.HTTPError`` means the round-trip SUCCEEDED and the
    server answered with a 4xx/5xx — the opposite of a connection failure. The
    blanket ``except RequestException`` arms print "Failed to connect to
    cao-server: …" for it, which reads as "the server is down, go restart it";
    for CAO that reading is destructive (a ``cao-server`` restart is the one
    action that endangers live sessions) and it discards a body that usually
    names the real, user-correctable cause — e.g. the duplicate-``--session-name``
    400 whose ``detail`` is ``Session 'x' already exists``.

    Returns "cao-server returned HTTP <status>[: <detail>]", where ``detail`` is
    the structured domain detail when the body carries one (via
    :func:`format_domain_detail`), else the plain-string ``detail``, else the
    truncated response text. The word "connect" never appears.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    head = f"cao-server returned HTTP {status}" if status else "cao-server returned an error"
    if response is None:
        return head

    body: Any = None
    try:
        body = response.json()
    except Exception:
        body = None

    detail_text = ""
    if isinstance(body, dict):
        raw = body.get("detail")
        if isinstance(raw, dict):
            detail_text = format_domain_detail(raw)
        elif isinstance(raw, str):
            detail_text = raw.strip()
    if not detail_text:
        text = getattr(response, "text", "") or ""
        detail_text = text.strip()[:_SERVED_BODY_CHARS]

    return f"{head}: {detail_text}" if detail_text else head
