"""Unit tests for clients/tmux_fast.PaneLocator (one-exec tmux lookups).

Drive the locator with a scripted fake runner so no tmux server is needed and
every exec is counted. The contract under test: one exec per cached command,
identity-checked; ``None`` (never an exception, never an absence claim) on
anything the fast path cannot answer.
"""

from __future__ import annotations

import subprocess
from typing import Dict, List, Optional, Tuple

import pytest

from cli_agent_orchestrator.clients import tmux_fast
from cli_agent_orchestrator.clients.tmux_fast import PaneLocator

SEP = "\x1f"


def _pane_row(
    session: str, wid: str, widx: int, wname: str, pidx: int, pid: str, active: bool
) -> str:
    return SEP.join([session, wid, str(widx), wname, str(pidx), pid, "1" if active else "0"])


class FakeTmux:
    """Scripted tmux: an inventory plus per-pane identity and command output."""

    def __init__(self) -> None:
        self.inventory: List[str] = []
        # pane_id -> (session, window, active)
        self.panes: Dict[str, Tuple[str, str, bool]] = {}
        # pane_id -> stdout for the real command
        self.output: Dict[str, str] = {}
        self.calls: List[List[str]] = []
        self.fail_inventory = False
        self.command_rc: Dict[str, int] = {}
        # pane_id -> pane_id reported in the identity line (defense-in-depth probe)
        self.ident_pane_override: Dict[str, str] = {}

    def add_pane(
        self,
        session: str,
        wid: str,
        widx: int,
        wname: str,
        pidx: int,
        pid: str,
        active: bool = True,
        output: str = "",
    ) -> None:
        self.inventory.append(_pane_row(session, wid, widx, wname, pidx, pid, active))
        self.panes[pid] = (session, wname, active)
        self.output[pid] = output

    def __call__(self, argv: List[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        args = argv[1:]
        if args[:2] == ["list-panes", "-a"]:
            if self.fail_inventory:
                return subprocess.CompletedProcess(argv, 1, "", "no server running")
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.inventory) + "\n", "")
        assert args[0] == "display-message" and args[1] == "-t"
        pid = args[2]
        semi = args.index(";")
        command = args[semi + 1]
        assert args[semi + 2 : semi + 4] == ["-t", pid]
        ident = self.panes.get(pid)
        if ident is None:
            # tmux falls back to "some" pane for display-message and the real
            # command fails: emulate both.
            other = next(iter(self.panes.values()), ("?", "?", False))
            head = SEP.join([other[0], other[1], "%999", "1" if other[2] else "0"])
            return subprocess.CompletedProcess(argv, 1, head + "\n", f"can't find pane: {pid}")
        reported = self.ident_pane_override.get(pid, pid)
        head = SEP.join([ident[0], ident[1], reported, "1" if ident[2] else "0"])
        rc = self.command_rc.get(pid, 0)
        body = self.output.get(pid, "")
        return subprocess.CompletedProcess(argv, rc, head + "\n" + body, "" if rc == 0 else "boom")


@pytest.fixture
def fake() -> FakeTmux:
    f = FakeTmux()
    f.add_pane("s1", "@1", 0, "w1", 0, "%10", active=True, output="line a\nline b\n\n\n")
    f.add_pane("s1", "@2", 1, "w2", 1, "%21", active=True, output="active\n")
    f.add_pane("s1", "@2", 1, "w2", 0, "%20", active=False, output="first\n")
    f.add_pane("s2", "@3", 0, "w1", 0, "%30", active=True, output="other session\n")
    return f


@pytest.fixture
def locator(fake: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> PaneLocator:
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)
    return PaneLocator(runner=fake)


def test_cold_call_is_two_execs_then_one(locator: PaneLocator, fake: FakeTmux) -> None:
    out = locator.run_pane_command("s1", "w1", "capture-pane", "-p", "-S", "-45")
    assert out == ["line a", "line b"]  # trailing empties dropped, libtmux-style
    assert [c[1] for c in fake.calls] == ["list-panes", "display-message"]
    fake.calls.clear()
    out = locator.run_pane_command("s1", "w1", "capture-pane", "-p")
    assert out == ["line a", "line b"]
    assert [c[1] for c in fake.calls] == ["display-message"]  # cache hit: ONE exec


def test_command_argv_shape_matches_libtmux(locator: PaneLocator, fake: FakeTmux) -> None:
    locator.run_pane_command("s1", "w1", "capture-pane", "-e", "-p", "-S", "-80")
    argv = fake.calls[-1]
    semi = argv.index(";")
    assert argv[semi + 1 :] == ["capture-pane", "-t", "%10", "-e", "-p", "-S", "-80"]
    assert argv[1:4] == ["display-message", "-t", "%10"]


