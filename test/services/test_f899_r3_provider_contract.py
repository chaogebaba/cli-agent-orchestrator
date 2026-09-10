"""F899 (#751) r3: the repairs the EMPIRICAL gate blocked on (2026-09-10).

Three required repairs, one section each:

  R1  ``child_proc_probe`` misses an exec-replaced direct tool child. The gate's
      live counterexample was a Codex tool call whose command begins with
      ``exec``: the tool shell is replaced, leaving ``codex ─── sleep`` with no
      shell on the branch, and shell ancestry reported ``live=False`` while real
      work ran. The repair is the task-input arm (``terminals.last_active``).

  R2  the shipped suite pinned only pi-shaped trees and only the direct
      ``get_status`` detector, while the module claims the rule for pi, codex,
      grok and claude_code and the fusion feeds three of them through
      ``get_status_from_screen``. This section pins idle/working trees per
      provider — Codex in its real ``command -> node -> codex`` wrapper topology
      — and composes the real detectors on BOTH lowering arms, including the
      screen branch and a published COMPLETED.

  R3  neither per-terminal map this feature added was ever collected. Deleting a
      terminal must evict both.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.child_proc_probe import ChildProcProbe
from cli_agent_orchestrator.services.pane_liveness import PaneLivenessService, _CaptureResult
from cli_agent_orchestrator.services.question_state import QuestionStateService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

# ── synthetic procfs WITH temporal evidence ────────────────────────────────
#
# The pre-r3 fixture writes no ``/proc/stat`` and a zero ``starttime``, which is
# exactly the "no temporal evidence" case that leaves the r3 arm silent. These
# helpers write both, so the task-input arm can be exercised at all.

_BOOT = 1_700_000_000.0
_LAST_INPUT = _BOOT + 10_000.0  # the terminal's last delivered input

# Read as wall-clock offsets from the last input.
_BEFORE_INPUT = _LAST_INPUT - 600.0
_AFTER_INPUT = _LAST_INPUT + 5.0


def _write_tree(tmp_path, tree, *, name="proc"):
    """tree: {pid: (comm, ppid, start_epoch)} -> a directory usable as _PROC_ROOT.

    ``/proc/<pid>/stat`` field 22 (starttime) sits at index 19 of the tail that
    follows the closing paren, i.e. index 17 of everything after ``state ppid``.
    """
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text(f"cpu  0 0 0 0\nbtime {int(_BOOT)}\nprocesses 1\n")
    hz = float(os.sysconf("SC_CLK_TCK") or 100)
    for pid, (comm, ppid, start_epoch) in tree.items():
        d = root / str(pid)
        d.mkdir(exist_ok=True)
        rest = ["0"] * 30
        rest[17] = str(int(round((start_epoch - _BOOT) * hz)))
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {' '.join(rest)}\n")
    return root


@pytest.fixture
def probe_at(monkeypatch, tmp_path):
    """(tree, last_active) -> a ChildProcProbe over that synthetic /proc."""
    import cli_agent_orchestrator.services.fork_context_service as fcs

    counter = {"n": 0}

    def _make(tree, *, pane_pid=100, last_active=_LAST_INPUT):
        counter["n"] += 1
        monkeypatch.setattr(
            fcs, "_PROC_ROOT", _write_tree(tmp_path, tree, name=f"proc{counter['n']}")
        )
        monkeypatch.setattr(fcs, "pane_pid", lambda _s, _w: pane_pid)
        metadata = {"tmux_session": "s", "tmux_window": "w"}
        if last_active is not None:
            from datetime import datetime, timezone

            metadata["last_active"] = datetime.fromtimestamp(last_active, tz=timezone.utc)
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            lambda _tid: metadata,
        )
        return ChildProcProbe()

    return _make


# ── R1: the exec-replaced direct tool child ────────────────────────────────
#
# Measured live by the gate: `zsh -> command -> node -> codex -> sleep`, no
# shell anywhere under the provider, real work in flight.

_CODEX_EXEC_TREE = {
    100: ("zsh", 1, _BEFORE_INPUT),
    101: ("command", 100, _BEFORE_INPUT),
    102: ("node", 101, _BEFORE_INPUT),
    103: ("codex", 102, _BEFORE_INPUT),
    104: ("cao-mcp-server", 103, _BEFORE_INPUT),
    105: ("node_repl", 103, _BEFORE_INPUT),
    106: ("codex-code-mode", 103, _BEFORE_INPUT),
    107: ("sleep", 103, _AFTER_INPUT),  # the exec'd tool target
}

_CODEX_IDLE_TREE = {k: v for k, v in _CODEX_EXEC_TREE.items() if k != 107}


def test_r3_exec_replaced_direct_tool_child_is_live_work(probe_at):
    """R1 BLOCKER: the gate's live counterexample must classify as work."""
    result = probe_at(_CODEX_EXEC_TREE).probe("t1")
    assert result.status == "ok"
    assert result.live is True
    assert "sleep" in result.comms


