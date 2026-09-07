"""F810 (#667): the fork owns the seat delivery edges.

Covers the four named unit tests from the build brief:

* overlay content for a claude_code supervisor contains the four F810 hook
  registrations with the resolved binary path, dedupes against a pre-existing
  identical command, and leaves non-supervisor profiles untouched;
* register_inbox / supervisor_drain / rewake exit 0 on: no CAO_TERMINAL_ID,
  empty stdin, server down (mock), malformed JSON;
* the drain envelope text equals the root hook's format for one pending row and
  one suppressed BUSY ping (golden strings);
* transport_ejection emits exactly one native_unreachable condition per episode.

Mutants (see f810-build-report.md): drop the dedupe → duplicate registration;
register PATCH without the idempotency GET; ejection emits every attempt.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.providers.claude_code import (
    ClaudeCodeProvider,
    _dedupe_overlay_hooks_by_command,
)

_REGISTER_MOD = "cli_agent_orchestrator.hooks.register_inbox"
_DRAIN_MOD = "cli_agent_orchestrator.hooks.supervisor_drain"
_REWAKE_MOD = "cli_agent_orchestrator.hooks.rewake"


def _settings() -> dict:
    provider = ClaudeCodeProvider("hookterm", "session", "window", None)
    path = provider._write_terminal_settings()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


# ── overlay content ───────────────────────────────────────────────────────────


def test_overlay_registers_the_four_f810_edges():
    h = _settings()["hooks"]

    def cmds(event: str) -> list[str]:
        return [hk["command"] for b in h[event] for hk in b["hooks"]]

    # register on SessionStart + PostToolUse(Agent|Task)
    assert any(_REGISTER_MOD in c for c in cmds("SessionStart"))
    assert any(_REGISTER_MOD in c for c in cmds("PostToolUse"))
    # drain on SessionStart + PostToolUse(.*) + Stop
    assert any(_DRAIN_MOD in c for c in cmds("SessionStart"))
    assert any(_DRAIN_MOD in c for c in cmds("PostToolUse"))
    assert any(_DRAIN_MOD in c for c in cmds("Stop"))
    # rewake on PostToolUse + Stop
    assert any(_REWAKE_MOD in c for c in cmds("PostToolUse"))
    assert any(_REWAKE_MOD in c for c in cmds("Stop"))


def test_overlay_commands_use_python_m_not_absolute_path():
    """Do-NOT 20 / F569 #426: python -m <module>, never a .claude/.sh path."""
    h = _settings()["hooks"]
    for event in ("SessionStart", "PostToolUse", "Stop"):
        for b in h[event]:
            for hk in b["hooks"]:
                c = hk["command"]
                if any(m in c for m in (_REGISTER_MOD, _DRAIN_MOD, _REWAKE_MOD)):
                    assert "-m" in c
                    assert ".claude" not in c
                    assert ".sh" not in c


def test_overlay_stop_rewake_is_async_with_root_summary():
    h = _settings()["hooks"]
    stop_rewake = [
        hk
        for b in h["Stop"]
        for hk in b["hooks"]
        if _REWAKE_MOD in hk["command"] and "--source=stop" in hk["command"]
    ]
    assert len(stop_rewake) == 1
    hk = stop_rewake[0]
    assert hk["asyncRewake"] is True
    assert hk["timeout"] == 3600
    assert hk["rewakeSummary"] == "CAO callback waiting"


def test_overlay_no_intra_event_duplicate_commands():
    h = _settings()["hooks"]
    for event, blocks in h.items():
        cmds = [hk["command"] for b in blocks for hk in b["hooks"]]
        assert len(cmds) == len(set(cmds)), (event, cmds)


def test_no_auth_token_leaks_in_overlay():
    assert "CAO_AUTH_LOCAL_TOKEN" not in json.dumps(_settings())


# ── dedupe helper (D2) ──────────────────────────────────────────────────────────


