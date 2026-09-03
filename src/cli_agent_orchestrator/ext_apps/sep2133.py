"""SEP-2133 capability negotiation for the CAO MCP App surface.

SEP-2133 (Extension Framework) lets a server advertise the MCP App capabilities
it supports so a host can decide whether to render the ``ui://cao/*`` resources.
CAO's negotiation is intentionally **default-off**: nothing here changes the
localhost-only posture unless ``CAO_MCP_APPS_ENABLED`` is set.

Two complementary surfaces are provided:

  ``negotiate_capabilities(client_capabilities)``
    Pull model — returns the capability set CAO offers given the client's
    capabilities. Returns ``{}`` (a no-op) when disabled.

  ``advertise_capability(mcp)``
    Push model — registers a SEP-2133 server extension with FastMCP 4 so
    ``io.modelcontextprotocol/ui`` is advertised in
    ``capabilities.extensions`` (for 2026-07-28 clients) AND mirrors the
    capability into ``experimental_capabilities`` (for legacy clients on
    protocol revisions that lack the ``extensions`` field). No-op when
    ``CAO_MCP_APPS_ENABLED`` is unset.

  ``client_supports_mcp_apps(mcp)``
    Returns True if the *current* MCP request context shows the connected client
    advertised ``io.modelcontextprotocol/ui`` support. Call inside a tool handler
    to decide whether to return a UI resource or a text-only fallback.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# The SEP-2133 extension identifier for MCP Apps.
EXTENSION_ID = "io.modelcontextprotocol/ui"

# The MCP App capabilities CAO advertises when enabled. ``resources`` signals the
# ``ui://cao/*`` views; ``tools`` signals the App tool channel; ``ui`` carries the
# rendering hints the host honours (CSP-sandboxed iframe, no host-side eval).
_CAPABILITIES: Dict[str, Any] = {
    "resources": True,
    "tools": True,
    "ui": {
        "iframe": True,
        "allowUnsafeEval": False,
    },
}

# Capability block advertised under capabilities.extensions[EXTENSION_ID]
# and mirrored into experimental_capabilities for legacy clients.
SERVER_EXTENSION_CAPABILITY: Dict[str, Any] = {
    EXTENSION_ID: {
        "mimeTypes": ["text/html;profile=mcp-app"],
    }
}


def _is_enabled() -> bool:
    """Return whether the MCP App surface is enabled via ``apps.enabled``
    (``CAO_MCP_APPS_ENABLED`` env var or ``settings.json``)."""

    return bool(ConfigService.get("apps.enabled", default=False))


def negotiate_capabilities(client_capabilities: Any = None) -> Dict[str, Any]:
    """Return the MCP App capabilities CAO offers given the client's capabilities.

    No-op (returns ``{}``) unless ``CAO_MCP_APPS_ENABLED`` is set. When enabled,
    returns CAO's advertised capability set; the ``client_capabilities`` argument
    is accepted for forward compatibility (future intersection logic) but does not
    yet narrow the result.
    """

    if not _is_enabled():
        return {}
    if client_capabilities is not None:
        logger.debug("SEP-2133 negotiation with client capabilities: %s", client_capabilities)
    return dict(_CAPABILITIES)


def advertise_capability(mcp: Any) -> None:
    """Advertise ``io.modelcontextprotocol/ui`` via dual-location strategy.

    Under FastMCP 4 / mcp 2.0:

    1. **capabilities.extensions** — registers a proper ``ServerExtension``
       so the identifier and settings are advertised in the ``server/discover``
       response and the ``initialize`` response for 2026-07-28 clients.
       FastMCP 4 also advertises the bare identifier natively; our extension
       registration overrides that with our settings (``mimeTypes``).

    2. **experimental_capabilities** — injects the same capability map into
       the FastMCP instance's ``experimental_capabilities`` dict so legacy
       clients (protocol ≤2025-11-25, e.g. cline 3.0.56) still see it in
       ``ServerCapabilities.experimental``. The SDK strips ``extensions`` for
       pre-2026 protocol versions, so ``experimental`` is the only fallback
       that reaches them.

    No-op when ``CAO_MCP_APPS_ENABLED`` is unset.
    """
    if not _is_enabled():
        return

    # --- Strategy 1: capabilities.extensions via ServerExtension ---
    try:
        from fastmcp.server.extensions import ServerExtension

        class McpAppsExtension(ServerExtension):
            """SEP-2133 extension for MCP Apps (io.modelcontextprotocol/ui)."""
            identifier = EXTENSION_ID

            def settings(self) -> Dict[str, Any]:
                return {"mimeTypes": ["text/html;profile=mcp-app"]}

        mcp.add_extension(McpAppsExtension())
        logger.info(
            "SEP-2133: registered %s extension (capabilities.extensions)", EXTENSION_ID
        )
    except Exception:
        logger.warning(
            "SEP-2133: failed to register ServerExtension for %s; "
            "extensions advertisement may be limited to experimental only",
            EXTENSION_ID,
            exc_info=True,
        )

    # --- Strategy 2: experimental_capabilities fallback for legacy clients ---
    try:
        experimental = getattr(mcp, "experimental_capabilities", None)
        if experimental is not None:
            experimental.update(SERVER_EXTENSION_CAPABILITY)
            logger.info(
                "SEP-2133: mirrored %s into experimental_capabilities", EXTENSION_ID
            )
        else:
            # FastMCP instance has no experimental_capabilities attribute;
            # fall back to the legacy monkey-patch if _mcp_server is available
            logger.debug(
                "SEP-2133: no experimental_capabilities attr on %r; "
                "legacy clients may not see the advertisement", mcp
            )
    except Exception:
        logger.warning(
            "SEP-2133: failed to set experimental_capabilities fallback for %s",
            EXTENSION_ID,
            exc_info=True,
        )


def client_supports_mcp_apps(mcp: Any) -> bool:
    """Return True if the connected client advertised MCP Apps support.

    Must be called from within an active MCP tool handler (uses the FastMCP
    request context). Returns False when the check is impossible (no context, no
    client params, or capability absent). When ``CAO_MCP_APPS_ENABLED`` is unset,
    always returns False so tools unconditionally serve text-only fallbacks.
    """
    if not _is_enabled():
        return False

    try:
        ctx = mcp.get_context()
        session = getattr(ctx, "session", None)
        if session is None:
            return False
        client_params = getattr(session, "client_params", None)
        if client_params is None:
            return False
        capabilities = getattr(client_params, "capabilities", None)
        if capabilities is None:
            return False
        # SEP-1865 advertises under capabilities.extensions (per SEP-1724); the
        # installed SDK also exposes `experimental`. Accept either so both
        # current- and future-SDK hosts are recognized.
        experimental = getattr(capabilities, "experimental", None) or {}
        extensions = getattr(capabilities, "extensions", None) or {}
        return EXTENSION_ID in experimental or EXTENSION_ID in extensions
    except Exception:
        logger.debug("client_supports_mcp_apps check failed; assuming unsupported", exc_info=True)
        return False
