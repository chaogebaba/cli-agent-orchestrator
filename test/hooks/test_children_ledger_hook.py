"""F568 D12a children-ledger hook: classify, containment, fail-open."""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.hooks import children_ledger
from cli_agent_orchestrator.hooks.children_ledger import main


def _run(event: dict) -> int:
    with patch("sys.stdin", io.StringIO(json.dumps(event))):
        return main()


# ---- classify -------------------------------------------------------------


def test_classify_register_on_agent_and_task():
    assert children_ledger._classify(
        {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_call_id": "c1"}
    ) == ("register", "c1")
    assert children_ledger._classify(
        {"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_call_id": "c2"}
    ) == ("register", "c2")


def test_classify_register_synthesizes_id_when_absent():
    op, child_id = children_ledger._classify(
        {"hook_event_name": "PreToolUse", "tool_name": "Agent"}
    )
    assert op == "register"
    assert isinstance(child_id, str) and child_id


def test_classify_release_on_subagent_stop_with_id():
    assert children_ledger._classify({"hook_event_name": "SubagentStop", "agent_id": "a1"}) == (
        "release",
        "a1",
    )


def test_classify_release_on_subagent_stop_without_id():
    assert children_ledger._classify({"hook_event_name": "SubagentStop"}) == ("release", None)


def test_classify_drops_non_dispatch_pretooluse():
    assert children_ledger._classify({"hook_event_name": "PreToolUse", "tool_name": "Bash"}) is None


def test_classify_drops_unrelated_events():
    # A seat-turn Stop is NOT a child release (that is the F507 marker's edge).
    assert children_ledger._classify({"hook_event_name": "Stop"}) is None
    assert (
        children_ledger._classify({"hook_event_name": "PostToolUse", "tool_name": "Agent"}) is None
    )


# ---- containment + fail-open ---------------------------------------------


def test_containment_no_terminal_id_zero_side_effects(monkeypatch):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_call_id": "c1"}
    with (
        patch.object(children_ledger.cao_http, "post") as post,
        patch.object(children_ledger, "_deadletter") as deadletter,
    ):
        assert _run(event) == 0
    post.assert_not_called()
    deadletter.assert_not_called()


def test_fail_open_unreachable_server(monkeypatch, tmp_path):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(children_ledger, "CAO_HOME_DIR", str(tmp_path))
    event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_call_id": "c1"}
    with (
        patch.object(children_ledger.cao_http, "post", side_effect=OSError("refused")),
        patch.object(children_ledger, "get_local_bearer", return_value=None),
    ):
        assert _run(event) == 0
    deadletter = Path(tmp_path) / "hook-deadletter.jsonl"
    assert deadletter.exists()
    record = json.loads(deadletter.read_text(encoding="utf-8").splitlines()[0])
    assert record["terminal_id"] == "abcd1234"


def test_register_posts_expected_payload(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_call_id": "c1"}
    response = MagicMock()
    with (
        patch.object(children_ledger, "get_local_bearer", return_value=None),
        patch.object(children_ledger.cao_http, "post", return_value=response) as post,
    ):
        assert _run(event) == 0
    assert post.call_count == 1
    _args, kwargs = post.call_args
    assert post.call_args[0][0] == "/terminals/abcd1234/children-ledger"
    payload = kwargs["json"]
    assert payload["op"] == "register"
    assert payload["child_id"] == "c1"
    assert payload["terminal_id"] == "abcd1234"


def test_release_posts_without_child_id(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    event = {"hook_event_name": "SubagentStop"}
    response = MagicMock()
    with (
        patch.object(children_ledger, "get_local_bearer", return_value=None),
        patch.object(children_ledger.cao_http, "post", return_value=response) as post,
    ):
        assert _run(event) == 0
    payload = post.call_args[1]["json"]
    assert payload["op"] == "release"
    assert "child_id" not in payload


def test_dropped_event_no_post(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
    with patch.object(children_ledger.cao_http, "post") as post:
        assert _run(event) == 0
    post.assert_not_called()
