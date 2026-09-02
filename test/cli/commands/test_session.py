"""Tests for the session CLI command."""

import json
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
            "results": [
                {
                    "terminal_id": "term-a",
                    "status": "rebound",
                    "retryable": False,
                    "error_code": None,
                    "interrupted_turn": True,
                    "requires_supervisor_reconciliation": True,
                }
            ],
            "manifest_error": None,
        }
        result = runner.invoke(
            session,
            [
                "recover",
                "cao-test",
                "--reason",
                "provider-reauth",
                "--terminal",
                "term-a",
                "--interrupt",
                "--json",
            ],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["results"][0]["interrupted_turn"] is True
        assert (
            result.output == json.dumps(mock_post.return_value.json.return_value, indent=2) + "\n"
        )
        assert "IDLE until messaged" not in result.output
        assert mock_post.call_args.kwargs["json"] == {
            "reason": "provider-reauth",
            "provider": "codex",
            "terminal_ids": ["term-a"],
            "interrupt": True,
            "acknowledge_ownership": False,
        }

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_recover_rebound_prints_idle_until_messaged_reminder(self, mock_post, runner):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [{"terminal_id": "term-a", "status": "rebound", "error_code": None}],
            "manifest_error": None,
        }

        result = runner.invoke(
            session,
            ["recover", "cao-test", "--reason", "provider-reauth", "--terminal", "term-a"],
        )

        assert result.exit_code == 0
        assert (
            result.output.count(
                "NOTE: recovered terminal term-a is IDLE until messaged — send a continue nudge."
            )
            == 1
        )

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_recover_rebound_with_error_still_prints_reminder(self, mock_post, runner):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [
                {
                    "terminal_id": "term-a",
                    "status": "rebound",
                    "error_code": "delivery_guard_release_failed",
                }
            ],
            "manifest_error": None,
        }

        result = runner.invoke(
            session,
            ["recover", "cao-test", "--reason", "provider-reauth", "--terminal", "term-a"],
        )

        assert result.exit_code == 0
        assert "term-a: rebound [delivery_guard_release_failed]" in result.output
        assert (
            result.output.count(
                "NOTE: recovered terminal term-a is IDLE until messaged — send a continue nudge."
            )
            == 1
        )

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_recover_skipped_busy_prints_no_reminder(self, mock_post, runner):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [{"terminal_id": "term-a", "status": "skipped_busy"}],
            "manifest_error": None,
        }

        result = runner.invoke(
            session,
            ["recover", "cao-test", "--reason", "provider-reauth", "--terminal", "term-a"],
        )

        assert result.exit_code == 0
        assert "IDLE until messaged" not in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_recover_resume_failed_prints_no_reminder(self, mock_post, runner):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [{"terminal_id": "term-a", "status": "resume_failed"}],
            "manifest_error": None,
        }

        result = runner.invoke(
            session,
            ["recover", "cao-test", "--reason", "provider-reauth", "--terminal", "term-a"],
        )

        assert result.exit_code == 0
        assert "IDLE until messaged" not in result.output

    def test_recover_requires_explicit_reason(self, runner):
        result = runner.invoke(session, ["recover", "cao-test"])
        assert result.exit_code != 0
        assert "--reason" in result.output

    def test_acknowledge_ownership_requires_exactly_one_terminal(self, runner):
        result = runner.invoke(
            session,
            [
                "recover",
                "cao-test",
                "--reason",
                "provider-reauth",
                "--acknowledge-ownership",
            ],
        )
        assert result.exit_code != 0
        assert "exactly one --terminal" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_content_recovery_default_on_payload_and_sent_render(self, mock_post, runner):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [
                {
                    "terminal_id": "term-a",
                    "status": "rebound",
                    "error_code": None,
                    "nudge": {"status": "sent", "nudge_message_id": 41},
                }
            ],
            "manifest_error": None,
        }
        result = runner.invoke(
            session,
            ["recover", "cao-test", "--reason", "content-flag", "--terminal", "term-a"],
        )
        assert result.exit_code == 0
        assert "term-a: nudge sent [message 41]" in result.output
        assert mock_post.call_args.kwargs["json"] == {
            "reason": "content-flag",
            "provider": "codex",
            "terminal_ids": ["term-a"],
            "interrupt": False,
            "acknowledge_ownership": False,
            "show": False,
            "force": False,
        }

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_content_recovery_no_nudge_and_caller_skip_render_idle_reminder(
        self, mock_post, runner
    ):
        for skip_reason in ("no-nudge-flag", "caller-unresolvable"):
            mock_post.return_value.json.return_value = {
                "session": "cao-test",
                "results": [
                    {
                        "terminal_id": "term-a",
                        "status": "rebound",
                        "error_code": None,
                        "nudge": {"status": "skipped", "skip_reason": skip_reason},
                    }
                ],
                "manifest_error": None,
            }
            args = ["recover", "cao-test", "--reason", "content-flag"]
            if skip_reason == "no-nudge-flag":
                args.append("--no-nudge")
            result = runner.invoke(session, args)
            assert result.exit_code == 0
            assert f"nudge skipped [{skip_reason}]" in result.output
            assert "IDLE until messaged" in result.output
        assert mock_post.call_args_list[0].kwargs["json"]["nudge"] is False
        assert "nudge" not in mock_post.call_args_list[1].kwargs["json"]

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @pytest.mark.parametrize("status", ["failed", "not_attempted"])
    def test_content_recovery_closed_status_render(self, mock_post, runner, status):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [
                {
                    "terminal_id": "term-a",
                    "status": "rebound" if status == "failed" else "resume_failed",
                    "error_code": None,
                    "nudge": {"status": status},
                }
            ],
            "manifest_error": None,
        }
        result = runner.invoke(session, ["recover", "cao-test", "--reason", "content-flag"])
        assert result.exit_code == 0
        assert f"nudge {status}" in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    @pytest.mark.parametrize(
        "nudge",
        [
            {"status": "failed", "nudge_message_id": 1},
            {"status": "sent"},
            {"status": "skipped"},
            {"status": "failed", "skip_reason": "no-nudge-flag"},
        ],
    )
    def test_content_recovery_rejects_nudge_field_presence_violations(
        self, mock_post, runner, nudge
    ):
        mock_post.return_value.json.return_value = {
            "session": "cao-test",
            "results": [{"terminal_id": "term-a", "status": "rebound", "nudge": nudge}],
            "manifest_error": None,
        }
        result = runner.invoke(session, ["recover", "cao-test", "--reason", "content-flag"])
        assert result.exit_code != 0
        assert "invalid recovery nudge result" in result.output

    def test_content_recovery_flag_validation(self, runner):
        assert (
            runner.invoke(
                session, ["recover", "cao-test", "--reason", "provider-reauth", "--force"]
            ).exit_code
            != 0
        )
        assert (
            runner.invoke(
                session,
                ["recover", "cao-test", "--reason", "content-flag", "--provider", "grok_cli"],
            ).exit_code
            != 0
        )


