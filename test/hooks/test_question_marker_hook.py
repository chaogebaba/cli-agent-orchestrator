"""F507 layer-1 hook containment + fail-open + storm control (AC7, AC8, AC9)."""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.hooks import question_marker
from cli_agent_orchestrator.hooks.question_marker import main


def _run(event: dict) -> int:
    with patch("sys.stdin", io.StringIO(json.dumps(event))):
        return main()


def test_ac7_containment_no_terminal_id_zero_side_effects(monkeypatch):
    """AC7: CAO_TERMINAL_ID unset + valid event ⇒ exit 0, zero HTTP, zero dead-letter."""
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    event = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}
    with (
        patch.object(question_marker.cao_http, "post") as post,
        patch.object(question_marker, "_deadletter") as deadletter,
    ):
        assert _run(event) == 0
    post.assert_not_called()
    deadletter.assert_not_called()


def test_ac8_fail_open_unreachable_server(monkeypatch, tmp_path):
    """AC8: cao-server unreachable ⇒ exit 0 and exactly one dead-letter line."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(question_marker, "CAO_HOME_DIR", str(tmp_path))
    event = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}
    with (
        patch.object(question_marker, "cao_tmp_dir", return_value=tmp_path),
        patch.object(
            question_marker.cao_http, "post", side_effect=OSError("connection refused")
        ),
        patch.object(question_marker, "get_local_bearer", return_value=None),
    ):
        assert _run(event) == 0
    deadletter = Path(tmp_path) / "hook-deadletter.jsonl"
    assert deadletter.exists()
    lines = deadletter.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["terminal_id"] == "abcd1234"


def test_ac9_storm_control_one_post_per_cooldown(monkeypatch, tmp_path):
    """AC9: a burst of identical open markers produces at most one POST per window."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    event = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}
    response = MagicMock()
    with (
        patch.object(question_marker, "cao_tmp_dir", return_value=tmp_path),
        patch.object(question_marker, "get_local_bearer", return_value=None),
        patch.object(question_marker.cao_http, "post", return_value=response) as post,
    ):
        for _ in range(50):
            assert _run(event) == 0
    assert post.call_count == 1


def test_classify_open_edges():
    assert question_marker._classify(
        {"hook_event_name": "Notification", "notification_type": "permission_prompt"}
    ) == ("question_open", None)
    assert question_marker._classify(
        {"hook_event_name": "Notification", "notification_type": "elicitation_dialog"}
    ) == ("question_open", None)
    assert question_marker._classify(
        {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}
    ) == ("question_open", "AskUserQuestion")


def test_classify_clear_edges():
    assert question_marker._classify(
        {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"}
    ) == ("question_clear", "AskUserQuestion")
    assert question_marker._classify(
        {"hook_event_name": "PostToolUseFailure", "tool_name": "AskUserQuestion"}
    ) == ("question_clear", "AskUserQuestion")
    assert question_marker._classify({"hook_event_name": "Stop"}) == ("question_clear", None)
    assert question_marker._classify(
        {"hook_event_name": "Notification", "notification_type": "elicitation_complete"}
    ) == ("question_clear", None)


@pytest.mark.parametrize(
    "ntype", ["idle_prompt", "auth_success", "agent_needs_input", ""]
)
def test_classify_excluded_notification_types_dropped(ntype):
    """Fork B pick (iii): idle_prompt/auth_success etc. are NOT open edges."""
    assert (
        question_marker._classify(
            {"hook_event_name": "Notification", "notification_type": ntype}
        )
        is None
    )


def test_classify_wrong_tool_pretooluse_dropped():
    assert (
        question_marker._classify({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
        is None
    )
