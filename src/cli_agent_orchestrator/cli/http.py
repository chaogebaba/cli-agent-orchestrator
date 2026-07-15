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
