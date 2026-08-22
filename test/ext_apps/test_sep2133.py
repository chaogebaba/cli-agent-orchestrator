"""Tests for SEP-2133 capability negotiation (pull + push) and client detection.

All three surfaces are default-off: they no-op / return falsy unless
``CAO_MCP_APPS_ENABLED`` is set.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from cli_agent_orchestrator.ext_apps.sep2133 import (
    EXTENSION_ID,
    advertise_capability,
    client_supports_mcp_apps,
    negotiate_capabilities,
)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)


class TestNegotiateCapabilities:
    def test_noop_when_disabled(self, disabled: None) -> None:
        assert negotiate_capabilities() == {}

    def test_returns_capabilities_when_enabled(self, enabled: None) -> None:
        caps = negotiate_capabilities()
        assert caps.get("resources") is True
        assert caps.get("tools") is True
        assert caps["ui"]["allowUnsafeEval"] is False


class _FakeLowLevel:
    def __init__(self) -> None:
        self.received: List[Dict[str, Any]] = []

    def create_initialization_options(
        self,
        notification_options: Any = None,
        experimental_capabilities: Any = None,
        **kw: Any,
    ) -> Dict[str, Any]:
        caps = dict(experimental_capabilities or {})
        self.received.append(caps)
        return {"experimental": caps}


class _FakeExtension:
    """Captures the extension registered via add_extension."""

    def __init__(self) -> None:
        self.identifier: Optional[str] = None
        self.settings_result: Optional[Dict[str, Any]] = None

    def store(self, ext: Any) -> None:
        self.identifier = ext.identifier
        self.settings_result = ext.settings()


class _FakeMcp:
    def __init__(self) -> None:
        self._mcp_server = _FakeLowLevel()
        self.experimental_capabilities: Dict[str, Any] = {}
        self._registered_extensions: List[Any] = []

    def add_extension(self, ext: Any) -> None:
        self._registered_extensions.append(ext)


class TestAdvertiseCapability:
    def test_noop_when_disabled(self, disabled: None) -> None:
        mcp = _FakeMcp()
        advertise_capability(mcp)
        # Neither extension registered nor experimental set
        assert len(mcp._registered_extensions) == 0
        assert EXTENSION_ID not in mcp.experimental_capabilities

    def test_advertises_extension_when_enabled(self, enabled: None) -> None:
        mcp = _FakeMcp()
        advertise_capability(mcp)
        # Extension registered via add_extension
        assert len(mcp._registered_extensions) == 1
        ext = mcp._registered_extensions[0]
        assert ext.identifier == EXTENSION_ID
        assert ext.settings() == {"mimeTypes": ["text/html;profile=mcp-app"]}

    def test_mirrors_to_experimental_when_enabled(self, enabled: None) -> None:
        mcp = _FakeMcp()
        advertise_capability(mcp)
        # Legacy fallback: experimental_capabilities dict updated
        assert EXTENSION_ID in mcp.experimental_capabilities
        assert mcp.experimental_capabilities[EXTENSION_ID]["mimeTypes"] == [
            "text/html;profile=mcp-app"
        ]

    def test_preserves_existing_experimental_caps(self, enabled: None) -> None:
        mcp = _FakeMcp()
        mcp.experimental_capabilities["some.other/ext"] = {"x": 1}
        advertise_capability(mcp)
        assert mcp.experimental_capabilities["some.other/ext"] == {"x": 1}
        assert EXTENSION_ID in mcp.experimental_capabilities

    def test_no_add_extension_does_not_raise(self, enabled: None) -> None:
        class _NoExtensionSupport:
            experimental_capabilities: Dict[str, Any] = {}

        # No add_extension method — should not raise, just log
        advertise_capability(_NoExtensionSupport())  # type: ignore[arg-type]
        # Fallback to experimental still works
        assert EXTENSION_ID in _NoExtensionSupport.experimental_capabilities


class _Caps:
    def __init__(self, experimental: Optional[Dict[str, Any]]) -> None:
        self.experimental = experimental


class _ClientParams:
    def __init__(self, capabilities: Any) -> None:
        self.capabilities = capabilities


class _Session:
    def __init__(self, client_params: Any) -> None:
        self.client_params = client_params


class _Ctx:
    def __init__(self, session: Any) -> None:
        self.session = session


class _CtxMcp:
    def __init__(self, ctx: Any, raise_: bool = False) -> None:
        self._ctx = ctx
        self._raise = raise_

    def get_context(self) -> Any:
        if self._raise:
            raise RuntimeError("no active request context")
        return self._ctx


def _mcp_with(experimental: Optional[Dict[str, Any]]) -> _CtxMcp:
    return _CtxMcp(_Ctx(_Session(_ClientParams(_Caps(experimental)))))


class TestClientSupportsMcpApps:
    def test_false_when_disabled(self, disabled: None) -> None:
        assert client_supports_mcp_apps(_mcp_with({EXTENSION_ID: {}})) is False

    def test_true_when_client_advertises(self, enabled: None) -> None:
        assert client_supports_mcp_apps(_mcp_with({EXTENSION_ID: {}})) is True

    def test_true_when_client_advertises_via_extensions(self, enabled: None) -> None:
        # Spec-compliant host on a newer SDK advertises under `extensions`
        # (the SEP-1865 / SEP-1724 location) rather than `experimental`.
        class _ExtCaps:
            experimental = None
            extensions = {EXTENSION_ID: {}}

        mcp = _CtxMcp(_Ctx(_Session(_ClientParams(_ExtCaps()))))
        assert client_supports_mcp_apps(mcp) is True

    def test_false_when_client_lacks_extension(self, enabled: None) -> None:
        assert client_supports_mcp_apps(_mcp_with({})) is False

    def test_false_when_context_unavailable(self, enabled: None) -> None:
        assert client_supports_mcp_apps(_CtxMcp(None, raise_=True)) is False
