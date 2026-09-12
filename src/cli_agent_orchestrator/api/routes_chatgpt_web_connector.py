"""Connector composition seam for Amendment D (D5/D7).

This module intentionally does not import or modify ``api.main``.  B3 may mount
the returned ASGI app under its chosen public route while keeping the connector
separately killable and loopback-bound.
"""

from __future__ import annotations

from typing import Any

from cli_agent_orchestrator.workspace_connector.http_server import ConnectorServer


def build_connector_app(server: ConnectorServer) -> Any:
    """Return the loopback connector ASGI application for one attempt."""
    return server.build_app()


def connector_audit_projection(server: ConnectorServer) -> list[dict[str, Any]]:
    """Return digest/refusal-only audit rows consumed by the runner verifier."""
    return server.audit_projection()
