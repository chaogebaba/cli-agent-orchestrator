"""Captured-shape tests for the base-refresh transcript observer."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[3] / "probes" / "base-refresh-observer.py"
TOKEN = "V2-ANSWER-TOKEN"


def codex_records(case: str) -> list[dict]:
    pre_token = TOKEN if case != "missing_pre" else "old-value"
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "refresh-read",
                "output": pre_token,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "[FRESH] fork task"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "answer-send",
                "input": {
                    "command": "mcp__cao_mcp_server__send_message",
                    "arguments": {"message": TOKEN},
                },
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "answer-send",
                "output": {"message": TOKEN},
            },
        },
    ]
    if case == "leak":
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "leak-read",
                    "arguments": {"cmd": f"rg {TOKEN} file.txt"},
                },
            }
        )
    return records


def grok_records(case: str) -> list[dict]:
    pre_token = TOKEN if case != "missing_pre" else "old-value"
    records = [
        {"type": "tool_result", "tool_call_id": "refresh-read", "content": pre_token},
        {
            "type": "user",
            "content": [{"text": "<user_query>\n[FRESH] fork task"}],
        },
        {
            "type": "assistant",
            "tool_calls": [
                {
                    "id": "answer-send",
                    "name": "use_tool",
                    "arguments": json.dumps(
                        {
                            "tool_name": "cao-mcp-server__send_message",
                            "arguments": {"message": TOKEN},
                        }
                    ),
                }
            ],
        },
        {"type": "tool_result", "tool_call_id": "answer-send", "content": TOKEN},
    ]
    if case == "leak":
        records.append(
            {
                "type": "assistant",
                "tool_calls": [
                    {
                        "id": "leak-read",
                        "name": "read_file",
                        "arguments": json.dumps({"query": TOKEN}),
                    }
                ],
            }
        )
    return records


@pytest.mark.parametrize("provider,records", [("codex", codex_records), ("grok", grok_records)])
@pytest.mark.parametrize(
    "case,expected_code,error",
    [
        ("answer_only", 0, ""),
        ("leak", 1, "FAIL fork-time read evidence"),
        ("missing_pre", 1, "FAIL positive-control"),
    ],
)
def test_observer_nested_send_exemption(provider, records, case, expected_code, error, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records(case)))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), provider, str(transcript), TOKEN],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_code
    assert error in result.stderr
    if expected_code == 0:
        assert json.loads(result.stdout)["post_leaks"] == 0
