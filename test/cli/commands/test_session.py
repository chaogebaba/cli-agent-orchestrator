"""Tests for the session CLI command."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.session import session


@pytest.fixture
def runner():
    return CliRunner()


class TestListSessions:
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_success(self, mock_get, runner):
        """Test listing sessions with conductor info."""
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = [{"name": "cao-test"}]
        terminals_resp = MagicMock(status_code=200)
        terminals_resp.json.return_value = [{"id": "abc12345"}]
        terminal_resp = MagicMock(status_code=200)
        terminal_resp.json.return_value = {
            "id": "abc12345",
            "agent_profile": "dev",
            "provider": "kiro_cli",
            "status": "idle",
        }
        mock_get.side_effect = [sessions_resp, terminals_resp, terminal_resp]

        result = runner.invoke(session, ["list"])

        assert result.exit_code == 0
        assert "cao-test" in result.output
        assert "idle" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_empty(self, mock_get, runner):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])

        result = runner.invoke(session, ["list"])

        assert result.exit_code == 0
        assert "No active sessions" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_empty_json(self, mock_get, runner):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])

        result = runner.invoke(session, ["list", "--json"])

        assert result.exit_code == 0
        assert result.output.strip() == "[]"

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_json(self, mock_get, runner):
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = [{"name": "cao-test"}]
        terminals_resp = MagicMock(status_code=200)
        terminals_resp.json.return_value = [{"id": "abc12345"}]
        terminal_resp = MagicMock(status_code=200)
        terminal_resp.json.return_value = {
            "id": "abc12345",
            "agent_profile": "dev",
            "provider": "kiro_cli",
            "status": "idle",
        }
        mock_get.side_effect = [sessions_resp, terminals_resp, terminal_resp]

        result = runner.invoke(session, ["list", "--json"])

        assert result.exit_code == 0
        assert '"session": "cao-test"' in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_server_down(self, mock_get, runner):
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")

        result = runner.invoke(session, ["list"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_terminal_fetch_error_skips_session(self, mock_get, runner):
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = [{"name": "cao-test"}]
        mock_get.side_effect = [sessions_resp, requests.exceptions.ConnectionError("refused")]

        result = runner.invoke(session, ["list"])

        assert result.exit_code == 0

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_list_sessions_no_conductor(self, mock_get, runner):
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = [{"name": "cao-test"}]
        terminals_resp = MagicMock(status_code=200)
        terminals_resp.json.return_value = []
        mock_get.side_effect = [sessions_resp, terminals_resp]

        result = runner.invoke(session, ["list"])

        assert result.exit_code == 0
        assert "N/A" in result.output


class TestRecover:
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_recover_json_contract(self, mock_post, runner):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [{
                "terminal_id": "term-a", "status": "rebound", "retryable": False,
                "error_code": None, "interrupted_turn": True,
                "requires_supervisor_reconciliation": True,
            }],
            "manifest_error": None,
        }
        result = runner.invoke(session, [
            "recover", "cao-test", "--reason", "provider-reauth",
            "--terminal", "term-a", "--interrupt", "--json",
        ])
        assert result.exit_code == 0
        assert __import__("json").loads(result.output)["results"][0]["interrupted_turn"] is True
        assert mock_post.call_args.kwargs["json"] == {
            "reason": "provider-reauth", "provider": "codex",
            "terminal_ids": ["term-a"], "interrupt": True,
            "acknowledge_ownership": False,
        }

    def test_recover_requires_explicit_reason(self, runner):
        result = runner.invoke(session, ["recover", "cao-test"])
        assert result.exit_code != 0
        assert "--reason" in result.output

    def test_acknowledge_ownership_requires_exactly_one_terminal(self, runner):
        result = runner.invoke(session, [
            "recover", "cao-test", "--reason", "provider-reauth",
            "--acknowledge-ownership",
        ])
        assert result.exit_code != 0
        assert "exactly one --terminal" in result.output


class TestLegacyStatus:
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_status_v1_json(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "schema_version": "cao.session-status/v1",
            "session": {"name": "cao-test"}, "backend_present": True,
            "epoch": None, "ready_bases": [], "warm_intents": [],
            "quarantined": [], "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        result = runner.invoke(session, ["status", "cao-test", "--json"])
        assert result.exit_code == 0
        assert '"schema_version": "cao.session-status/v1"' in result.output

    @pytest.mark.parametrize("removed", ["--terminal", "--workers"])
    def test_removed_legacy_selectors_are_usage_errors(self, runner, removed):
        args = ["status", "cao-test", removed]
        if removed == "--terminal":
            args.append("abc12345")
        assert runner.invoke(session, args).exit_code == 2

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_human_render_is_v1_not_conductor_output(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "session": {"name": "cao-test"}, "backend_present": False,
            "epoch": {"count": 2}, "ready_bases": [{}], "warm_intents": [],
            "quarantined": [{}, {}], "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        result = runner.invoke(session, ["status", "cao-test"])
        assert result.exit_code == 0
        assert "Backend present: false" in result.output
        assert "Epoch: 2" in result.output
        assert "Terminal:" not in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_status_uses_single_v1_endpoint(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "session": {"name": "cao-test"}, "backend_present": True,
            "epoch": None, "ready_bases": [], "warm_intents": [],
            "quarantined": [], "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        assert runner.invoke(session, ["status", "cao-test"]).exit_code == 0
        assert mock_get.call_count == 1
        assert mock_get.call_args.args[0].endswith("/sessions/cao-test/status")

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_status_http_error_is_visible(self, mock_get, runner):
        mock_get.side_effect = requests.ConnectionError("refused")
        result = runner.invoke(session, ["status", "cao-test"])
        assert result.exit_code == 1
        assert "failed to fetch session status" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_durable_only_json_is_preserved(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "schema_version": "cao.session-status/v1",
            "session": {"name": "cao-test"}, "backend_present": False,
            "manifest": None, "manifest_error": "no_terminals", "epoch": None,
            "ready_bases": [{"base_name": "codex"}], "warm_intents": [],
            "quarantined": [], "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        result = runner.invoke(session, ["status", "cao-test", "--json"])
        assert result.exit_code == 0
        assert '"manifest_error": "no_terminals"' in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_human_ledger_never_renders_zero(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "session": {"name": "cao-test"}, "backend_present": True,
            "epoch": None, "ready_bases": [], "warm_intents": [],
            "quarantined": [], "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        result = runner.invoke(session, ["status", "cao-test"])
        assert "Ledger: unavailable" in result.output
        assert "Ledger: 0" not in result.output


class TestSend:
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_async(self, mock_get, mock_post, runner):
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        status_resp = MagicMock(status_code=200)
        status_resp.json.return_value = {"status": "idle"}
        mock_get.side_effect = [resolve_resp, status_resp]
        mock_post.return_value = MagicMock(status_code=200)

        result = runner.invoke(session, ["send", "cao-test", "hello", "--async"])

        assert result.exit_code == 0
        assert "Message sent" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_specific_terminal(self, mock_get, mock_post, runner):
        status_resp = MagicMock(status_code=200)
        status_resp.json.return_value = {"status": "idle"}
        mock_get.return_value = status_resp
        mock_post.return_value = MagicMock(status_code=200)

        result = runner.invoke(
            session, ["send", "cao-test", "hello", "--terminal", "xyz99999", "--async"]
        )

        assert result.exit_code == 0
        assert "Message sent" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_server_down(self, mock_get, mock_post, runner):
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        status_resp = MagicMock(status_code=200)
        status_resp.json.return_value = {"status": "idle"}
        mock_get.side_effect = [resolve_resp, status_resp]
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")

        result = runner.invoke(session, ["send", "cao-test", "hello"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_terminal_not_idle(self, mock_get, runner):
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        status_resp = MagicMock(status_code=200)
        status_resp.json.return_value = {"status": "processing"}
        mock_get.side_effect = [resolve_resp, status_resp]

        result = runner.invoke(session, ["send", "cao-test", "hello"])

        assert result.exit_code != 0
        assert "processing" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_resolve_conductor_no_terminals(self, mock_get, runner):
        resolve_resp = MagicMock(status_code=200, json=lambda: [])
        mock_get.return_value = resolve_resp

        result = runner.invoke(session, ["send", "cao-test", "hello"])

        assert result.exit_code != 0
        assert "No terminals found" in result.output


class TestSendSync:
    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_completed(self, mock_get, mock_post, mock_time, runner):
        """Default (sync) mode polls until completed, then prints output."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed"}
        output_resp = MagicMock(status_code=200)
        output_resp.json.return_value = {"output": "The answer is 42"}
        mock_get.side_effect = [resolve_resp, pre_send_status_resp, poll_resp, output_resp]
        mock_post.return_value = MagicMock(status_code=200)
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()

        result = runner.invoke(session, ["send", "cao-test", "question"])

        assert result.exit_code == 0
        assert "The answer is 42" in result.output
        assert "Message sent" not in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_error_status(self, mock_get, mock_post, mock_time, runner):
        """Default (sync) mode detects error status and raises."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "error"}
        mock_get.side_effect = [resolve_resp, pre_send_status_resp, poll_resp]
        mock_post.return_value = MagicMock(status_code=200)
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()

        result = runner.invoke(session, ["send", "cao-test", "question"])

        assert result.exit_code != 0
        assert "ERROR" in result.output

    @patch("cli_agent_orchestrator.utils.terminal.time")
    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_timeout(
        self, mock_get, mock_post, mock_session_time, mock_terminal_time, runner
    ):
        """--timeout raises on expiry."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "processing"}
        mock_get.side_effect = [resolve_resp, pre_send_status_resp, poll_resp]
        mock_post.return_value = MagicMock(status_code=200)
        mock_session_time.time.return_value = 0
        mock_session_time.sleep = MagicMock()
        mock_terminal_time.time.side_effect = [0, 31]
        mock_terminal_time.sleep = MagicMock()

        result = runner.invoke(session, ["send", "cao-test", "question", "--timeout", "30"])

        assert result.exit_code != 0
        assert "Timed out" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_timeout_completes_before_expiry(
        self, mock_get, mock_post, mock_time, runner
    ):
        """--timeout does not interfere when terminal completes in time."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed"}
        output_resp = MagicMock(status_code=200)
        output_resp.json.return_value = {"output": "done"}
        mock_get.side_effect = [resolve_resp, pre_send_status_resp, poll_resp, output_resp]
        mock_post.return_value = MagicMock(status_code=200)
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()

        result = runner.invoke(session, ["send", "cao-test", "question", "--timeout", "60"])

        assert result.exit_code == 0
        assert "done" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_poll_request_exception(self, mock_get, mock_post, mock_time, runner):
        """Poll failure raises ClickException."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        mock_get.side_effect = [
            resolve_resp,
            pre_send_status_resp,
            requests.exceptions.ConnectionError("refused"),
        ]
        mock_post.return_value = MagicMock(status_code=200)
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()

        result = runner.invoke(session, ["send", "cao-test", "question"])

        assert result.exit_code != 0
        assert "Failed to poll terminal status" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_output_fetch_error(self, mock_get, mock_post, mock_time, runner):
        """Output fetch failure after completion is silently ignored."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed"}
        mock_get.side_effect = [
            resolve_resp,
            pre_send_status_resp,
            poll_resp,
            requests.exceptions.ConnectionError("refused"),
        ]
        mock_post.return_value = MagicMock(status_code=200)
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()

        result = runner.invoke(session, ["send", "cao-test", "question"])

        assert result.exit_code == 0

    @patch("cli_agent_orchestrator.cli.commands.session.sys.exit")
    @patch("cli_agent_orchestrator.utils.terminal.time")
    @patch("cli_agent_orchestrator.cli.commands.session.time")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_send_sync_keyboard_interrupt(
        self, mock_get, mock_post, mock_session_time, mock_terminal_time, mock_exit, runner
    ):
        """KeyboardInterrupt during poll calls sys.exit(130)."""
        resolve_resp = MagicMock(status_code=200, json=lambda: [{"id": "abc12345"}])
        pre_send_status_resp = MagicMock(status_code=200)
        pre_send_status_resp.json.return_value = {"status": "idle"}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "processing"}
        output_resp = MagicMock(status_code=200)
        output_resp.json.return_value = {"output": None}
        mock_get.side_effect = [resolve_resp, pre_send_status_resp, poll_resp, output_resp]
        mock_post.return_value = MagicMock(status_code=200)
        mock_session_time.time.return_value = 0
        mock_session_time.sleep = MagicMock()
        mock_terminal_time.time.return_value = 0
        # sleep(1) in poll loop raises KeyboardInterrupt
        mock_terminal_time.sleep.side_effect = KeyboardInterrupt()

        runner.invoke(session, ["send", "cao-test", "question"])

        mock_exit.assert_any_call(130)