def test_r3_codex_idle_wrapper_tree_is_not_live(probe_at):
    """The same tree without the tool target — three persistent stdio helpers
    under the provider — must stay non-live, or every Codex seat pins working."""
    result = probe_at(_CODEX_IDLE_TREE).probe("t1")
    assert result.status == "ok"
    assert result.live is False
    assert result.comms == ()


def test_r3_lazily_spawned_helpers_are_not_work_even_after_the_last_input(probe_at):
    """Measured 2026-09-10: codex's helpers start 40-55 s AFTER codex, so the
    input clock alone would read them as work. They are excluded by comm."""
    tree = dict(_CODEX_IDLE_TREE)
    for pid in (104, 105, 106):
        comm, ppid, _ = tree[pid]
        tree[pid] = (comm, ppid, _AFTER_INPUT)
    result = probe_at(tree).probe("t1")
    assert result.live is False


def test_r3_direct_child_started_before_the_last_input_is_not_work(probe_at):
    """A non-shell direct child that predates the open turn is a helper, not
    work — the arm requires positive temporal evidence, never a guess."""
    tree = dict(_CODEX_IDLE_TREE)
    tree[108] = ("some-daemon", 103, _BEFORE_INPUT)
    result = probe_at(tree).probe("t1")
    assert result.live is False


def test_r3_unknown_non_shell_child_started_after_the_input_is_work(probe_at):
    """Not a comm allowlist: any non-helper process started for the open turn
    counts, which is what makes the arm cover exec'd targets generally."""
    tree = dict(_CODEX_IDLE_TREE)
    tree[108] = ("pytest", 103, _AFTER_INPUT)
    result = probe_at(tree).probe("t1")
    assert result.live is True
    assert "pytest" in result.comms


def test_r3_shell_arm_is_never_gated_on_the_input_clock(probe_at):
    """DELIBERATE: a follow-up message delivered while a long tool runs moves
    last_active PAST the tool's start. The shell rule must ignore the input
    clock or F899's original false-IDLE returns."""
    tree = dict(_CODEX_IDLE_TREE)
    tree[108] = ("bash", 103, _BEFORE_INPUT)
    tree[109] = ("pytest", 108, _BEFORE_INPUT)
    result = probe_at(tree).probe("t1")
    assert result.live is True
    assert set(result.comms) >= {"bash", "pytest"}


def test_r3_no_last_active_falls_back_to_the_shell_rule(probe_at):
    """No input clock ⇒ the arm is silent and behaviour is exactly pre-r3."""
    assert probe_at(_CODEX_EXEC_TREE, last_active=None).probe("t1").live is False
    tree = dict(_CODEX_IDLE_TREE)
    tree[108] = ("bash", 103, _AFTER_INPUT)
    assert probe_at(tree, last_active=None).probe("t1").live is True


def test_r3_missing_btime_falls_back_to_the_shell_rule(monkeypatch, tmp_path, probe_at):
    """A procfs root without btime yields no start epochs, so the arm stays
    silent rather than guessing."""
    import cli_agent_orchestrator.services.fork_context_service as fcs

    probe = probe_at(_CODEX_EXEC_TREE)
    root = _write_tree(tmp_path, _CODEX_EXEC_TREE, name="proc-nobtime")
    (root / "stat").write_text("cpu  0 0 0 0\nprocesses 1\n")
    monkeypatch.setattr(fcs, "_PROC_ROOT", root)
    assert probe.probe("t1").live is False


# ── R2 part 1: per-provider idle/working trees ─────────────────────────────
#
# Every shape below was measured on this fleet (see the module docstring of
# child_proc_probe). The contract the module advertises is that ONE rule holds
# for all four providers; these pin it per provider instead of only for pi.


def _pair(provider_comm, helper_comm="cao-mcp-server"):
    """(idle, working) trees for a `zsh -> <provider> -> helper` topology."""
    idle = {
        100: ("zsh", 1, _BEFORE_INPUT),
        101: (provider_comm, 100, _BEFORE_INPUT),
        102: (helper_comm, 101, _BEFORE_INPUT),
    }
    working = dict(idle)
    working[103] = ("bash", 101, _AFTER_INPUT)
    working[104] = ("pytest", 103, _AFTER_INPUT)
    return idle, working


