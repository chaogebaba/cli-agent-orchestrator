import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from pydantic import ValidationError
import pytest

from cli_agent_orchestrator.api.main import SessionRecoverRequest
from cli_agent_orchestrator.cli.commands.session import session


FIXTURES = Path(__file__).parents[2] / "fixtures"


def _response(payload):
    class Response:
        def raise_for_status(self): pass
        def json(self): return payload
    return Response()


def test_d1_provider_reauth_json_and_cli_golden_are_byte_stable():
    payload_text = (FIXTURES / "wp2s2_provider_reauth_golden.json").read_text().strip()
    payload = json.loads(payload_text)
    with patch("cli_agent_orchestrator.cli.commands.session.requests.post",
               return_value=_response(payload)) as post:
        result = CliRunner().invoke(session, ["recover", "cao-s", "--reason", "provider-reauth"])
    assert result.exit_code == 0
    assert result.output == (FIXTURES / "wp2s2_provider_reauth_cli.txt").read_text()
    assert post.call_args.kwargs["json"] == {
        "reason": "provider-reauth", "provider": "codex", "terminal_ids": [],
        "interrupt": False, "acknowledge_ownership": False,
    }
    assert json.dumps(payload, separators=(",", ":")) == payload_text


def test_d1_epoch_cli_payload_and_rendering():
    payload = {"session": "cao-s", "reason": "epoch", "results": [
        {"base": "codex", "terminal_id": "new", "status": "resumed", "error_code": None}],
        "respawn_candidates": [{"intent_id": "i", "profile": "dev", "base": "codex",
                                "base_state": "resumed"}], "manifest_error": None}
    with patch("cli_agent_orchestrator.cli.commands.session.requests.post",
               return_value=_response(payload)) as post:
        result = CliRunner().invoke(session, ["recover", "cao-s", "--reason", "epoch",
                                                     "--base", "codex"])
    assert result.exit_code == 0
    assert "Recovery: cao-s (epoch)" in result.output
    assert "offer i: dev from codex [resumed]" in result.output
    assert post.call_args.kwargs["json"]["base_names"] == ["codex"]


@pytest.mark.parametrize("args", [
    ["--terminal", "t"], ["--interrupt"], ["--acknowledge-ownership", "--terminal", "t"],
])
def test_d1_epoch_rejects_reauth_only_cli_flags(args):
    result = CliRunner().invoke(session, ["recover", "cao-s", "--reason", "epoch", *args])
    assert result.exit_code != 0
    assert "rejects" in result.output


def test_d1_api_reason_and_flag_validation():
    assert SessionRecoverRequest(reason="epoch", base_names=["b"]).reason == "epoch"
    with pytest.raises(ValidationError):
        SessionRecoverRequest(reason="unknown")


def test_close_cli_contract():
    payload = {"session": "cao-s", "session_closed": True,
               "terminals": [{"terminal_id": "t", "status": "deleted"}],
               "bases": [{"base": "b", "status": "retired"}],
               "intents": {"removed": 1, "retained": 0}}
    with patch("cli_agent_orchestrator.cli.commands.session.requests.post",
               return_value=_response(payload)) as post:
        result = CliRunner().invoke(session, ["close", "cao-s", "--force"])
    assert result.exit_code == 0
    assert "session_closed=true" in result.output
    assert post.call_args.kwargs["params"] == {"keep_bases": "false", "force": "true"}
