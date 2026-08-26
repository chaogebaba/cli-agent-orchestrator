"""F507 overlay additivity (AC12): new interaction-marker hook blocks are
added without disturbing the existing SessionStart transcript-binding block."""

import json

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


def _settings() -> dict:
    provider = ClaudeCodeProvider("hookterm", "session", "window", None)
    path = provider._write_terminal_settings()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


def test_ac12_session_start_block_unchanged_and_transcript_hook_present():
    settings = _settings()
    session_start = settings["hooks"]["SessionStart"][0]
    assert session_start["matcher"] == "startup|resume|clear|compact"
    assert (
        "cli_agent_orchestrator.hooks.transcript_binding"
        in session_start["hooks"][0]["command"]
    )


def test_open_edge_blocks_present():
    settings = settings = _settings()
    hooks = settings["hooks"]
    notif = hooks["Notification"][0]
    assert "permission_prompt" in notif["matcher"]
    assert "elicitation_dialog" in notif["matcher"]
    assert "elicitation_url_dialog" in notif["matcher"]
    assert "cli_agent_orchestrator.hooks.question_marker" in notif["hooks"][0]["command"]

    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "AskUserQuestion"
    assert "cli_agent_orchestrator.hooks.question_marker" in pre["hooks"][0]["command"]


def test_clear_edge_blocks_present():
    hooks = _settings()["hooks"]
    assert hooks["PostToolUse"][0]["matcher"] == "AskUserQuestion"
    assert hooks["PostToolUseFailure"][0]["matcher"] == "AskUserQuestion"
    # Stop takes no matcher (D7).
    stop = hooks["Stop"][0]
    assert "matcher" not in stop
    assert "cli_agent_orchestrator.hooks.question_marker" in stop["hooks"][0]["command"]
    # elicitation clear notification types are routed to the module too.
    assert "elicitation_complete" in hooks["Notification"][0]["matcher"]
    assert "elicitation_response" in hooks["Notification"][0]["matcher"]


def test_no_auth_token_leaks_into_settings():
    assert "CAO_AUTH_LOCAL_TOKEN" not in json.dumps(_settings())


def test_all_marker_hooks_have_timeout_5():
    hooks = _settings()["hooks"]
    for event in ("Notification", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"):
        for block in hooks[event]:
            for hook in block["hooks"]:
                assert hook["timeout"] == 5
