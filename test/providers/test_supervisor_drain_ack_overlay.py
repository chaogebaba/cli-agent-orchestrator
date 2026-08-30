"""F543 D22 overlay: the supervisor drain/ack hooks are composed into the
claude_code settings overlay (SessionStart drain, Stop ack) exactly like the
four existing hooks — no ~/.claude, no absolute path (Do-NOT 20)."""

import io
import json
from unittest.mock import patch

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

_DRAIN_MOD = "cli_agent_orchestrator.hooks.supervisor_drain"
_ACK_MOD = "cli_agent_orchestrator.hooks.supervisor_ack"


def _settings() -> dict:
    provider = ClaudeCodeProvider("hookterm", "session", "window", None)
    path = provider._write_terminal_settings()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


def test_drain_hook_composed_on_sessionstart():
    hooks = _settings()["hooks"]
    commands = [h["command"] for block in hooks["SessionStart"] for h in block["hooks"]]
    assert any(_DRAIN_MOD in c for c in commands)


def test_ack_hook_composed_on_stop():
    hooks = _settings()["hooks"]
    commands = [h["command"] for block in hooks["Stop"] for h in block["hooks"]]
    assert any(_ACK_MOD in c for c in commands)


def test_drain_hook_is_python_module_not_absolute_path():
    """Do-NOT 20 / F569 #426: composed as `python -m <module>`, never a
    hardcoded install path or a ~/.claude / .claude/hooks reference. (The python
    interpreter path from sys.executable is legitimate; the hazard is a
    hardcoded hook-SCRIPT path.)"""
    hooks = _settings()["hooks"]
    for block in hooks["SessionStart"]:
        for h in block["hooks"]:
            if _DRAIN_MOD in h["command"]:
                assert "-m" in h["command"]
                assert ".claude" not in h["command"]


def test_drain_ack_hooks_have_timeout_5():
    hooks = _settings()["hooks"]
    for event in ("SessionStart", "Stop"):
        for block in hooks[event]:
            for h in block["hooks"]:
                if _DRAIN_MOD in h["command"] or _ACK_MOD in h["command"]:
                    assert h["timeout"] == 5


def test_no_auth_token_leaks():
    assert "CAO_AUTH_LOCAL_TOKEN" not in json.dumps(_settings())


# ---- hook containment + fail-open ----------------------------------------


def _run_drain(event: dict) -> int:
    from cli_agent_orchestrator.hooks import supervisor_drain

    with patch("sys.stdin", io.StringIO(json.dumps(event))):
        return supervisor_drain.main()


def test_drain_hook_no_terminal_id_zero_side_effects(monkeypatch):
    from cli_agent_orchestrator.hooks import supervisor_drain

    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    with patch.object(supervisor_drain.cao_http, "post") as post:
        assert _run_drain({"hook_event_name": "SessionStart"}) == 0
    post.assert_not_called()


def test_drain_hook_fail_open_on_unreachable(monkeypatch):
    from cli_agent_orchestrator.hooks import supervisor_drain

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    with (
        patch.object(supervisor_drain.cao_http, "post", side_effect=OSError("refused")),
        patch.object(supervisor_drain, "get_local_bearer", return_value=None),
    ):
        assert _run_drain({"hook_event_name": "SessionStart"}) == 0


def test_drain_hook_posts_to_drain_endpoint(monkeypatch):
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.hooks import supervisor_drain

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    with (
        patch.object(supervisor_drain, "get_local_bearer", return_value=None),
        patch.object(supervisor_drain.cao_http, "post", return_value=MagicMock()) as post,
    ):
        assert _run_drain({"hook_event_name": "SessionStart"}) == 0
    assert post.call_args[0][0] == "/terminals/abcd1234/inbox/drain"