@pytest.mark.parametrize("provider_comm", ["pi", "codex", "grok", "claude"])
def test_r3_provider_idle_tree_is_not_live(probe_at, provider_comm):
    idle, _ = _pair(provider_comm)
    assert probe_at(idle).probe("t1").live is False


@pytest.mark.parametrize("provider_comm", ["pi", "codex", "grok", "claude"])
def test_r3_provider_working_tree_is_live(probe_at, provider_comm):
    _, working = _pair(provider_comm)
    result = probe_at(working).probe("t1")
    assert result.live is True
    assert set(result.comms) == {"bash", "pytest"}


def test_r3_codex_wrapper_topology_working_through_a_shell(probe_at):
    """Codex's real depth: the provider is at depth 3, not depth 1."""
    tree = dict(_CODEX_IDLE_TREE)
    tree[108] = ("bash", 103, _AFTER_INPUT)
    tree[109] = ("grokfleet", 108, _AFTER_INPUT)
    result = probe_at(tree).probe("t1")
    assert result.live is True
    assert set(result.comms) == {"bash", "grokfleet"}


def test_r3_provider_launched_as_the_pane_command_still_discriminates(probe_at):
    """base_depth 0: no login shell, the provider IS the pane process."""
    idle = {
        200: ("codex", 1, _BEFORE_INPUT),
        201: ("cao-mcp-server", 200, _BEFORE_INPUT),
    }
    assert probe_at(idle, pane_pid=200).probe("t1").live is False
    working = dict(idle)
    working[202] = ("sleep", 200, _AFTER_INPUT)
    assert probe_at(working, pane_pid=200).probe("t1").live is True


# ── R2 part 2: the real detectors composed on both lowering arms ───────────
#
# The r2 suite covered only a stub with `supports_direct_status_probe`. Codex,
# grok and claude_code all route through `get_status_from_screen`, which no
# F899 test exercised, and no test used a published COMPLETED.


