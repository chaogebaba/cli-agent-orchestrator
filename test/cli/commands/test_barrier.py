"""Operator callback-barrier CLI."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli


def _response(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = str(payload)
    return response


def test_status_requires_typed_selector():
    result = CliRunner().invoke(cli, ["barrier", "status"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_numeric_label_stays_label(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    with patch(
        "cli_agent_orchestrator.cli.commands.barrier.cao_http.get",
        return_value=_response({"id": 3, "label": "123"}),
    ) as get:
        result = CliRunner().invoke(cli, ["barrier", "status", "--label", "123"])
    assert result.exit_code == 0
    assert get.call_args.kwargs["params"] == {
        "barrier_label": "123",
        "owner": "aaaaaaaa",
    }


def test_cancel_requires_yes_then_posts():
    runner = CliRunner()
    denied = runner.invoke(cli, ["barrier", "cancel", "--id", "3"])
    assert denied.exit_code != 0 and "requires --yes" in denied.output
    with patch(
        "cli_agent_orchestrator.cli.commands.barrier.cao_http.post",
        return_value=_response({"id": 3, "state": "CANCELLED"}),
    ) as post:
        allowed = runner.invoke(cli, ["barrier", "cancel", "--id", "3", "--yes"])
    assert allowed.exit_code == 0
    assert post.call_args.kwargs["params"] == {"barrier_id": 3}
