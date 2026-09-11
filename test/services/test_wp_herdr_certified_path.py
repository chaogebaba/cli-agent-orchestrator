"""WP-HERDR H1 §8/§10 — what stops running on the CERTIFIED path.

Three edits, one rule: on a certified terminal the herdr EventSource is the ONE
lifecycle source, so anything else that infers lifecycle for that terminal is a
second source for the same fact.  Two of the three are GATES rather than
deletions, and the tests below are mostly about the gate being closed on one
side and open on the other — an uncertified pi terminal is every pi terminal
today, and its behaviour must be byte-identical.

The third is a plain deletion: ``_probe_screen_status_stage0a_dead`` was 372
lines of frozen Stage-0a implementation with zero callers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils import herdr_runtime_gate

SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"


@pytest.fixture(autouse=True)
def _clean_gate():
    herdr_runtime_gate.reset_gate()
    yield
    herdr_runtime_gate.reset_gate()


# --------------------------------------------------------------------------
# (a) the dead stage-0a probe is gone
# --------------------------------------------------------------------------


def test_the_dead_stage0a_probe_is_deleted() -> None:
    tree = ast.parse((SRC / "services" / "status_monitor.py").read_text(encoding="utf-8"))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_probe_screen_status_stage0a_dead" not in names
    # The live one is untouched.
    assert "probe_screen_status" in names
    assert "_probe_screen_status_stage0b" in names


# --------------------------------------------------------------------------
# the gate itself
# --------------------------------------------------------------------------


def test_the_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """main stays behaviour-neutral: nothing is certified until the flag is set."""
    monkeypatch.delenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, raising=False)
    herdr_runtime_gate.bind_terminal("t1", True)
    assert herdr_runtime_gate.herdr_runtime_enabled() is False
    assert herdr_runtime_gate.herdr_lifecycle_authoritative("t1") is False


def test_both_halves_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, "1")
    herdr_runtime_gate.bind_terminal("certified", True)
    herdr_runtime_gate.bind_terminal("plain", False)
    assert herdr_runtime_gate.herdr_lifecycle_authoritative("certified") is True
    assert herdr_runtime_gate.herdr_lifecycle_authoritative("plain") is False
    assert herdr_runtime_gate.herdr_lifecycle_authoritative("never-bound") is False


def test_the_answer_never_changes_mid_occupant(monkeypatch: pytest.MonkeyPatch) -> None:
    """§8: never switch truth sources under a live occupant.  A second, different
    answer for a bound terminal is refused, not applied."""
    monkeypatch.setenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, "1")
    herdr_runtime_gate.bind_terminal("t1", True)
    herdr_runtime_gate.bind_terminal("t1", False)
    assert herdr_runtime_gate.terminal_certified("t1") is True
    herdr_runtime_gate.forget_terminal("t1")
    herdr_runtime_gate.bind_terminal("t1", False)
    assert herdr_runtime_gate.terminal_certified("t1") is False


# --------------------------------------------------------------------------
# (b) pi's chrome scraper is gated, never deleted
# --------------------------------------------------------------------------


def _pi_provider(terminal_id: str):
    from cli_agent_orchestrator.providers.pi_cli import PiCliProvider

    provider = PiCliProvider(terminal_id, "s", "w")
    provider._initialized = True
    return provider


#: A REAL captured pi pane showing the live working spinner — the same fixture
#: the provider's own status-truth tests classify.  An invented buffer would not
#: reach the whole-row ``_WORKING_ROW`` anchor and the "uncertified still
#: scrapes" test would pass for the wrong reason (UNKNOWN either way).
_WORKING_PANE = (
    Path(__file__).resolve().parents[1]
    / "providers"
    / "fixtures"
    / "status_truth"
    / "pi_cli"
    / "working-1.txt"
).read_text(encoding="utf-8")


def test_an_uncertified_pi_terminal_still_scrapes_its_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every pi terminal today.  The classifier must be untouched for it."""
    monkeypatch.setenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, "1")
    provider = _pi_provider("plain")
    herdr_runtime_gate.bind_terminal("plain", False)
    monkeypatch.setattr(type(provider), "_resolve_native_status", lambda self, b: None)
    assert provider.get_status(_WORKING_PANE) is TerminalStatus.PROCESSING


def test_a_certified_pi_terminal_infers_no_lifecycle_from_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same buffer, same spinner, certified terminal: UNKNOWN rather than a
    lifecycle answer invented from pixels.  The herdr source owns this fact, and
    its having nothing to say is not permission to guess."""
    monkeypatch.setenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, "1")
    provider = _pi_provider("certified")
    herdr_runtime_gate.bind_terminal("certified", True)
    monkeypatch.setattr(type(provider), "_resolve_native_status", lambda self, b: None)
    assert provider.get_status(_WORKING_PANE) is TerminalStatus.UNKNOWN


def test_a_certified_pi_terminal_still_reports_the_native_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is below the native read, so the authoritative answer wins."""
    monkeypatch.setenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, "1")
    provider = _pi_provider("certified")
    herdr_runtime_gate.bind_terminal("certified", True)
    monkeypatch.setattr(
        type(provider), "_resolve_native_status", lambda self, b: TerminalStatus.PROCESSING
    )
    assert provider.get_status("") is TerminalStatus.PROCESSING


def test_the_chrome_classifier_itself_is_still_present() -> None:
    """A gate, not a deletion: the methods the plan named are all still here."""
    tree = ast.parse((SRC / "providers" / "pi_cli.py").read_text(encoding="utf-8"))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"get_status", "_live_working_spinner", "_has_idle_chrome"} <= names


# --------------------------------------------------------------------------
# (c) the certified cohort is not registered with the one pane sampler
# --------------------------------------------------------------------------


def test_the_watchdog_skips_certified_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    """F506 Do-NOT #1: seam A must not become a second sampler, so a certified
    terminal is never handed to ``pane_liveness.observe``."""
    monkeypatch.setenv(herdr_runtime_gate.HERDR_RUNTIME_ENV_VAR, "1")
    herdr_runtime_gate.bind_terminal("certified", True)
    herdr_runtime_gate.bind_terminal("plain", False)

    from cli_agent_orchestrator.services import pane_liveness as pl_mod
    from cli_agent_orchestrator.services import stalled_callback_watchdog as mod

    observed: list[str] = []
    # The watchdog imports ``pane_liveness`` inside the method, so the patch has
    # to land on the singleton in its own module, not on a watchdog attribute.
    monkeypatch.setattr(
        pl_mod.pane_liveness,
        "observe",
        lambda terminal_id, now, monitor: observed.append(terminal_id),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.list_all_terminals",
        lambda: [{"id": "certified"}, {"id": "plain"}],
    )
    watchdog = mod.StalledCallbackWatchdog()
    watchdog.refresh_screen_fingerprints(now=0.0)
    assert observed == ["plain"]
