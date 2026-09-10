"""F826 (#683) — the Claude statusLine emitter (hooks/status_emit.py).

Covers D3 / NIT-1 / SHOULD-5 / AC5: atomic write-then-print, a CONSTANT line
for a constant selection, an unconditional print (a failed sidecar write must
not blank the pane), CAO_TERMINAL_ID from env, and stdlib-only imports.

F894 (#746) adds the chain to the user's own ``~/.claude/settings.json``
``statusLine.command``: its output replaces the constant line when it succeeds,
and every failure mode (absent / unreadable / non-zero / timeout / empty /
recursive) falls back to the constant line. Every test pins ``HOME`` to a tmp
dir so the developer's real statusline is never executed.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.hooks import status_emit


def _write_user_settings(home: Path, statusline: object) -> None:
    """Seed ``<home>/.claude/settings.json`` with a ``statusLine`` entry."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": statusline}))


def _run(monkeypatch, capsys, event, *, home, terminal_id="t1"):
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    # Isolate the user statusline chain (F894): HOME points at the tmp dir, so
    # no test ever reads or runs the developer's real ~/.claude/statusline.sh.
    monkeypatch.setenv("HOME", str(home))
    if terminal_id is not None:
        monkeypatch.setenv("CAO_TERMINAL_ID", terminal_id)
    else:
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    rc = status_emit.main()
    out = capsys.readouterr().out
    return rc, out


def test_writes_sidecar_and_prints_constant_line(monkeypatch, capsys, tmp_path):
    event = {
        "model": {"id": "claude-opus", "display_name": "Opus 4.6"},
        "effort": {"level": "high"},
        "session_id": "sess-1",
    }
    rc, out = _run(monkeypatch, capsys, event, home=tmp_path)
    assert rc == 0
    # The printed line is `<display_name> · <effort>` and CONSTANT (no clock).
    assert out.strip() == "Opus 4.6 \u00b7 high"
    sidecar = tmp_path / "observe" / "t1.json"
    assert sidecar.exists()
    record = json.loads(sidecar.read_text())
    assert record["model"] == "Opus 4.6"
    assert record["effort"] == "high"
    assert record["claude_session_id"] == "sess-1"
    assert isinstance(record["event_time"], int)


def test_line_is_identical_across_two_runs_for_same_selection(monkeypatch, capsys, tmp_path):
    """SHOULD-5: byte-identical output for a constant selection (stability check)."""
    event = {"model": {"display_name": "Opus"}, "effort": {"level": "medium"}, "session_id": "s"}
    _, out1 = _run(monkeypatch, capsys, event, home=tmp_path)
    _, out2 = _run(monkeypatch, capsys, event, home=tmp_path)
    assert out1 == out2


def test_effort_absent_renders_dash(monkeypatch, capsys, tmp_path):
    """N2: a non-thinking model has no effort.level -> `<model> · -`."""
    event = {"model": {"display_name": "haiku"}, "session_id": "s"}
    _, out = _run(monkeypatch, capsys, event, home=tmp_path)
    assert out.strip() == "haiku \u00b7 -"


def test_prints_even_when_sidecar_write_fails(monkeypatch, capsys, tmp_path):
    """NIT-1: a failed sidecar write degrades the marker, never blanks the pane."""
    event = {"model": {"display_name": "Opus"}, "effort": {"level": "high"}, "session_id": "s"}
    monkeypatch.setattr(status_emit, "_write_sidecar", lambda *a, **k: False)
    _, out = _run(monkeypatch, capsys, event, home=tmp_path)
    assert out.strip() == "Opus \u00b7 high"


def test_missing_terminal_id_still_prints_no_sidecar(monkeypatch, capsys, tmp_path):
    event = {"model": {"display_name": "Opus"}, "effort": {"level": "high"}, "session_id": "s"}
    _, out = _run(monkeypatch, capsys, event, home=tmp_path, terminal_id=None)
    assert out.strip() == "Opus \u00b7 high"
    assert not (tmp_path / "observe" / "t1.json").exists()


