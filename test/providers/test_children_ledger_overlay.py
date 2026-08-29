"""F568 D12a overlay: the children-ledger hook blocks are added to the
claude_code settings overlay without disturbing the F507 marker blocks."""

import json

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

_LEDGER_MOD = "cli_agent_orchestrator.hooks.children_ledger"
_MARKER_MOD = "cli_agent_orchestrator.hooks.question_marker"


def _settings() -> dict:
    provider = ClaudeCodeProvider("hookterm", "session", "window", None)
    path = provider._write_terminal_settings()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


def test_register_block_pretooluse_agent_task():
    hooks = _settings()["hooks"]
    ledger_blocks = [b for b in hooks["PreToolUse"] if _LEDGER_MOD in b["hooks"][0]["command"]]
    assert len(ledger_blocks) == 1
    assert ledger_blocks[0]["matcher"] == "Agent|Task"


def test_release_block_subagent_stop_no_matcher():
    hooks = _settings()["hooks"]
    assert "SubagentStop" in hooks
    stop = hooks["SubagentStop"][0]
    assert "matcher" not in stop
    assert _LEDGER_MOD in stop["hooks"][0]["command"]


def test_f507_marker_pretooluse_block_untouched():
    """The existing AskUserQuestion marker block is still PreToolUse[0]."""
    hooks = _settings()["hooks"]
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "AskUserQuestion"
    assert _MARKER_MOD in pre["hooks"][0]["command"]
    # The two PreToolUse blocks are routed to DIFFERENT modules.
    assert _LEDGER_MOD not in pre["hooks"][0]["command"]


def test_ledger_hooks_have_timeout_5():
    hooks = _settings()["hooks"]
    for event in ("PreToolUse", "SubagentStop"):
        for block in hooks[event]:
            for hook in block["hooks"]:
                assert hook["timeout"] == 5


def test_no_auth_token_leaks():
    assert "CAO_AUTH_LOCAL_TOKEN" not in json.dumps(_settings())
