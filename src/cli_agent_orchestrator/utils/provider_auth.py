"""Offline classification of installed-provider authentication refresh output."""

from __future__ import annotations

from typing import Literal

AuthRefreshState = Literal[
    "interactive_login_required",
    "access_token_acquisition_failed",
    "credential_write_failed",
    "transient_network_failure",
    "auth_changed_skip",
]

_TERMINAL: dict[str, tuple[str, ...]] = {
    "interactive_login_required": (
        "Authentication failed. Please check your API credentials.",
        "Authentication failed: Invalid authorization code",
        "Session token expired or invalid",
    ),
    "access_token_acquisition_failed": (
        "Authentication failed (401)",
        "Authentication failed",
        "Token refresh failed",
    ),
    "credential_write_failed": (
        "Failed to save OAuth tokens",
        "tengu_oauth_tokens_save_failed",
        "tengu_oauth_tokens_save_exception",
    ),
}
_NON_TERMINAL: dict[str, tuple[str, ...]] = {
    "transient_network_failure": (
        "Auto-refresh failed, falling back to needsRefresh:",
        "Credential renew failed:",
        "network is unreachable",
        "connection timed out",
    ),
    "auth_changed_skip": ("Skipping token refresh because auth changed after guarded reload.",),
}


def classify_auth_refresh_output(provider: str, output: str) -> AuthRefreshState | None:
    """Classify only version-pinned strings captured in the offline fixture corpus."""
    if provider not in {"claude_code", "codex"}:
        return None
    for state, markers in _TERMINAL.items():
        if any(marker in output for marker in markers):
            return state  # type: ignore[return-value]
    for state, markers in _NON_TERMINAL.items():
        if any(marker in output for marker in markers):
            return state  # type: ignore[return-value]
    return None


def auth_refresh_state_is_terminal(state: AuthRefreshState) -> bool:
    return state in _TERMINAL