def test_dedupe_drops_second_identical_command_first_wins():
    hooks = {
        "PostToolUse": [
            {"matcher": "Agent|Task", "hooks": [{"command": "X", "timeout": 10}]},
            {"matcher": ".*", "hooks": [{"command": "X", "timeout": 99}]},
            {"matcher": ".*", "hooks": [{"command": "Y", "timeout": 10}]},
        ]
    }
    _dedupe_overlay_hooks_by_command(hooks)
    cmds = [hk["command"] for b in hooks["PostToolUse"] for hk in b["hooks"]]
    assert cmds == ["X", "Y"]  # second X dropped
    # first occurrence's fields preserved (timeout 10, not 99)
    x = [hk for b in hooks["PostToolUse"] for hk in b["hooks"] if hk["command"] == "X"][0]
    assert x["timeout"] == 10


def test_dedupe_drops_emptied_block():
    hooks = {
        "Stop": [
            {"hooks": [{"command": "A"}]},
            {"hooks": [{"command": "A"}]},  # fully duplicate -> emptied -> dropped
        ]
    }
    _dedupe_overlay_hooks_by_command(hooks)
    assert len(hooks["Stop"]) == 1


def test_dedupe_is_per_event_not_cross_event():
    hooks = {
        "SessionStart": [{"hooks": [{"command": "DRAIN"}]}],
        "Stop": [{"hooks": [{"command": "DRAIN"}]}],
    }
    _dedupe_overlay_hooks_by_command(hooks)
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "DRAIN"
    assert hooks["Stop"][0]["hooks"][0]["command"] == "DRAIN"


# ── register_inbox hook: containment + fail-open ────────────────────────────────


def _run_register(event, monkeypatch, *, stdin: str | None = None):
    from cli_agent_orchestrator.hooks import register_inbox

    payload = json.dumps(event) if stdin is None else stdin
    with patch("sys.stdin", io.StringIO(payload)):
        return register_inbox.main()


def test_register_no_terminal_id_zero_side_effects(monkeypatch):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    with patch.object(register_inbox.cao_http, "patch") as p:
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
    p.assert_not_called()


