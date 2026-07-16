"""Process-local sandbox admission and mutation guards."""

from __future__ import annotations

import os
from typing import Any

IDENTITY_ENV_KEYS = ("CAO_TERMINAL_ID", "CAO_INSTANCE_ID", "CAO_ENDPOINT")


class SandboxMutationForbidden(RuntimeError):
    """A production-mutating operation was attempted by a sandbox process."""


class SandboxProviderUnsafe(RuntimeError):
    """A provider without a G7 plane was requested in a sandbox."""


def is_sandbox() -> bool:
    return bool(os.environ.get("CAO_INSTANCE_ID", "").strip())


def require_not_sandbox_mutation(action: str) -> None:
    if is_sandbox():
        raise SandboxMutationForbidden(f"sandbox mutation forbidden: {action}")


def require_provider_admitted(provider: str) -> None:
    if is_sandbox():
        raise SandboxProviderUnsafe(f"sandbox_provider_unsafe:{provider}")


def bind_mcp_server_identity(config: dict[str, Any], terminal_id: str) -> dict[str, Any]:
    """Force terminal, instance, and endpoint identity into an MCP config copy."""
    result = dict(config)
    if "command" not in result:
        return result
    env = dict(result.get("env") or {})
    from cli_agent_orchestrator.utils.http import resolve_endpoint

    expected = {
        "CAO_TERMINAL_ID": terminal_id,
        "CAO_INSTANCE_ID": os.environ.get("CAO_INSTANCE_ID", ""),
        "CAO_ENDPOINT": resolve_endpoint(),
    }
    for key, value in expected.items():
        supplied = env.get(key)
        if supplied is not None and supplied != value:
            raise ValueError(f"profile may not override {key}")
        env[key] = value
    result["env"] = env
    return result


def bind_pane_identity(environment: dict[str, str] | None, terminal_id: str) -> dict[str, str]:
    """Force immutable terminal/instance affinity into a pane environment."""
    result = dict(environment or {})
    from cli_agent_orchestrator.utils.http import resolve_endpoint

    expected = {
        "CAO_TERMINAL_ID": terminal_id,
        "CAO_INSTANCE_ID": os.environ.get("CAO_INSTANCE_ID", ""),
        "CAO_ENDPOINT": resolve_endpoint(),
    }
    for key, value in expected.items():
        supplied = result.get(key)
        if supplied is not None and supplied != value:
            raise ValueError(f"pane environment may not override {key}")
        result[key] = value
    return result
