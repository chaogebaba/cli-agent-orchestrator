"""WPQ11 parked-message CLI forwarding and JSON rendering."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli


def _response(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


def test_parked_list_forwards_raw_selectors_and_stays_json_only():
    payload = {
        "items": [
            {
                "id": 7,
                "status": "parked",
                "owner_receiver_id": "abcdef12",
                "owner_generation": 3,
                "dead_to_successor": True,
            }
        ],
        "next_after_id": None,
        "has_more": False,
    }
    with patch(
        "cli_agent_orchestrator.cli.commands.messages.cao_http.get",
        return_value=_response(payload),
    ) as get:
        result = CliRunner().invoke(
            cli,
            [
                "messages",
                "list",
                "--to",
                "abcdef12",
                "--status",
                "parked",
                "--generation",
                "not-an-int",
                "--original-receiver-id",
                "BAD",
                "--audit-browse",
            ],
        )

    assert result.exit_code == 0
    assert json.loads(result.output) == payload
    assert get.call_args.kwargs["params"] == {
        "to": "abcdef12",
        "limit": 25,
        "status": "parked",
        "generation": "not-an-int",
        "original_receiver_id": "BAD",
        "audit_browse": True,
    }
