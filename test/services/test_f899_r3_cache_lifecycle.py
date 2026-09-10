"""F899 (#751) r3 blocker 3: both new per-terminal caches are collected on delete.

The EMPIRICAL gate (2026-09-10) found that this feature introduced two
per-terminal maps and evicted neither: ``ChildProcProbe._cache``, populated on
every probe and exposing an uncalled ``forget``, and
``StatusMonitor._last_rederive_check``, populated by the fresh-sample
re-derivation and not popped by ``clear_terminal``. Every deleted terminal
therefore leaked two process-lifetime entries.

Scope note: the gate's blockers 1 and 2 (the shell-ancestry discriminator's
blind spot for an exec-replaced tool child, and the missing four-provider
contract pins) are NOT addressed here. They are deferred to the structural
co-design of the status-truth fix, which is expected to replace the
discriminator with a per-provider contract table. This file pins only the
lifecycle repair, which is independent of whichever discriminator survives.
"""

from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.services.child_proc_probe import ChildProcProbe
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

_STAT_TAIL = " ".join(["0"] * 30)

# The measured working tree: a tool shell under the provider, so the probe
# caches a live result and eviction is observable rather than vacuous.
_WORKING_TREE = {
    100: ("zsh", 1),
    101: ("pi", 100),
    102: ("cao-mcp-server", 101),
    103: ("bash", 101),
    104: ("pytest", 103),
}


def _write_tree(tmp_path, tree):
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    for pid, (comm, ppid) in tree.items():
        d = root / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {_STAT_TAIL}\n")
    return root


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """A ChildProcProbe over a synthetic /proc showing live work."""
    import cli_agent_orchestrator.services.fork_context_service as fcs

    monkeypatch.setattr(fcs, "_PROC_ROOT", _write_tree(tmp_path, _WORKING_TREE))
    monkeypatch.setattr(fcs, "pane_pid", lambda _s, _w: 100)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _tid: {"tmux_session": "s", "tmux_window": "w"},
    )
    instance = ChildProcProbe()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", instance
    )
    return instance


@pytest.fixture
def monitor(monkeypatch):
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    monkeypatch.setattr(pl, "pane_liveness", PaneLivenessService(_clock=clock))
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    return StatusMonitor()


def test_r3_clear_terminal_evicts_both_new_caches(probe, monitor):
    """BLOCKER 3: neither map was collected on delete."""
    probe.probe("t1")
    monitor._rederive_from_pane_sample("t1", "")
    assert probe.peek("t1") is not None
    assert "t1" in monitor._last_rederive_check

    monitor.clear_terminal("t1")

    assert probe.peek("t1") is None
    assert "t1" not in monitor._last_rederive_check


def test_r3_unregister_evicts_both_new_caches(probe, monitor):
    """unregister() delegates to clear_terminal, so delete covers both paths."""
    probe.probe("t2")
    monitor._rederive_from_pane_sample("t2", "")

    monitor.unregister("t2")

    assert probe.peek("t2") is None
    assert "t2" not in monitor._last_rederive_check


def test_r3_eviction_does_not_disturb_other_terminals(probe, monitor):
    """Per-terminal eviction, not a cache flush — a live sibling keeps its
    entries or the next fleet read pays for a fresh /proc walk on every seat."""
    probe.probe("keep")
    monitor._rederive_from_pane_sample("keep", "")
    probe.probe("drop")
    monitor._rederive_from_pane_sample("drop", "")

    monitor.clear_terminal("drop")

    assert probe.peek("keep") is not None
    assert "keep" in monitor._last_rederive_check


def test_r3_clear_terminal_never_raises_when_the_probe_explodes(monkeypatch, monitor):
    """Teardown must survive a broken probe — clear_terminal is a delete path
    and its callers have nothing useful to do with an exception from a cache."""
    exploding = MagicMock()
    exploding.forget.side_effect = RuntimeError("boom")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", exploding
    )
    monitor.clear_terminal("t3")
    exploding.forget.assert_called_once_with("t3")
