"""F689 (#544): the production-API fence on ``cao_http``.

F334 (#190) fenced direct in-process DB access. It does not cover the other
axis: a test that speaks HTTP to the live server on :9889 never touches the
fenced code path. That is how the fixture id ``abcd1234`` reached the running
production instance 48x in a single burst. These tests pin the symmetric fence.

The fence's discriminator is IDENTITY against the ``requests`` verbs captured at
import: only an unpatched verb can actually open a socket. So the cases that
must trip the fence deliberately leave ``requests.get`` genuine and stub the
transport one layer lower, at ``HTTPAdapter.send`` — that way a regression
(fence removed) surfaces as "the adapter was reached", never as live traffic to
production.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.adapters import HTTPAdapter

from cli_agent_orchestrator.utils.http import (
    CAOHttpClient,
    EndpointConfigurationError,
    cao_http,
)


@pytest.fixture
def default_endpoint(monkeypatch):
    """Resolve to the production default (loopback:9889), as a bare run does."""
    monkeypatch.delenv("CAO_ENDPOINT", raising=False)
    monkeypatch.delenv("CAO_API_HOST", raising=False)
    monkeypatch.delenv("CAO_API_PORT", raising=False)
    monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)
    monkeypatch.delenv("CAO_ALLOW_PROD_API_IN_TESTS", raising=False)


@pytest.fixture
def wire(monkeypatch):
    """Stub the socket layer, leaving the ``requests`` verbs themselves genuine.

    Records every URL that reached the adapter. Under the fence this list must
    stay empty for production targets; with the fence removed it fills, which is
    exactly the regression signal.
    """
    reached: list[str] = []

    def _fake_send(self, request, **kwargs):
        reached.append(request.url)
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.url = request.url
        return response

    monkeypatch.setattr(HTTPAdapter, "send", _fake_send)
    return reached


class TestProductionApiFence:
    def test_fence_blocks_live_request_to_production(self, default_endpoint, wire):
        """The whole point: nothing leaves a test process toward :9889."""
        with pytest.raises(EndpointConfigurationError) as exc:
            cao_http.get("/terminals/abcd1234")

        assert "F689 FENCE" in str(exc.value)
        assert "9889" in str(exc.value)
        assert wire == []  # never reached the wire

    def test_fence_blocks_every_verb(self, default_endpoint, wire):
        for verb in ("get", "post", "put", "patch", "delete"):
            with pytest.raises(EndpointConfigurationError):
                getattr(cao_http, verb)("/terminals/abcd1234")

        with pytest.raises(EndpointConfigurationError):
            cao_http.request("GET", "/terminals/abcd1234")

        assert wire == []

    def test_non_production_port_is_allowed(self, default_endpoint, wire, monkeypatch):
        """A fixture server on its own free port is what tests should target."""
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19889")

        cao_http.get("/health")

        assert wire == ["http://127.0.0.1:19889/health"]

    def test_explicit_override_env_allows_production(self, default_endpoint, wire, monkeypatch):
        """Mirrors CAO_ALLOW_PROD_DB_IN_TESTS — contract tests only."""
        monkeypatch.setenv("CAO_ALLOW_PROD_API_IN_TESTS", "1")

        cao_http.get("/health")

        assert wire == ["http://127.0.0.1:9889/health"]

    def test_fence_is_inert_outside_pytest(self, default_endpoint, wire, monkeypatch):
        """Production is the product's normal target; only tests are fenced."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        cao_http.get("/health")

        assert wire == ["http://127.0.0.1:9889/health"]


class TestStubbedCallersAreNotFenced:
    """Tests that already stub their transport must keep working untouched."""

    def test_injected_transport_is_not_fenced(self, default_endpoint, wire):
        transport = MagicMock()
        client = CAOHttpClient(transport=lambda: transport)

        client.get("/terminals/abcd1234")

        transport.get.assert_called_once()
        assert wire == []

    def test_unittest_mock_patched_verb_is_not_fenced(self, default_endpoint, wire):
        with patch.object(requests, "get") as mock_get:
            cao_http.get("/terminals/abcd1234")

        mock_get.assert_called_once()
        assert wire == []

    def test_plain_function_stub_is_not_fenced(self, default_endpoint, wire, monkeypatch):
        """A hand-rolled stub function is as inert as a mock — do not fence it.

        This is the shape used across test/mcp_server (``patch.object(mod.requests,
        "get", _fake_get_factory(...))``); a heuristic that only recognised
        unittest.mock doubles would red those suites for no safety gain.
        """
        calls: list[str] = []

        def _fake_get(url, **kwargs):
            calls.append(url)
            return "stubbed"

        monkeypatch.setattr(requests, "get", _fake_get)

        assert cao_http.get("/terminals/abcd1234") == "stubbed"
        assert calls == ["http://127.0.0.1:9889/terminals/abcd1234"]
        assert wire == []
