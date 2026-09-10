"""F899 (#751): child-process liveness before the pane-hold expiry falls to idle.

Three branches of the new rule-3b arm, plus the probe unit itself:

  (a) hold expired + a live tool subprocess  -> PROCESSING / "child_proc_live"
  (b) hold expired + no live subprocess      -> published  / "pane_delta_expired"
  (c) probe cannot answer (pid gone/denied)  -> published  / "pane_delta_expired"

The tree shapes are the ones measured live on the fleet 2026-09-10 (see
``child_proc_probe`` module docstring): an IDLE worker still carries a
persistent ``cao-mcp-server`` under the provider, so (b) is only meaningful
against that shape — a naive "any descendant" rule fails it.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.child_proc_probe import ChildProcProbe
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ── synthetic procfs ────────────────────────────────────────────────────────
#
# Only ``/proc/<pid>/stat`` is read, and only fields comm (in parens) and ppid
# (the second field after the closing paren), so the fixture writes a minimal
# but format-faithful stat line.

_STAT_TAIL = " ".join(["0"] * 30)


def _write_tree(tmp_path, tree):
    """tree: {pid: (comm, ppid)} -> a directory usable as _PROC_ROOT."""
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    for pid, (comm, ppid) in tree.items():
        d = root / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {_STAT_TAIL}\n")
    return root


# Measured live: idle pi_cli worker.
_IDLE_TREE = {
    100: ("zsh", 1),  # pane pid
    101: ("pi", 100),  # the provider
    102: ("cao-mcp-server", 101),  # PERSISTENT stdio helper — not work
}

# Measured live: the same worker running `grokfleet lease run ... pytest`.
_WORKING_TREE = {
    100: ("zsh", 1),
    101: ("pi", 100),
    102: ("cao-mcp-server", 101),
    103: ("bash", 101),  # the provider's Bash tool
    104: ("grokfleet", 103),
    105: ("sshpass", 104),
    106: ("ssh", 105),
}


@pytest.fixture
def probe_at(monkeypatch, tmp_path):
    """Return a factory: (tree) -> a ChildProcProbe pointed at that synthetic /proc."""
    import cli_agent_orchestrator.services.fork_context_service as fcs

    def _make(tree, *, pane_pid=100, metadata={"tmux_session": "s", "tmux_window": "w"}):
        if tree is not None:
            monkeypatch.setattr(fcs, "_PROC_ROOT", _write_tree(tmp_path, tree))
        monkeypatch.setattr(fcs, "pane_pid", lambda _s, _w: pane_pid)
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            lambda _tid: metadata,
        )
        return ChildProcProbe()

    return _make


# ── probe unit ──────────────────────────────────────────────────────────────


def test_probe_idle_tree_is_not_live(probe_at):
    """The persistent MCP helper under the provider is NOT live work (the
    discriminator that keeps this fix from pinning every seat PROCESSING)."""
    result = probe_at(_IDLE_TREE).probe("t1")
    assert result.status == "ok"
    assert result.live is False
    assert result.comms == ()


def test_probe_working_tree_is_live_and_names_the_subprocesses(probe_at):
    result = probe_at(_WORKING_TREE).probe("t1")
    assert result.status == "ok"
    assert result.live is True
    # The shell and everything under it; the MCP helper is excluded.
    assert set(result.comms) == {"bash", "grokfleet", "sshpass", "ssh"}
    assert "cao-mcp-server" not in result.comms


def test_probe_caps_comms_at_five(probe_at):
    tree = {100: ("zsh", 1), 101: ("pi", 100), 102: ("bash", 101)}
    for i in range(8):
        tree[200 + i] = (f"worker{i}", 102)
    result = probe_at(tree).probe("t1")
    assert result.live is True
    assert len(result.comms) == 5


def test_probe_provider_as_pane_command(probe_at):
    """base_depth 0 layout: the provider IS the pane process, tool shells at
    depth 1. The helper is still excluded, the shell still counts."""
    tree = {
        100: ("node", 1),  # pane pid is the provider itself, not a shell
        101: ("cao-mcp-server", 100),
        102: ("bash", 100),
        103: ("pytest", 102),
    }
    result = probe_at(tree).probe("t1")
    assert result.live is True
    assert set(result.comms) == {"bash", "pytest"}


def test_probe_pid_gone_is_unavailable_not_an_exception(probe_at):
    result = probe_at(_IDLE_TREE, pane_pid=999999).probe("t1")
    assert result.status == "unavailable"
    assert result.reason == "pane_pid_gone"
    assert result.live is False


def test_probe_no_metadata_is_unavailable(probe_at):
    result = probe_at(_IDLE_TREE, metadata=None).probe("t1")
    assert result.status == "unavailable"
    assert result.reason == "no_metadata"
    assert result.live is False


def test_probe_procfs_denied_is_unavailable(monkeypatch, probe_at):
    probe = probe_at(_IDLE_TREE)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.child_proc_probe._live_work",
        MagicMock(side_effect=PermissionError("denied")),
    )
    result = probe.probe("t1")
    assert result.status == "unavailable"
    assert result.reason == "procfs_unavailable"
    assert result.live is False


def test_probe_is_ttl_cached(probe_at):
    probe = probe_at(_WORKING_TREE)
    calls = {"n": 0}
    real = probe._probe_uncached

    def counting(tid):
        calls["n"] += 1
        return real(tid)

    probe._probe_uncached = counting  # type: ignore[method-assign]
    probe.probe("t1", now=0.0)
    probe.probe("t1", now=1.0)
    assert calls["n"] == 1  # inside the 5 s TTL
    probe.probe("t1", now=100.0)
    assert calls["n"] == 2


def test_peek_is_pure(probe_at):
    probe = probe_at(_WORKING_TREE)
    assert probe.peek("t1") is None  # never probed -> no scan, no result
    probe.probe("t1")
    assert probe.peek("t1").live is True


# ── the fuse_status arm ─────────────────────────────────────────────────────


@pytest.fixture
def expired_hold(monkeypatch):
    """Drive a terminal to pane_hold_expired and hand back (monitor, published)."""
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    sm = StatusMonitor()
    captured = {"fp": 0}

    def fake_capture(_tid):
        captured["fp"] += 1
        return _CaptureResult(str(captured["fp"]), "tail", None, 0, ())  # churns

    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.IDLE),
    ):
        pane.observe("t1", monitor=sm)
        clock.advance(301.0)
        pane.observe("t1", monitor=sm)
        assert pane.peek("t1").pane_hold_expired is True
        yield sm


def _install_probe(monkeypatch, probe):
    monkeypatch.setattr("cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", probe)


def test_branch_a_expired_hold_with_live_child_stays_processing(
    monkeypatch, expired_hold, probe_at
):
    _install_probe(monkeypatch, probe_at(_WORKING_TREE))
    status, reason = expired_hold.fuse_status("t1", TerminalStatus.IDLE)
    assert status is TerminalStatus.PROCESSING
    assert reason == "child_proc_live"


def test_branch_b_expired_hold_without_live_child_falls_to_idle(
    monkeypatch, expired_hold, probe_at
):
    _install_probe(monkeypatch, probe_at(_IDLE_TREE))
    status, reason = expired_hold.fuse_status("t1", TerminalStatus.IDLE)
    assert status is TerminalStatus.IDLE
    assert reason == "pane_delta_expired"


def test_branch_c_probe_failure_keeps_pre_f899_behaviour(monkeypatch, expired_hold, probe_at):
    _install_probe(monkeypatch, probe_at(_IDLE_TREE, pane_pid=999999))
    status, reason = expired_hold.fuse_status("t1", TerminalStatus.IDLE)
    assert status is TerminalStatus.IDLE
    assert reason == "pane_delta_expired"


def test_branch_c_probe_raising_is_swallowed(monkeypatch, expired_hold):
    exploding = MagicMock()
    exploding.probe.side_effect = RuntimeError("boom")
    _install_probe(monkeypatch, exploding)
    status, reason = expired_hold.fuse_status("t1", TerminalStatus.IDLE)
    assert status is TerminalStatus.IDLE
    assert reason == "pane_delta_expired"


def test_unexpired_hold_never_probes(monkeypatch, probe_at):
    """The probe is scoped to the expiry arm — the ordinary pane_delta path
    must not pay for a /proc walk."""
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    sm = StatusMonitor()
    probe = probe_at(_WORKING_TREE)
    sentinel = MagicMock(wraps=probe)
    _install_probe(monkeypatch, sentinel)
    captured = {"fp": 0}

    def fake_capture(_tid):
        captured["fp"] += 1
        return _CaptureResult(str(captured["fp"]), "tail", None, 0, ())

    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch.object(pane, "_capture", side_effect=fake_capture),
        patch.object(sm, "get_published_status", return_value=TerminalStatus.IDLE),
    ):
        pane.observe("t1", monitor=sm)
    status, reason = sm.fuse_status("t1", TerminalStatus.IDLE)
    assert status is TerminalStatus.PROCESSING
    assert reason == "pane_delta"
    sentinel.probe.assert_not_called()


# ── payload ─────────────────────────────────────────────────────────────────


def test_fleet_child_procs_key_is_pure_and_only_set_after_a_live_probe(monkeypatch, probe_at):
    from cli_agent_orchestrator.services.fleet_service import _child_procs

    probe = probe_at(_WORKING_TREE)
    _install_probe(monkeypatch, probe)
    assert _child_procs("t1") is None  # never probed -> no scan
    probe.probe("t1")
    assert set(_child_procs("t1")) == {"bash", "grokfleet", "sshpass", "ssh"}


def test_fleet_child_procs_none_when_probe_unavailable(monkeypatch, probe_at):
    from cli_agent_orchestrator.services.fleet_service import _child_procs

    probe = probe_at(_IDLE_TREE, pane_pid=999999)
    _install_probe(monkeypatch, probe)
    probe.probe("t1")
    assert _child_procs("t1") is None