class TestLegacyStatus:
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_status_v1_json(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "schema_version": "cao.session-status/v1",
            "session": {"name": "cao-test"},
            "backend_present": True,
            "epoch": None,
            "ready_bases": [],
            "warm_intents": [],
            "quarantined": [],
            "ledger": {"available": False, "count": None},
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
            "session": {"name": "cao-test"},
            "backend_present": False,
            "epoch": {"count": 2},
            "ready_bases": [{}],
            "warm_intents": [],
            "quarantined": [{}, {}],
            "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response

    @pytest.mark.skip(
        reason=(
            "Fork: `cao session status` is the cao.session-status/v1 lifecycle "
            "projection — the --terminal/--workers selectors this upstream test "
            "drives were removed (see test_removed_legacy_selectors), so there is no "
            "client-side conductor pick to pin here. The oldest-first server contract "
            "it guards is covered by test/clients/test_database.py and "
            "test/services/test_session_service.py::"
            "test_list_sessions_reports_the_creator_as_owner."
        )
    )
    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_status_resolves_the_conductor_from_index_zero(self, mock_get, runner):
        """``status`` must label index 0 as the Conductor, not any other terminal.

        The sibling tests above return the SAME terminal payload for every
        ``requests.get``, so they never observe *which* id was fetched — reading
        ``terminals[-1]`` instead of ``terminals[0]`` passes all of them. This
        routes by URL so the id actually gets pinned, which is what makes the
        oldest-first guarantee on ``GET /sessions/{name}/terminals`` enforceable
        from the client side rather than only documented.
        """
        listing = MagicMock(status_code=200)
        # Fork: this CLI reads `status` straight off the listing rows (upstream
        # re-fetched each terminal for it), so the listing payload must carry it.
        listing.json.return_value = [
            {
                "id": "cond1234",
                "agent_profile": "conductor",
                "provider": "kiro_cli",
                "status": "idle",
            },
            {
                "id": "work5678",
                "agent_profile": "dev",
                "provider": "kiro_cli",
                "status": "processing",
            },
        ]
        by_id = {
            "cond1234": {
                "id": "cond1234",
                "agent_profile": "conductor",
                "provider": "kiro_cli",
                "status": "idle",
            },
            "work5678": {
                "id": "work5678",
                "agent_profile": "dev",
                "provider": "kiro_cli",
                "status": "processing",
            },
        }

        def _route(url, *args, **kwargs):
            if url.endswith("/terminals"):
                return listing
            if "/output" in url:
                out = MagicMock(status_code=200)
                out.json.return_value = {"output": None}
                return out
            resp = MagicMock(status_code=200)
            resp.json.return_value = by_id[url.rstrip("/").rsplit("/", 1)[-1]]
            return resp

        mock_get.side_effect = _route

        result = runner.invoke(session, ["status", "cao-test", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        conductor = payload.get("conductor") or payload
        assert (
            conductor["id"] == "cond1234"
        ), f"conductor must be terminals[0]; got {conductor['id']}"
        result = runner.invoke(session, ["status", "cao-test"])
        assert result.exit_code == 0
        assert "Backend present: false" in result.output
        assert "Epoch: 2" in result.output
        assert "Terminal:" not in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_status_uses_single_v1_endpoint(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "session": {"name": "cao-test"},
            "backend_present": True,
            "epoch": None,
            "ready_bases": [],
            "warm_intents": [],
            "quarantined": [],
            "ledger": {"available": False, "count": None},
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
            "session": {"name": "cao-test"},
            "backend_present": False,
            "manifest": None,
            "manifest_error": "no_terminals",
            "epoch": None,
            "ready_bases": [{"base_name": "codex"}],
            "warm_intents": [],
            "quarantined": [],
            "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        result = runner.invoke(session, ["status", "cao-test", "--json"])
        assert result.exit_code == 0
        assert '"manifest_error": "no_terminals"' in result.output

    @patch("cli_agent_orchestrator.cli.commands.session.requests.get")
    def test_human_ledger_never_renders_zero(self, mock_get, runner):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "session": {"name": "cao-test"},
            "backend_present": True,
            "epoch": None,
            "ready_bases": [],
            "warm_intents": [],
            "quarantined": [],
            "ledger": {"available": False, "count": None},
        }
        mock_get.return_value = response
        result = runner.invoke(session, ["status", "cao-test"])
        assert "Ledger: unavailable" in result.output
        assert "Ledger: 0" not in result.output


class TestStart:
    """`cao session start` — F565 (#421): --yolo mirrors `cao launch --yolo`
    by resolving allowed_tools to '*', the ONLY way to reproduce the
    unrestricted supervisor seat through the canonical lifecycle verb."""

    def _ok_post(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "session": {"name": "cao-test"},
            "supervisor_terminal": {"name": "cao-test-sup"},
        }
        return resp

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_yolo_forwards_wildcard_allowed_tools(self, mock_post, runner):
        mock_post.return_value = self._ok_post()

        result = runner.invoke(
            session,
            ["start", "cao-test", "--agents", "chao_supervisor", "--yolo"],
        )

        assert result.exit_code == 0
        assert mock_post.call_args.kwargs["params"]["allowed_tools"] == "*"

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_without_yolo_omits_allowed_tools(self, mock_post, runner):
        mock_post.return_value = self._ok_post()

        result = runner.invoke(
            session,
            ["start", "cao-test", "--agents", "chao_supervisor"],
        )

        assert result.exit_code == 0
        assert "allowed_tools" not in mock_post.call_args.kwargs["params"]

    @patch("cli_agent_orchestrator.cli.commands.session.requests.post")
    def test_tools_flag_still_forwarded_verbatim(self, mock_post, runner):
        mock_post.return_value = self._ok_post()

        result = runner.invoke(
            session,
            ["start", "cao-test", "--agents", "dev", "--tools", "@cao-mcp-server,fs_read"],
        )

        assert result.exit_code == 0
        assert mock_post.call_args.kwargs["params"]["allowed_tools"] == "@cao-mcp-server,fs_read"

    def test_yolo_and_tools_are_mutually_exclusive(self, runner):
        result = runner.invoke(
            session,
            ["start", "cao-test", "--agents", "dev", "--yolo", "--tools", "@cao-mcp-server"],
        )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


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
