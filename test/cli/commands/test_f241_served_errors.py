"""F241 (#64) family sweep — no CLI surface reports a SERVED error as a connection failure.

#64's bug-family clause names six sites; `launch.py` is covered by its own tests in
`test_launch.py`. This module covers the five siblings: a 4xx/5xx the server ANSWERED
must print the served status and body, never "Failed to connect to cao-server", which
reads as "restart the server" — the one action that kills live CAO sessions.

Revert-sensitive: dropping any `except requests.exceptions.HTTPError` arm puts the
HTTPError back in the blanket `RequestException` arm (or, for `terminal.py`, lets it
escape as a bare traceback) and the matching test fails.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.session import session
from cli_agent_orchestrator.cli.commands.shutdown import shutdown
from cli_agent_orchestrator.cli.commands.terminal import terminal


def served(status_code, body=None, text=""):
    """A response whose raise_for_status() raises the HTTPError requests would."""
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    if body is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = body
    response.raise_for_status.side_effect = requests.exceptions.HTTPError(
        f"{status_code} Error", response=response
    )
    return response


def assert_served_not_connect(result, status, needle):
    assert result.exit_code != 0
    assert "connect" not in result.output.lower(), result.output
    assert f"HTTP {status}" in result.output, result.output
    assert needle in result.output, result.output


# ---- session.py -----------------------------------------------------------


def test_session_list_reports_served_error():
    runner = CliRunner()
    with patch("cli_agent_orchestrator.cli.commands.session.cao_http.get") as mock_get:
        mock_get.return_value = served(500, {"detail": "registry unavailable"})
        result = runner.invoke(session, ["list"])
    assert_served_not_connect(result, 500, "registry unavailable")


def test_session_send_reports_served_error():
    """The status GET inside send()'s try raises HTTPError on a served 404."""
    runner = CliRunner()
    with patch("cli_agent_orchestrator.cli.commands.session.cao_http.get") as mock_get:
        mock_get.return_value = served(404, {"detail": "Terminal 't-1' not found"})
        result = runner.invoke(
            session, ["send", "cao-sess", "hello", "--terminal", "t-1", "--async"]
        )
    assert_served_not_connect(result, 404, "Terminal 't-1' not found")


# ---- shutdown.py ----------------------------------------------------------


def test_shutdown_list_reports_served_error():
    runner = CliRunner()
    with patch("cli_agent_orchestrator.cli.commands.shutdown.cao_http.get") as mock_get:
        mock_get.return_value = served(503, {"detail": "server draining"})
        result = runner.invoke(shutdown, ["--all"])
    assert_served_not_connect(result, 503, "server draining")


def test_shutdown_delete_reports_served_error():
    runner = CliRunner()
    with (
        patch("cli_agent_orchestrator.cli.commands.shutdown.cao_http.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.shutdown.cao_http.delete") as mock_delete,
    ):
        listing = MagicMock()
        listing.status_code = 200
        listing.raise_for_status.return_value = None
        listing.json.return_value = [{"name": "cao-doomed"}]
        mock_get.return_value = listing
        mock_delete.return_value = served(500, {"detail": "teardown lease held"})
        result = runner.invoke(shutdown, ["--session", "cao-doomed"])
    assert_served_not_connect(result, 500, "teardown lease held")


# ---- terminal.py ----------------------------------------------------------


def test_terminal_restore_reports_served_error(tmp_path):
    """terminal.py only caught ConnectionError, so a served 5xx escaped as a traceback."""
    import json as _json

    snapshot = tmp_path / "t-1.snapshot.json"
    snapshot.write_text(
        _json.dumps(
            {
                "session_name": "cao-sess",
                "window_name": "win-0",
                "working_directory": str(tmp_path),
            }
        )
    )
    (tmp_path / "t-1.scrollback").write_text("scrollback\n")

    runner = CliRunner()
    with (
        patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path),
        patch("cli_agent_orchestrator.cli.commands.terminal.cao_http.get") as mock_get,
    ):
        mock_get.return_value = served(500, {"detail": "session index corrupt"})
        result = runner.invoke(terminal, ["restore", "t-1"])
    assert_served_not_connect(result, 500, "session index corrupt")


# ---- the truthful arm is untouched ----------------------------------------


@pytest.mark.parametrize(
    "group,args,target",
    [
        (session, ["list"], "cli_agent_orchestrator.cli.commands.session.cao_http.get"),
        (shutdown, ["--all"], "cli_agent_orchestrator.cli.commands.shutdown.cao_http.get"),
    ],
)
def test_real_transport_failure_still_says_failed_to_connect(group, args, target):
    runner = CliRunner()
    with patch(target) as mock_call:
        mock_call.side_effect = requests.exceptions.ConnectionError("Connection refused")
        result = runner.invoke(group, args)
    assert result.exit_code != 0
    assert "Failed to connect to cao-server" in result.output
