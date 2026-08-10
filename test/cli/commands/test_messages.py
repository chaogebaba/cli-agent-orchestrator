"""WPQ11 parked-message CLI forwarding and JSON rendering.

F134 additions: --json flag emits compact machine-readable output.
"""

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


_SAMPLE_PAYLOAD = {
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


def test_parked_list_forwards_raw_selectors_and_stays_json_only():
    with patch(
        "cli_agent_orchestrator.cli.commands.messages.cao_http.get",
        return_value=_response(_SAMPLE_PAYLOAD),
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
    assert json.loads(result.output) == _SAMPLE_PAYLOAD
    assert get.call_args.kwargs["params"] == {
        "to": "abcdef12",
        "limit": 25,
        "status": "parked",
        "generation": "not-an-int",
        "original_receiver_id": "BAD",
        "audit_browse": True,
    }


def test_list_default_output_is_indented():
    """Without --json the output is pretty-printed (indent=2)."""
    with patch(
        "cli_agent_orchestrator.cli.commands.messages.cao_http.get",
        return_value=_response(_SAMPLE_PAYLOAD),
    ):
        result = CliRunner().invoke(
            cli,
            ["messages", "list", "--to", "abcdef12"],
        )

    assert result.exit_code == 0
    expected = json.dumps(_SAMPLE_PAYLOAD, indent=2) + "\n"
    assert result.output == expected


def test_list_json_flag_emits_compact_json():
    """--json emits a single compact JSON document parseable by json.loads."""
    with patch(
        "cli_agent_orchestrator.cli.commands.messages.cao_http.get",
        return_value=_response(_SAMPLE_PAYLOAD),
    ):
        result = CliRunner().invoke(
            cli,
            ["messages", "list", "--to", "abcdef12", "--json"],
        )

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == _SAMPLE_PAYLOAD
    # Compact means no leading whitespace on lines (no indent)
    assert "\n" not in result.output.strip()
