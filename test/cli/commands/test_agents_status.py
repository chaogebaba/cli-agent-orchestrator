"""Acceptance tests for ``cao agents status`` (wp-agents-status)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch

import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import agents as agents_command
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.utils.http import EndpointConfigurationError


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def _fleet() -> dict:
    return {
        "session_name": "cao-status",
        "terminals": [
            {
                "id": "aaaaaaaa",
                "profile": "worker_a",
                "provider": "codex",
                "window_index": 4,
                "window_name": "worker-a",
                "parent_id": "99999999",
                "depth": 1,
                "orphan": False,
                "status": "processing",
                "since_last_input": 5.9,
                "lifecycle": "ephemeral",
                "reparented_from": None,
            },
            {
                "id": "bbbbbbbb",
                "profile": "worker_b",
                "provider": "codex",
                "window_index": 2,
                "window_name": "worker-b",
                "parent_id": "99999999",
                "depth": 1,
                "orphan": True,
                "status": "idle",
                "since_last_input": 65.2,
                "lifecycle": "ephemeral",
                "reparented_from": None,
            },
            {
                "id": "cccccccc",
                "profile": "base",
                "provider": "claude_code",
                "window_index": None,
                "window_name": None,
                "parent_id": None,
                "depth": 0,
                "orphan": False,
                "status": "error",
                "since_last_input": 172800.0,
                "lifecycle": "sticky",
                "reparented_from": None,
            },
            {
                "id": "dddddddd",
                "profile": "worker_d",
                "provider": "grok",
                "window_index": 1,
                "window_name": "worker-d",
                "parent_id": "99999999",
                "depth": 1,
                "orphan": False,
                "status": "completed",
                "since_last_input": 3700.0,
                "lifecycle": "ephemeral",
                "reparented_from": "88888888",
            },
        ],
    }


def test_ac1_rows_preserve_payload_order_and_status_and_ac2_header_partition(monkeypatch):
    payload = _fleet()
    response = _response(payload)
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    with patch.object(agents_command.cao_http, "get", return_value=response) as get:
        result = CliRunner().invoke(cli, ["agents", "status", "cao-status"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert (
        lines[0]
        == "Session: cao-status — 4 terminals · 1 idle · 1 processing · 1 completed · 1 error · 1 orphan"
    )
    assert lines[1].startswith("IDX  ID")
    rows = [line.split() for line in lines[2:]]
    assert [row[1] for row in rows] == [item["id"] for item in payload["terminals"]]
    assert [row[3] for row in rows] == [item["status"] for item in payload["terminals"]]
    assert [row[4] for row in rows] == ["5s", "1m", "2d", "1h"]
    get.assert_called_once_with("/sessions/cao-status/fleet", timeout=10.0)


def test_ac1_age_ladder_boundaries():
    assert agents_command._format_age(None) == "—"
    assert agents_command._format_age(59.99) == "59s"
    assert agents_command._format_age(3599.99) == "59m"
    assert agents_command._format_age(172799.99) == "47h"
    assert agents_command._format_age(172800) == "2d"


def test_ac3_json_is_the_raw_payload(monkeypatch):
    payload = _fleet()
    monkeypatch.setattr(agents_command.cao_http, "get", lambda *args, **kwargs: _response(payload))
    result = CliRunner().invoke(cli, ["agents", "status", "cao-status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload


def test_ac4_resolution_order_valid_terminal_and_explicit_wins(monkeypatch):
    payload = _fleet()
    terminal = _response({"session_name": "from-terminal"})
    fleet = _response(payload)
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    with patch.object(agents_command.cao_http, "get", side_effect=[terminal, fleet]) as get:
        result = CliRunner().invoke(cli, ["agents", "status"])
    assert result.exit_code == 0, result.output
    assert get.call_args_list[0].args == ("/terminals/11111111",)
    assert get.call_args_list[0].kwargs == {"timeout": 10.0}
    assert get.call_args_list[1].args == ("/sessions/from-terminal/fleet",)
    assert get.call_args_list[1].kwargs == {"timeout": 10.0}

    with patch.object(agents_command.cao_http, "get", return_value=fleet) as get:
        result = CliRunner().invoke(cli, ["agents", "status", "explicit"])
    assert result.exit_code == 0, result.output
    get.assert_called_once_with("/sessions/explicit/fleet", timeout=10.0)


def test_ac4_missing_malformed_and_resolution_failures(monkeypatch):
    runner = CliRunner()
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    result = runner.invoke(cli, ["agents", "status"])
    assert result.exit_code == 1
    assert "session_name required outside a CAO terminal" in result.output

    monkeypatch.setenv("CAO_TERMINAL_ID", "not-a-terminal")
    result = runner.invoke(cli, ["agents", "status"])
    assert result.exit_code == 1
    assert "session_name required outside a CAO terminal" in result.output

    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    error = requests.HTTPError("404")
    with patch.object(agents_command.cao_http, "get", side_effect=error):
        result = runner.invoke(cli, ["agents", "status"])
    assert result.exit_code == 1
    assert "could not resolve the session for terminal 11111111" in result.output

    with patch.object(agents_command.cao_http, "get", side_effect=requests.ConnectionError("down")):
        result = runner.invoke(cli, ["agents", "status"])
    assert result.exit_code == 1
    assert "cao-server" in result.output

    with patch.object(
        agents_command.cao_http,
        "get",
        side_effect=EndpointConfigurationError("CAO_ENDPOINT must be a loopback http origin"),
    ):
        result = runner.invoke(cli, ["agents", "status"])
    assert result.exit_code == 1
    assert "endpoint binding" in result.output.lower()
    assert "cao-server" not in result.output

    with patch.object(agents_command.cao_http, "get", return_value=_response({"id": "11111111"})):
        result = runner.invoke(cli, ["agents", "status"])
    assert result.exit_code == 1
    assert "could not resolve the session for terminal 11111111" in result.output


def test_ac5_rows_render_missing_idx_parent_window_and_warning(monkeypatch):
    payload = _fleet()
    monkeypatch.setattr(agents_command.cao_http, "get", lambda *args, **kwargs: _response(payload))
    result = CliRunner().invoke(cli, ["agents", "status", "cao-status"])
    assert result.exit_code == 0, result.output
    rows = [line.split() for line in result.output.splitlines()[2:]]
    assert rows[1][-1] == "⚠"
    assert rows[2] == ["—", "cccccccc", "base", "error", "2d", "—", "—", "⚠"]
    assert rows[3][-1] == "⚠"


def test_ac7_unknown_and_server_down_keep_json_stdout_empty(monkeypatch):
    runner = CliRunner()
    error = requests.HTTPError("404")
    with patch.object(agents_command.cao_http, "get", side_effect=error):
        result = runner.invoke(cli, ["agents", "status", "missing", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "missing" in result.output

    with patch.object(agents_command.cao_http, "get", side_effect=requests.ConnectionError("down")):
        result = runner.invoke(cli, ["agents", "status", "cao-status", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "cao-server" in result.output