def test_first_vs_active_pane_selection(locator: PaneLocator, fake: FakeTmux) -> None:
    assert locator.run_pane_command("s1", "w2", "capture-pane", "-p", pane="first") == ["first"]
    assert locator.run_pane_command("s1", "w2", "display-message", "-p", "x", pane="active") == [
        "active"
    ]


def test_same_window_name_in_other_session_is_not_confused(
    locator: PaneLocator, fake: FakeTmux
) -> None:
    assert locator.run_pane_command("s2", "w1", "capture-pane", "-p") == ["other session"]
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") == ["line a", "line b"]


def test_absent_target_returns_none_not_absence(locator: PaneLocator, fake: FakeTmux) -> None:
    assert locator.run_pane_command("s1", "nope", "capture-pane", "-p") is None
    assert locator.run_pane_command("nosess", "w1", "capture-pane", "-p") is None


def test_ambiguous_window_name_falls_back(fake: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)
    fake.add_pane("s1", "@9", 5, "w1", 0, "%90", output="dup")
    locator = PaneLocator(runner=fake)
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") is None
    # ...but the unambiguous sibling still resolves.
    assert locator.run_pane_command("s1", "w2", "capture-pane", "-p") == ["first"]


def test_stale_id_is_reresolved_once_in_same_call(locator: PaneLocator, fake: FakeTmux) -> None:
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") == ["line a", "line b"]
    # Window recreated: same name, new ids.
    fake.inventory = [r for r in fake.inventory if "%10" not in r]
    del fake.panes["%10"]
    fake.add_pane("s1", "@7", 0, "w1", 0, "%70", output="reborn\n")
    fake.calls.clear()
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") == ["reborn"]
    assert [c[1] for c in fake.calls] == ["display-message", "list-panes", "display-message"]


def test_server_restart_id_reuse_detected_by_identity(locator: PaneLocator, fake: FakeTmux) -> None:
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") == ["line a", "line b"]
    # New server: %10 now belongs to a different window.
    fake.panes["%10"] = ("s1", "somethingelse", True)
    fake.inventory = [_pane_row("s1", "@1", 0, "somethingelse", 0, "%10", True)]
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") is None


def test_identity_pane_id_mismatch_is_stale_even_when_names_match(
    locator: PaneLocator, fake: FakeTmux
) -> None:
    """Same session+window but a different pane id in the identity line -> not our pane."""
    assert locator.run_pane_command("s1", "w2", "capture-pane", "-p") == ["first"]
    fake.ident_pane_override["%20"] = "%21"
    assert locator.run_pane_command("s1", "w2", "capture-pane", "-p") is None


def test_active_pane_focus_change_refreshes(locator: PaneLocator, fake: FakeTmux) -> None:
    assert locator.run_pane_command("s1", "w2", "display-message", "-p", "x", pane="active") == [
        "active"
    ]
    # Focus moved to %20.
    fake.panes["%21"] = ("s1", "w2", False)
    fake.panes["%20"] = ("s1", "w2", True)
    fake.inventory = [
        (
            r.replace(SEP + "1", SEP + "0")
            if "%21" in r
            else r.replace(SEP + "0", SEP + "1") if "%20" in r else r
        )
        for r in fake.inventory
    ]
    assert locator.run_pane_command("s1", "w2", "display-message", "-p", "x", pane="active") == [
        "first"
    ]


def test_command_failure_returns_none(locator: PaneLocator, fake: FakeTmux) -> None:
    fake.command_rc["%10"] = 1
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") is None


def test_inventory_failure_returns_none(locator: PaneLocator, fake: FakeTmux) -> None:
    fake.fail_inventory = True
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") is None
    assert locator.session_windows("s1") is None


def test_runner_exceptions_are_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)

    def boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    locator = PaneLocator(runner=boom)
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") is None

    def slow(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["tmux"], 5.0)

    assert PaneLocator(runner=slow).run_pane_command("s1", "w1", "capture-pane", "-p") is None