@pytest.fixture(autouse=True)
def _provider_defaults_file(tmp_path, monkeypatch):
    """Grok/Codex read provider defaults at construction."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.PROVIDER_DEFAULTS_FILE",
        tmp_path / "providers.toml",
    )


def _codex_provider():
    from cli_agent_orchestrator.providers.codex import CodexProvider

    with patch(
        "cli_agent_orchestrator.providers.codex.resolve_provider_binary", return_value="codex"
    ):
        CodexProvider._supports_hook_trust_bypass.cache_clear()
        with patch.object(CodexProvider, "_supports_hook_trust_bypass", staticmethod(lambda: True)):
            return CodexProvider("test1234", "test-session", "window-0")


def _grok_provider():
    from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider

    return GrokCliProvider(
        terminal_id="term-grok",
        session_name="session",
        window_name="window",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )


def _claude_provider():
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    return ClaudeCodeProvider("test123", "test-session", "window-0")


def _pi_provider():
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider

    provider = PiCliProvider("t1234567", "sess", "win0")
    provider._initialized = True
    provider._task_dispatched = True
    provider._tui_processing_seen = True
    return provider


# Screens verbatim from each provider's own passing unit tests.
_CODEX_WORKING = [
    "› Fix the bug",
    "• Working (5s • esc to interrupt)",
    "› ",
    "  ? for shortcuts                     100% context left",
]
_CODEX_READY = [
    "› Fix the bug",
    "• I've fixed the issue in main.py.",
    "",
    "› ",
    "  ? for shortcuts                     100% context left",
]
_GROK_WORKING = [
    "⠋ Thinking… 1.0s",
    "❯",
    "Grok 4.5 (high) · always-approve · ctrl+o transcript",
]
_GROK_READY = ["❯", "Grok 4.5 (high) · always-approve · ctrl+o transcript"]
_CLAUDE_WORKING = [
    "● Working on the task",
    "✻ Cultivating… (12s · ↓ 1.2k tokens)",
    "─" * 60,
    "❯ ",
    "─" * 60,
]
_CLAUDE_READY = [
    "─" * 60,
    '❯ Try "fix typecheck errors"',
    "─" * 60,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]


def _pi_screens():
    from pathlib import Path

    fixtures = Path(__file__).parents[1] / "providers" / "fixtures"
    return (
        (fixtures / "pi_processing.txt").read_text(encoding="utf-8"),
        (fixtures / "pi_idle.txt").read_text(encoding="utf-8"),
    )


# (label, provider factory, working tail, ready tail, expected ready status)
def _cases():
    pi_working, pi_ready = _pi_screens()
    return [
        (
            "codex",
            _codex_provider,
            "\n".join(_CODEX_WORKING),
            "\n".join(_CODEX_READY),
            TerminalStatus.COMPLETED,
        ),
        (
            "grok",
            _grok_provider,
            "\n".join(_GROK_WORKING),
            "\n".join(_GROK_READY),
            TerminalStatus.IDLE,
        ),
        (
            "claude_code",
            _claude_provider,
            "\n".join(_CLAUDE_WORKING),
            "\n".join(_CLAUDE_READY),
            TerminalStatus.IDLE,
        ),
        # pi's ready fixture parses COMPLETED once a task has been dispatched
        # (its own unit test reads IDLE only with _task_dispatched False); both
        # are ready verdicts and fuse_status admits either.
        ("pi", _pi_provider, pi_working, pi_ready, TerminalStatus.COMPLETED),
    ]


def _ids():
    return [c[0] for c in _cases()]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def fusion(monkeypatch):
    """(published) -> a StatusMonitor whose pane hold has expired, plus a seeder."""
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    pane = PaneLivenessService(_clock=clock)
    monkeypatch.setattr(pl, "pane_liveness", pane)
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    sm = StatusMonitor()

    def _expire(published):
        captured = {"fp": 0}

        def fake_capture(_tid):
            captured["fp"] += 1
            return _CaptureResult(str(captured["fp"]), "tail", None, 0, ())

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch.object(pane, "_capture", side_effect=fake_capture),
            patch.object(sm, "get_published_status", return_value=published),
        ):
            pane.observe("t1", monitor=sm)
            clock.advance(301.0)
            pane.observe("t1", monitor=sm)
        assert pane.peek("t1").pane_hold_expired is True

    def _seed(tail):
        with pane._lock:
            state = pane._state.setdefault("t1", pl._PaneState())
            state.fp = "fp"
            state.filtered_tail = tail
            state.sampled_at = clock()
            state.last_change_monotonic = clock()

    return sm, _expire, _seed


def _install(monkeypatch, sm, provider, probe):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", probe)


@pytest.mark.parametrize("label,factory,working,ready,ready_status", _cases(), ids=_ids())
def test_r3_real_detector_holds_processing_on_the_idle_arm(
    monkeypatch, fusion, probe_at, label, factory, working, ready, ready_status
):
    """Both branches of _snapshot_detector_mode composed against the real
    provider: three of these route through get_status_from_screen, which no
    F899 test exercised."""
    provider = factory()
    sm, expire, seed = fusion
    expire(TerminalStatus.IDLE)
    idle_tree, _ = _pair(label.split("_")[0])
    _install(monkeypatch, sm, provider, probe_at(idle_tree))
    seed(working)
    status, reason = sm.fuse_status("t1", TerminalStatus.IDLE)
    assert (status, reason) == (TerminalStatus.PROCESSING, "fresh_capture_working")


@pytest.mark.parametrize("label,factory,working,ready,ready_status", _cases(), ids=_ids())
def test_r3_real_detector_holds_processing_on_the_error_arm(
    monkeypatch, fusion, probe_at, label, factory, working, ready, ready_status
):
    provider = factory()
    sm, _expire, seed = fusion
    idle_tree, _ = _pair(label.split("_")[0])
    _install(monkeypatch, sm, provider, probe_at(idle_tree))
    seed(working)
    status, reason = sm.fuse_status("t1", TerminalStatus.ERROR)
    assert (status, reason) == (TerminalStatus.PROCESSING, "fresh_capture_working")


@pytest.mark.parametrize("label,factory,working,ready,ready_status", _cases(), ids=_ids())
def test_r3_real_detector_admits_a_ready_verdict_on_the_error_arm(
    monkeypatch, fusion, probe_at, label, factory, working, ready, ready_status
):
    """A stale-buffer ERROR over a genuinely ready pane admits the READY
    verdict, not the ERROR — per provider, through its own detector."""
    provider = factory()
    sm, _expire, seed = fusion
    idle_tree, _ = _pair(label.split("_")[0])
    _install(monkeypatch, sm, provider, probe_at(idle_tree))
    seed(ready)
    status, reason = sm.fuse_status("t1", TerminalStatus.ERROR)
    assert status is ready_status
    assert reason == "fresh_capture_idle"


@pytest.mark.parametrize("label,factory,working,ready,ready_status", _cases(), ids=_ids())
def test_r3_published_completed_takes_the_same_expired_hold_arms(
    monkeypatch, fusion, probe_at, label, factory, working, ready, ready_status
):
    """H3.4: status_monitor claims the expired-hold arm for IDLE *and*
    COMPLETED, but every shipped test published IDLE."""
    provider = factory()
    sm, expire, seed = fusion
    expire(TerminalStatus.COMPLETED)
    _, working_tree = _pair(label.split("_")[0])
    _install(monkeypatch, sm, provider, probe_at(working_tree))
    seed(ready)
    status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)
    assert (status, reason) == (TerminalStatus.PROCESSING, "child_proc_live")


@pytest.mark.parametrize("label,factory,working,ready,ready_status", _cases(), ids=_ids())
def test_r3_published_completed_falls_through_when_nothing_is_running(
    monkeypatch, fusion, probe_at, label, factory, working, ready, ready_status
):
    provider = factory()
    sm, expire, seed = fusion
    expire(TerminalStatus.COMPLETED)
    idle_tree, _ = _pair(label.split("_")[0])
    _install(monkeypatch, sm, provider, probe_at(idle_tree))
    seed(ready)
    status, reason = sm.fuse_status("t1", TerminalStatus.COMPLETED)
    assert (status, reason) == (TerminalStatus.COMPLETED, "pane_delta_expired")


def test_r3_screen_detector_branch_is_actually_taken(monkeypatch, fusion, probe_at):
    """Pins the ROUTING, not just the verdict: a screen-detection provider must
    be called through get_status_from_screen with LINES, never get_status."""
    provider = _codex_provider()
    calls = {"screen": 0, "direct": 0}
    real_screen = provider.get_status_from_screen

    def spy_screen(lines):
        calls["screen"] += 1
        assert isinstance(lines, list)
        return real_screen(lines)

    monkeypatch.setattr(provider, "get_status_from_screen", spy_screen)
    monkeypatch.setattr(
        provider, "get_status", lambda _t: calls.__setitem__("direct", calls["direct"] + 1)
    )
    sm, expire, seed = fusion
    expire(TerminalStatus.IDLE)
    idle_tree, _ = _pair("codex")
    _install(monkeypatch, sm, provider, probe_at(idle_tree))
    seed("\n".join(_CODEX_WORKING))
    status, reason = sm.fuse_status("t1", TerminalStatus.IDLE)
    assert (status, reason) == (TerminalStatus.PROCESSING, "fresh_capture_working")
    assert calls == {"screen": 1, "direct": 0}


# ── R3: lifecycle eviction of both new per-terminal maps ───────────────────


def test_r3_clear_terminal_evicts_both_new_caches(monkeypatch, probe_at):
    """BLOCKER 3: ChildProcProbe._cache and StatusMonitor._last_rederive_check
    were populated per terminal and never collected."""
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    monkeypatch.setattr(pl, "pane_liveness", PaneLivenessService(_clock=clock))
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    sm = StatusMonitor()
    _, working = _pair("pi")
    probe = probe_at(working)
    monkeypatch.setattr("cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", probe)

    probe.probe("t1")
    sm._rederive_from_pane_sample("t1", "")
    assert probe.peek("t1") is not None
    assert "t1" in sm._last_rederive_check

    sm.clear_terminal("t1")

    assert probe.peek("t1") is None
    assert "t1" not in sm._last_rederive_check


def test_r3_unregister_evicts_both_new_caches(monkeypatch, probe_at):
    """unregister() delegates to clear_terminal, so delete covers both paths."""
    import cli_agent_orchestrator.services.pane_liveness as pl
    import cli_agent_orchestrator.services.question_state as qs

    clock = _Clock()
    monkeypatch.setattr(pl, "pane_liveness", PaneLivenessService(_clock=clock))
    monkeypatch.setattr(qs, "question_state", QuestionStateService(_clock=clock))
    sm = StatusMonitor()
    _, working = _pair("pi")
    probe = probe_at(working)
    monkeypatch.setattr("cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", probe)

    probe.probe("t2")
    sm._rederive_from_pane_sample("t2", "")
    sm.unregister("t2")

    assert probe.peek("t2") is None
    assert "t2" not in sm._last_rederive_check


def test_r3_clear_terminal_never_raises_when_the_probe_explodes(monkeypatch):
    """Teardown must survive a broken probe — clear_terminal is a delete path."""
    exploding = MagicMock()
    exploding.forget.side_effect = RuntimeError("boom")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.child_proc_probe.child_proc_probe", exploding
    )
    StatusMonitor().clear_terminal("t3")
