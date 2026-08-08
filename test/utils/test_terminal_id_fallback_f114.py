"""HOTFIX F114: terminal-id file write + recovery when CAO_TERMINAL_ID is unset."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils import terminal_id_fallback as f114


@pytest.fixture()
def f114_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_TMP_DIR", str(tmp_path))
    # Ensure recovery does not see a pre-set tid from the host pane.
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return tmp_path


def test_write_and_lookup_by_pane(f114_tmp):
    f114.write_terminal_id_fallback(terminal_id="abcd1234", pane_id="%42")
    path = f114.f114_tid_dir() / "pane-42"
    assert path.read_text(encoding="utf-8").strip() == "abcd1234"
    assert f114._lookup_file_by_pane("%42") == "abcd1234"


def test_write_and_lookup_by_window(f114_tmp):
    f114.write_terminal_id_fallback(
        terminal_id="deadbeef",
        session_name="cao-sess",
        window_name="kiro_dev-ab12",
    )
    assert (
        f114._lookup_file_by_window("cao-sess", "kiro_dev-ab12") == "deadbeef"
    )


def test_recover_from_tmux_pane_file(f114_tmp, monkeypatch):
    f114.write_terminal_id_fallback(terminal_id="cafebabe", pane_id="%7")
    monkeypatch.setenv("TMUX_PANE", "%7")
    assert f114.recover_and_apply_terminal_identity() == "cafebabe"
    assert os.environ["CAO_TERMINAL_ID"] == "cafebabe"


def test_recover_from_ancestor_environ(f114_tmp, monkeypatch):
    # Simulate: self has no tid; parent pid chain yields one with CAO_TERMINAL_ID.
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)

    def fake_ppid(pid):
        return {os.getpid(): 1111, 1111: 1}.get(pid)

    def fake_environ(pid):
        if pid == 1111:
            return {
                "CAO_TERMINAL_ID": "11223344",
                "CAO_INSTANCE_ID": "sandbox-x",
                "CAO_ENDPOINT": "http://127.0.0.1:9890",
            }
        return {}

    with (
        patch.object(f114, "_read_ppid", side_effect=fake_ppid),
        patch.object(f114, "_read_proc_environ", side_effect=fake_environ),
    ):
        tid = f114.recover_and_apply_terminal_identity()
    assert tid == "11223344"
    assert os.environ["CAO_TERMINAL_ID"] == "11223344"
    assert os.environ["CAO_INSTANCE_ID"] == "sandbox-x"
    assert os.environ["CAO_ENDPOINT"] == "http://127.0.0.1:9890"


def test_recover_noop_when_env_valid(f114_tmp, monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "99887766")
    assert f114.recover_and_apply_terminal_identity() == "99887766"


def test_recover_returns_none_when_nothing_found(f114_tmp, monkeypatch):
    with (
        patch.object(f114, "_read_ppid", return_value=None),
        patch.object(f114, "_read_proc_environ", return_value={}),
    ):
        assert f114.recover_and_apply_terminal_identity() is None
    assert "CAO_TERMINAL_ID" not in os.environ or not os.environ.get("CAO_TERMINAL_ID")