def test_mock_like_runner_output_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A patched subprocess (MagicMock) must never be mistaken for tmux output."""
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)
    from unittest.mock import MagicMock

    assert PaneLocator(runner=MagicMock()).run_pane_command("s", "w", "capture-pane") is None


def test_kill_switch(fake: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_TMUX_FAST_LOOKUP", "0")
    locator = PaneLocator(runner=fake)
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") is None
    assert locator.session_windows("s1") is None
    assert fake.calls == []
    monkeypatch.setenv("CAO_TMUX_FAST_LOOKUP", "1")
    assert locator.run_pane_command("s1", "w1", "capture-pane", "-p") == ["line a", "line b"]


def test_empty_output_is_a_real_answer(locator: PaneLocator, fake: FakeTmux) -> None:
    fake.output["%10"] = "\n"
    assert locator.run_pane_command("s1", "w1", "display-message", "-p", "#{x}") == []


def test_session_windows_keeps_duplicates_and_order(
    fake: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)
    fake.add_pane("s1", "@9", 5, "w1", 0, "%90")
    locator = PaneLocator(runner=fake)
    assert locator.session_windows("s1") == [
        {"name": "w1", "index": "0"},
        {"name": "w2", "index": "1"},
        {"name": "w1", "index": "5"},
    ]
    assert locator.session_windows("absent") is None


def test_forget(locator: PaneLocator, fake: FakeTmux) -> None:
    locator.run_pane_command("s1", "w1", "capture-pane", "-p")
    locator.run_pane_command("s2", "w1", "capture-pane", "-p")
    locator.forget("s1")
    assert locator.resolve("s2", "w1") is not None
    fake.calls.clear()
    locator.run_pane_command("s1", "w1", "capture-pane", "-p")
    assert fake.calls[0][1] == "list-panes"


def test_malformed_inventory_line_returns_none(
    fake: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAO_TMUX_FAST_LOOKUP", raising=False)
    fake.inventory.append("garbage")
    assert PaneLocator(runner=fake).refresh() is None


def test_module_singleton_is_a_locator() -> None:
    assert isinstance(tmux_fast.pane_locator, PaneLocator)


class TestTmuxClientIntegration:
    """TmuxClient prefers the fast path and falls back to libtmux on None."""

    def test_get_history_uses_fast_path_and_joins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from cli_agent_orchestrator.clients import tmux as tmux_mod

        client = tmux_mod.TmuxClient.__new__(tmux_mod.TmuxClient)
        client.server = MagicMock()
        calls: List[Tuple[str, str, Tuple[str, ...], str]] = []

        def fake_run(
            session: str, window: str, command: str, *args: str, pane: str = "first"
        ) -> Optional[List[str]]:
            calls.append((session, window, (command,) + args, pane))
            return ["x", "y"]

        monkeypatch.setattr(tmux_mod.pane_locator, "run_pane_command", fake_run)
        assert client.get_history("s", "w", tail_lines=45, strip_escapes=True) == "x\ny"
        assert calls == [("s", "w", ("capture-pane", "-p", "-S", "-45"), "first")]
        client.server.sessions.get.assert_not_called()
        assert client.get_history("s", "w", tail_lines=80) == "x\ny"
        assert calls[-1][2] == ("capture-pane", "-e", "-p", "-S", "-80")

    def test_get_history_falls_back_on_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from cli_agent_orchestrator.clients import tmux as tmux_mod

        client = tmux_mod.TmuxClient.__new__(tmux_mod.TmuxClient)
        client.server = MagicMock()
        client.server.sessions.get.return_value = None
        monkeypatch.setattr(tmux_mod.pane_locator, "run_pane_command", lambda *a, **k: None)
        with pytest.raises(ValueError, match="Session 's' not found"):
            client.get_history("s", "w")

    def test_get_pane_current_command_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from cli_agent_orchestrator.clients import tmux as tmux_mod

        client = tmux_mod.TmuxClient.__new__(tmux_mod.TmuxClient)
        client.server = MagicMock()
        seen: List[str] = []

        def fake_run(
            session: str, window: str, command: str, *args: str, pane: str = "first"
        ) -> Optional[List[str]]:
            seen.append(pane)
            return [" codex "]

        monkeypatch.setattr(tmux_mod.pane_locator, "run_pane_command", fake_run)
        assert client.get_pane_current_command("s", "w") == "codex"
        assert seen == ["active"]
        monkeypatch.setattr(tmux_mod.pane_locator, "run_pane_command", lambda *a, **k: [])
        assert client.get_pane_current_command("s", "w") is None

    def test_get_pane_size_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from cli_agent_orchestrator.clients import tmux as tmux_mod

        client = tmux_mod.TmuxClient.__new__(tmux_mod.TmuxClient)
        client.server = MagicMock()
        monkeypatch.setattr(tmux_mod.pane_locator, "run_pane_command", lambda *a, **k: ["200 50"])
        assert client.get_pane_size("s", "w") == (200, 50)
        monkeypatch.setattr(tmux_mod.pane_locator, "run_pane_command", lambda *a, **k: ["junk"])
        assert client.get_pane_size("s", "w") is None

    def test_get_session_windows_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from cli_agent_orchestrator.clients import tmux as tmux_mod

        client = tmux_mod.TmuxClient.__new__(tmux_mod.TmuxClient)
        client.server = MagicMock()
        rows = [{"name": "a", "index": "0"}]
        monkeypatch.setattr(tmux_mod.pane_locator, "session_windows", lambda s: rows)
        assert client.get_session_windows("s") == rows
        client.server.sessions.get.assert_not_called()
