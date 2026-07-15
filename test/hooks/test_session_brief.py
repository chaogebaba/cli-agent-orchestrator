import io, json
from unittest.mock import Mock, patch

from cli_agent_orchestrator.hooks.session_brief import MARKER, main


def _response(payload):
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_startup_noop(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "startup"})))
    monkeypatch.setenv("CAO_TERMINAL_ID", "term0001")
    monkeypatch.setenv("CAO_SESSION_BRIEF_MODE", "required")
    assert main() == 0
    assert capsys.readouterr().out == ""


def test_required_failure_is_loud(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "resume"})))
    monkeypatch.setenv("CAO_SESSION_BRIEF_MODE", "required")
    with patch("cli_agent_orchestrator.hooks.session_brief.requests.get", side_effect=RuntimeError("down")):
        assert main() == 0
    assert json.loads(capsys.readouterr().out)["additionalContext"] == MARKER


def test_compact_is_thin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "compact"})))
    monkeypatch.setenv("CAO_TERMINAL_ID", "term0001")
    manifest = {
        "generated_at": "now",
        "complete": False,
        "profiles": [{"name": "a"}],
        "sections": {"profiles": "ok", "tools": "not_collected", "activation": "error"},
        "errors": [{"section": "activation", "code": "RuntimeError", "message": "broken"}],
    }
    with patch("cli_agent_orchestrator.hooks.session_brief.requests.get", side_effect=[_response({"session_name": "cao-test"}), _response(manifest)]):
        assert main() == 0
    text = json.loads(capsys.readouterr().out)["additionalContext"]
    assert "run `cao session manifest --brief` for full" in text
    assert "tools=not_collected" in text
    assert "activation=error (RuntimeError)" in text
    assert "### Ready bases" not in text


def test_resume_and_clear_emit_exact_full_context(monkeypatch, capsys):
    manifest = {
        "generated_at": "now", "complete": True,
        "session": {"name": "cao-test"},
        "profiles": [{"name": "a", "role": "supervisor", "provider": "claude_code", "skills": [], "charter_digest": "digest"}],
        "ready_bases": [], "skills": [], "workflows": [], "terminals": [],
        "activation": {"cli_path": "current", "differing_files": 0, "server": "current", "source_root": "/repo"},
        "errors": [],
    }
    expected = {"additionalContext": __import__(
        "cli_agent_orchestrator.services.session_manifest_service", fromlist=["render_session_brief"]
    ).render_session_brief(manifest)}
    for source in ("resume", "clear"):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": source})))
        monkeypatch.setenv("CAO_TERMINAL_ID", "term0001")
        with patch(
            "cli_agent_orchestrator.hooks.session_brief.requests.get",
            side_effect=[_response({"session_name": "cao-test"}), _response(manifest)],
        ):
            assert main() == 0
        assert json.loads(capsys.readouterr().out) == expected
