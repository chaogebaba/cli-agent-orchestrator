"""F295 Half 2 — Relay preflight tests (AC1-AC6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.utils.grok_preflight import (
    RelayPreflightFailed,
    _probe_chat,
    _probe_responses,
    _probe_transport,
    _read_model_table,
    _redact_key,
    _resolve_model,
    run_preflight,
)


# ---------------------------------------------------------------------------
# AC4: RelayPreflightFailed structure
# ---------------------------------------------------------------------------


class TestRelayPreflightFailedStructure:
    """The exception has .code and .detail matching NativeHomeIsolationUnavailable shape."""

    def test_has_code(self):
        exc = RelayPreflightFailed("test detail")
        assert exc.code == "grok_relay_preflight_failed"

    def test_has_detail(self):
        exc = RelayPreflightFailed("some detail")
        assert exc.detail == "some detail"

    def test_is_runtime_error(self):
        assert issubclass(RelayPreflightFailed, RuntimeError)


# ---------------------------------------------------------------------------
# AC2: official direct route makes zero network calls
# ---------------------------------------------------------------------------


class TestOfficialRouteNoProbe:
    """When the resolved model has no base_url, no network call is made (D2)."""

    def test_no_base_url_no_probe(self, tmp_path: Path):
        """Resolved model without base_url → return immediately, no HTTP."""
        config_toml = tmp_path / "config.toml"
        config_toml.write_text('[model.grok-4]\nname = "grok-4"\n')

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", side_effect=AssertionError("should not call")),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.get", side_effect=AssertionError("should not call")),
        ):
            # Should succeed without making any network call
            run_preflight(agent_profile="dev", model=None)

    def test_no_model_table_no_probe(self, tmp_path: Path):
        """No [model.X] table at all → no probe."""
        config_toml = tmp_path / "config.toml"
        config_toml.write_text("[other]\nfoo = 1\n")

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", side_effect=AssertionError("should not call")),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.get", side_effect=AssertionError("should not call")),
        ):
            run_preflight(agent_profile="dev", model=None)


# ---------------------------------------------------------------------------
# AC3: exactly one probe, at creation only
# ---------------------------------------------------------------------------


class TestExactlyOneProbe:
    """A healthy relay receives exactly one request."""

    def test_one_request_on_responses_backend(self, tmp_path: Path):
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.grok-4]\nbase_url = "http://localhost:9999"\n'
            'api_key = "sk-test"\napi_backend = "responses"\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", return_value=mock_resp) as mock_post,
        ):
            run_preflight(agent_profile="dev", model=None)
            assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# AC4: failure names the route; api key appears nowhere
# ---------------------------------------------------------------------------


class TestKeyRedaction:
    """The api key must never appear in exception detail."""

    def test_key_stripped_from_body_tail(self):
        result = _redact_key("error body contains sk-secret-key here", "sk-secret-key")
        assert "sk-secret-key" not in result
        assert "[REDACTED]" in result

    def test_key_in_502_response(self, tmp_path: Path):
        """A 502 with the key in the body → detail has endpoint and model, no key."""
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.grok-4]\nbase_url = "http://relay.local:8080"\n'
            'api_key = "sk-live-secret123"\napi_backend = "responses"\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway: auth failed for sk-live-secret123"

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", return_value=mock_resp),
        ):
            with pytest.raises(RelayPreflightFailed) as exc_info:
                run_preflight(agent_profile="dev", model=None)

            detail = exc_info.value.detail
            # Must contain route info
            assert "relay.local:8080" in detail
            assert "grok-4" in detail
            assert "http_502" in detail
            # Must NOT contain key — grep -cF equivalent
            assert detail.count("sk-live-secret123") == 0


# ---------------------------------------------------------------------------
# AC5: both escapes work
# ---------------------------------------------------------------------------


class TestEscapes:
    """relay_preflight=false and is_sandbox() both bypass the probe."""

    def test_disabled_via_providers_toml(self, tmp_path: Path):
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.grok-4]\nbase_url = "http://dead:1"\napi_backend = "responses"\n'
        )
        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": False},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", side_effect=AssertionError("should not call")),
        ):
            # Dead relay but preflight disabled → no exception
            run_preflight(agent_profile="dev", model=None)

    def test_sandbox_skips(self, tmp_path: Path):
        with (
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox",
                return_value=True,
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", side_effect=AssertionError("should not call")),
        ):
            run_preflight(agent_profile="dev", model=None)


# ---------------------------------------------------------------------------
# AC6: probe shape follows api_backend; unknown degrades
# ---------------------------------------------------------------------------


class TestProbeShapes:
    """Probe shape keyed on api_backend; unknown degrades to transport-only."""

    def test_responses_backend_posts_responses(self, tmp_path: Path):
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.m]\nbase_url = "http://r:1"\napi_backend = "responses"\napi_key = "k"\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model", return_value="m"
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox", return_value=False
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", return_value=mock_resp) as mock_post,
        ):
            run_preflight(agent_profile=None, model="m")
            url_called = mock_post.call_args[0][0]
            assert "/responses" in url_called
            body = mock_post.call_args[1]["json"]
            assert "max_output_tokens" in body

    def test_chat_backend_posts_chat_completions(self, tmp_path: Path):
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.m]\nbase_url = "http://r:1"\napi_backend = "chat"\napi_key = "k"\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model", return_value="m"
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox", return_value=False
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", return_value=mock_resp) as mock_post,
        ):
            run_preflight(agent_profile=None, model="m")
            url_called = mock_post.call_args[0][0]
            assert "/chat/completions" in url_called
            body = mock_post.call_args[1]["json"]
            assert "max_tokens" in body

    def test_unknown_backend_degrades_to_get(self, tmp_path: Path):
        """Unknown api_backend → GET; a 404 passes (transport alive)."""
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.m]\nbase_url = "http://r:1"\napi_backend = "wat"\napi_key = "k"\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model", return_value="m"
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox", return_value=False
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.get", return_value=mock_resp) as mock_get,
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", side_effect=AssertionError("should not POST")),
        ):
            # 404 passes (any HTTP response = transport alive)
            run_preflight(agent_profile=None, model="m")
            assert mock_get.call_count == 1

    def test_unknown_backend_connection_refused_fails(self, tmp_path: Path):
        """Unknown api_backend with refused connection → fails."""
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.m]\nbase_url = "http://r:1"\napi_backend = "wat"\napi_key = "k"\n'
        )
        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model", return_value="m"
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox", return_value=False
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._http.get",
                side_effect=requests.ConnectionError("Connection refused"),
            ),
        ):
            with pytest.raises(RelayPreflightFailed) as exc_info:
                run_preflight(agent_profile=None, model="m")
            assert "connect_refused" in exc_info.value.detail


# ---------------------------------------------------------------------------
# AC1: connection refused → RelayPreflightFailed
# ---------------------------------------------------------------------------


class TestConnectionRefused:
    """A dead relay raises RelayPreflightFailed."""

    def test_connection_refused_raises(self, tmp_path: Path):
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.grok-4]\nbase_url = "http://localhost:1"\n'
            'api_backend = "responses"\napi_key = "k"\n'
        )
        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox", return_value=False
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._http.post",
                side_effect=requests.ConnectionError("Connection refused"),
            ),
        ):
            with pytest.raises(RelayPreflightFailed) as exc_info:
                run_preflight(agent_profile="dev", model=None)
            assert "connect_refused" in exc_info.value.detail
            assert "localhost" in exc_info.value.detail
            assert "grok-4" in exc_info.value.detail

    def test_timeout_raises(self, tmp_path: Path):
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            '[model.grok-4]\nbase_url = "http://relay:80"\n'
            'api_backend = "responses"\napi_key = "k"\n'
        )
        with (
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._canonical_config_path",
                return_value=config_toml,
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight._resolve_model",
                return_value="grok-4",
            ),
            patch(
                "cli_agent_orchestrator.utils.grok_preflight.get_provider_defaults",
                return_value={"relay_preflight": True},
            ),
            patch(
                "cli_agent_orchestrator.utils.sandbox_guard.is_sandbox", return_value=False
            ),
            patch("cli_agent_orchestrator.utils.grok_preflight._http.post", side_effect=requests.Timeout("timed out")),
        ):
            with pytest.raises(RelayPreflightFailed) as exc_info:
                run_preflight(agent_profile="dev", model=None)
            assert "timeout" in exc_info.value.detail
