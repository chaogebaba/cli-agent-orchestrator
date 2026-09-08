"""F826 (#683) — the Claude statusLine emitter (hooks/status_emit.py).

Covers D3 / NIT-1 / SHOULD-5 / AC5: atomic write-then-print, a CONSTANT line
for a constant selection, an unconditional print (a failed sidecar write must
not blank the pane), CAO_TERMINAL_ID from env, and stdlib-only imports.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.hooks import status_emit


def _run(monkeypatch, capsys, event, *, home, terminal_id="t1"):
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
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
