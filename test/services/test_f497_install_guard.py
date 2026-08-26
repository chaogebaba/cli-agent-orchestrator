"""F497 AC2 — fail-closed install guard + live resolver-support probe.

Two refusal cases (server reports no support; server unreachable) plus the
``CAO_SKIP_RESOLVER_PROBE`` escape, asserted against a resolver-less server
rather than a resolver-less CLI (the stale component is the RUNNING server).
Legacy profiles never probe.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.services.install_service import install_agent
from cli_agent_orchestrator.utils.resolver_probe import (
    resolver_probe_skipped,
    server_supports_resolver,
)

# --------------------------------------------------------------------------
# server_supports_resolver() — fail-closed probe
# --------------------------------------------------------------------------

_PROBE = "cli_agent_orchestrator.utils.resolver_probe.cao_http"


def _health_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


def test_probe_true_when_server_advertises_resolver(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with patch(_PROBE) as http:
        http.get.return_value = _health_response({"capabilities": {"profile_resolver": True}})
        assert server_supports_resolver() is True


def test_probe_false_when_server_reports_no_support(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with patch(_PROBE) as http:
        http.get.return_value = _health_response({"capabilities": {"profile_resolver": False}})
        assert server_supports_resolver() is False


def test_probe_false_when_capability_key_missing(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with patch(_PROBE) as http:
        http.get.return_value = _health_response({"status": "ok"})
        assert server_supports_resolver() is False


def test_probe_false_when_server_unreachable(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with patch(_PROBE) as http:
        http.get.side_effect = requests.ConnectionError("connection refused")
        assert server_supports_resolver() is False


def test_probe_false_on_non_200(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with patch(_PROBE) as http:
        http.get.return_value = _health_response({"capabilities": {"profile_resolver": True}}, 503)
        assert server_supports_resolver() is False


def test_probe_false_on_malformed_body(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with patch(_PROBE) as http:
        http.get.return_value = _health_response(["not", "a", "dict"])
        assert server_supports_resolver() is False


def test_skip_env_bypasses_probe(monkeypatch):
    monkeypatch.setenv("CAO_SKIP_RESOLVER_PROBE", "1")
    assert resolver_probe_skipped() is True
    with patch(_PROBE) as http:
        # Even an unreachable server returns True under the escape, and no
        # network call is required.
        http.get.side_effect = AssertionError("probe must not touch the network when skipped")
        assert server_supports_resolver() is True


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("", False),
        ("no", False),
    ],
)
def test_skip_env_truthiness(monkeypatch, val, expected):
    if val == "":
        monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    else:
        monkeypatch.setenv("CAO_SKIP_RESOLVER_PROBE", val)
    assert resolver_probe_skipped() is expected


# --------------------------------------------------------------------------
# install_agent() guard integration
# --------------------------------------------------------------------------

_READ_SRC = "cli_agent_orchestrator.services.install_service._read_agent_profile_source"
_SUPPORTS = "cli_agent_orchestrator.services.install_service.server_supports_resolver"
_SKIPPED = "cli_agent_orchestrator.services.install_service.resolver_probe_skipped"

_COMPOSED_PROFILE = (
    "---\nname: kiro_dev\ndescription: composed alias\nprovider: kiro_cli\n"
    "position: dev\n---\nstub\n"
)
_LEGACY_PROFILE = "---\nname: plain\ndescription: legacy profile\n---\nBody\n"


def test_install_refuses_composed_profile_when_server_lacks_support(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with (
        patch(_READ_SRC, return_value=_COMPOSED_PROFILE),
        patch(_SKIPPED, return_value=False),
        patch(_SUPPORTS, return_value=False),
    ):
        result = install_agent("kiro_dev", provider="kiro_cli")
    assert result.success is False
    assert "resolver support" in result.message
    assert "position" in result.message


def test_install_refuses_composed_profile_when_server_unreachable(monkeypatch):
    # Unreachable == no support == refuse (fail-closed). Modeled by
    # server_supports_resolver() returning False (its unreachable path).
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with (
        patch(_READ_SRC, return_value=_COMPOSED_PROFILE),
        patch(_SKIPPED, return_value=False),
        patch(_SUPPORTS, return_value=False),
    ):
        result = install_agent("kiro_dev", provider="kiro_cli")
    assert result.success is False
    assert "not running" in result.message or "resolver support" in result.message


def test_install_allows_composed_profile_under_skip_escape(monkeypatch):
    # The escape short-circuits: no probe, install proceeds (fails later for
    # unrelated reasons in a bare test env, but NOT with the resolver refusal).
    monkeypatch.setenv("CAO_SKIP_RESOLVER_PROBE", "1")
    with (
        patch(_READ_SRC, return_value=_COMPOSED_PROFILE),
        patch(_SUPPORTS, side_effect=AssertionError("probe must be skipped")),
    ):
        result = install_agent("kiro_dev", provider="grok_cli")
    # grok path writes no file and returns success; the point is it did NOT
    # refuse with the resolver-support message.
    assert "resolver support" not in (result.message or "")


def test_install_legacy_profile_never_probes(monkeypatch):
    monkeypatch.delenv("CAO_SKIP_RESOLVER_PROBE", raising=False)
    with (
        patch(_READ_SRC, return_value=_LEGACY_PROFILE),
        patch(_SUPPORTS, side_effect=AssertionError("legacy profile must not probe")),
    ):
        result = install_agent("plain", provider="grok_cli")
    assert result.success is True