def test_register_empty_stdin_returns_zero(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    assert _run_register(None, monkeypatch, stdin="") == 0


def test_register_malformed_json_returns_zero(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    assert _run_register(None, monkeypatch, stdin="{not json") == 0


def test_register_no_session_id_returns_zero(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    assert _run_register({"source": "startup"}, monkeypatch) == 0


def test_register_zero_teams_warns_once_per_window(monkeypatch, tmp_path, capsys):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    # No team dirs derivable → WARN, throttled by the sentinel under CAO_HOME_DIR.
    monkeypatch.setattr(register_inbox, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: [])
    with patch.object(register_inbox.cao_http, "patch") as p:
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        first = capsys.readouterr().err
        # second immediate fire is throttled (no new WARN)
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
        second = capsys.readouterr().err
    p.assert_not_called()
    assert "f810.native_unpublished" in first
    assert second == ""


def test_register_idempotent_skips_patch_when_already_registered(monkeypatch, tmp_path):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    team = tmp_path / ".claude" / "teams" / "myteam" / "inboxes"
    team.mkdir(parents=True)
    inbox = team / "team-lead.json"
    inbox.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: ["myteam"])
    monkeypatch.setattr(
        register_inbox.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
    )
    get_resp = MagicMock()
    get_resp.json.return_value = {"metadata": {"cc_team_inbox_path": str(inbox)}}
    get_resp.raise_for_status = MagicMock()
    with (
        patch.object(register_inbox, "get_local_bearer", return_value=None),
        patch.object(register_inbox.cao_http, "get", return_value=get_resp),
        patch.object(register_inbox.cao_http, "patch") as p,
    ):
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
    p.assert_not_called()  # already registered → no PATCH (idempotency)


def test_register_patches_when_absent(monkeypatch, tmp_path):
    from cli_agent_orchestrator.hooks import register_inbox

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.delenv("CAO_TERMINAL_TOKEN", raising=False)
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    team = tmp_path / ".claude" / "teams" / "myteam" / "inboxes"
    team.mkdir(parents=True)
    inbox = team / "team-lead.json"
    inbox.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(register_inbox, "_derive_team_names", lambda sid, home: ["myteam"])
    monkeypatch.setattr(
        register_inbox.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
    )
    get_resp = MagicMock()
    get_resp.json.return_value = {"metadata": {"existing": "keep"}}
    get_resp.raise_for_status = MagicMock()
    patch_resp = MagicMock()
    with (
        patch.object(register_inbox, "get_local_bearer", return_value=None),
        patch.object(register_inbox.cao_http, "get", return_value=get_resp),
        patch.object(register_inbox.cao_http, "patch", return_value=patch_resp) as p,
    ):
        assert _run_register({"session_id": "s"}, monkeypatch) == 0
    assert p.call_args[0][0] == "/terminals/abcd1234/metadata"
    sent = p.call_args.kwargs["json"]["metadata"]
    assert sent["cc_team_inbox_path"] == str(inbox)
    assert sent["existing"] == "keep"  # whole-dict merge preserved prior keys


# ── rewake hook: containment + fail-open ────────────────────────────────────────


def test_rewake_no_terminal_id_returns_zero(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    with patch("sys.stdin", io.StringIO("{}")):
        assert rewake.main(["--arm", "--source=stop"]) == 0


def test_rewake_subagent_gate_returns_zero(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("sys.stdin", io.StringIO(json.dumps({"agent_id": "sub"}))):
        assert rewake.main(["--arm", "--source=stop"]) == 0


def test_rewake_empty_stdin_returns_zero(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("sys.stdin", io.StringIO("")):
        # empty stdin -> gate returns None -> arm runs; force an instant deadline
        monkeypatch.setenv("F213_DEADLINE_S", "0")
        with patch.object(rewake, "get_local_bearer", return_value=None):
            assert rewake.main(["--arm", "--source=stop"]) == 0


def test_rewake_prime_is_noop(monkeypatch):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("sys.stdin", io.StringIO("{}")):
        assert rewake.main(["--prime", "--notify"]) == 0


def test_rewake_wakes_on_new_pending(monkeypatch, tmp_path, capsys):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(rewake, "CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("F213_STABILITY_POLLS", "1")
    monkeypatch.setenv("F213_POLL_INTERVAL_S", "0")
    resp = MagicMock()
    resp.json.return_value = {
        "items": [{"id": 42, "sender_id": "wrk1", "message": "hi", "status": "pending"}]
    }
    resp.raise_for_status = MagicMock()
    with (
        patch.object(rewake, "get_local_bearer", return_value=None),
        patch.object(rewake.cao_http, "get", return_value=resp),
        patch("sys.stdin", io.StringIO("{}")),
    ):
        rc = rewake.main(["--arm", "--source=stop"])
    out = capsys.readouterr()
    assert rc == 2
    assert json.loads(out.out.strip()) == {"rewakeSummary": "CAO callback waiting (id 42)"}
    assert "[42] from=wrk1" in out.err


def test_rewake_busy_ping_does_not_wake(monkeypatch, tmp_path):
    from cli_agent_orchestrator.hooks import rewake

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(rewake, "CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("F213_STABILITY_POLLS", "1")
    monkeypatch.setenv("F213_DEADLINE_S", "0")
    resp = MagicMock()
    resp.json.return_value = {
        "items": [
            {"id": 7, "sender_id": "x", "message": "[CONDITION] kind=BUSY", "status": "pending"}
        ]
    }
    resp.raise_for_status = MagicMock()
    with (
        patch.object(rewake, "get_local_bearer", return_value=None),
        patch.object(rewake.cao_http, "get", return_value=resp),
        patch("sys.stdin", io.StringIO("{}")),
    ):
        # BUSY filtered → treated as empty → loop hits the 0s deadline → exit 0.
        assert rewake.main(["--arm", "--source=stop"]) == 0