def test_empty_stdin_prints_unknowns(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_TERMINAL_ID", "t1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = status_emit.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "- \u00b7 -"


def test_atomic_write_leaves_no_tmp_file(monkeypatch, capsys, tmp_path):
    event = {"model": {"display_name": "Opus"}, "effort": {"level": "high"}, "session_id": "s"}
    _run(monkeypatch, capsys, event, home=tmp_path)
    observe = tmp_path / "observe"
    tmps = list(observe.glob(".*.tmp"))
    assert tmps == []


def test_emitter_imports_are_stdlib_only():
    """AC5: the emitter imports nothing beyond stdlib + json (50 ms budget).

    Run the module in a subprocess with the package src on the path and assert
    no cli_agent_orchestrator submodule (beyond the trivial package __init__ the
    module path forces) was imported.
    """
    src = str(Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys, json;"
        "import cli_agent_orchestrator.hooks.status_emit as m;"
        "mods=[k for k in sys.modules if k.startswith('cli_agent_orchestrator') "
        "and k not in ('cli_agent_orchestrator','cli_agent_orchestrator.hooks',"
        "'cli_agent_orchestrator.hooks.status_emit')];"
        "print(json.dumps(mods))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    leaked = json.loads(proc.stdout.strip())
    assert leaked == [], f"emitter dragged in heavy modules: {leaked}"


# --- F894 (#746): chain the user's own statusLine.command ---------------------

_EVENT = {
    "model": {"id": "claude-opus", "display_name": "Opus 4.6"},
    "effort": {"level": "high"},
    "session_id": "sess-1",
}


def _script(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable shell script under tmp_path and return its path."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_user_statusline_output_is_printed_and_sidecar_still_written(monkeypatch, capsys, tmp_path):
    """F894: the user's command wins the pane, the observe sidecar still lands."""
    script = _script(tmp_path, "user_bar.sh", 'printf "%s\\n" "dir | Opus | ctx:12%"\n')
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    rc, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert rc == 0
    assert out == "dir | Opus | ctx:12%\n"
    record = json.loads((tmp_path / "observe" / "t1.json").read_text())
    assert record["model"] == "Opus 4.6"
    assert record["effort"] == "high"


def test_user_statusline_receives_the_same_stdin_json(monkeypatch, capsys, tmp_path):
    """The chained command is piped the identical statusline JSON."""
    sink = tmp_path / "stdin.json"
    script = _script(tmp_path, "echo_stdin.sh", f'cat > "{sink}"\necho piped\n')
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "piped"
    assert json.loads(sink.read_text()) == _EVENT


def test_user_statusline_multiline_output_is_printed_in_full(monkeypatch, capsys, tmp_path):
    script = _script(tmp_path, "two_lines.sh", 'printf "one\\ntwo\\n"\n')
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out == "one\ntwo\n"


def test_no_user_settings_file_prints_constant_line(monkeypatch, capsys, tmp_path):
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_unreadable_user_settings_prints_constant_line(monkeypatch, capsys, tmp_path):
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text("{not json at all")
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_non_command_statusline_type_prints_constant_line(monkeypatch, capsys, tmp_path):
    _write_user_settings(tmp_path, {"type": "static", "command": "/bin/echo nope"})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_user_statusline_non_zero_exit_prints_constant_line(monkeypatch, capsys, tmp_path):
    script = _script(tmp_path, "fail.sh", "echo junk\nexit 3\n")
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_user_statusline_empty_output_prints_constant_line(monkeypatch, capsys, tmp_path):
    script = _script(tmp_path, "silent.sh", 'printf "  \\n"\n')
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_user_statusline_stderr_never_reaches_stdout(monkeypatch, capsys, tmp_path):
    script = _script(tmp_path, "noisy.sh", "echo boom >&2\necho good\n")
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out == "good\n"


def test_user_statusline_timeout_prints_constant_line(monkeypatch, capsys, tmp_path):
    """A slow user script is capped; the pane falls back to the constant line."""
    monkeypatch.setattr(status_emit, "_USER_TIMEOUT_S", 0.2)
    script = _script(tmp_path, "slow.sh", "sleep 5\necho late\n")
    _write_user_settings(tmp_path, {"type": "command", "command": script})
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_recursion_guard_ignores_self_referencing_command(monkeypatch, capsys, tmp_path):
    """A user command naming this module is treated as no command at all."""
    _write_user_settings(
        tmp_path,
        {
            "type": "command",
            "command": "python -m cli_agent_orchestrator.hooks.status_emit",
        },
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    assert status_emit._user_statusline_command() is None
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"


def test_missing_command_binary_prints_constant_line(monkeypatch, capsys, tmp_path):
    _write_user_settings(
        tmp_path, {"type": "command", "command": str(tmp_path / "does-not-exist.sh")}
    )
    _, out = _run(monkeypatch, capsys, _EVENT, home=tmp_path)
    assert out.strip() == "Opus 4.6 \u00b7 high"
