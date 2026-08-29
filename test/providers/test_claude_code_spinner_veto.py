"""F568 #425 D12d — claude_code spinner busy-marker veto (AC-F568-6 predicate).

Pure unit tests for the extracted helper ``new_tui_box_spinner_live`` and the
provider wiring (``ClaudeCodeProvider.rule3a_busy_marker`` /
``BaseProvider.rule3a_busy_marker``). These consume the byte-exact live fixtures
committed under ``test/providers/fixtures/f568/`` — never synthesised strings.

The predicate is parametrised (§10 AC-F568-6):
  * ``True``  on every spinner fixture (incl. the bare `<glyph> <Gerund>…` and
    the `›`-push capture the window-6/`›`-skip change now catches);
  * ``False`` on ``idle-subagent-churn`` and the wpq1 composer fixtures;
  * ``None``  on the codex approval-modal family and any snapshot with no
    complete rail-prompt-rail box.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.claude_code import new_tui_box_spinner_live

_FIX = Path(__file__).parent / "fixtures"
_F568 = _FIX / "f568"
_WPQ1 = _FIX / "wpq1_claude_2_1_211"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---- AC-F568-6 predicate matrix -----------------------------------------
_TRUE_FIXTURES = [
    _F568 / "spinner-cascading.txt",
    _F568 / "spinner-roosting.txt",
    _F568 / "spinner-ebbing-bare.txt",  # bare <glyph> <Gerund>… (no tuple)
    _F568 / "supervisor-pane-working-033435.txt",  # `›`-push + window-6 positive
]
_FALSE_FIXTURES = [
    _F568 / "idle-subagent-churn-a.txt",
    _F568 / "idle-subagent-churn-b.txt",
    _WPQ1 / "completed-composer.txt",
    _WPQ1 / "initial-empty-composer.txt",
]
_NONE_FIXTURES = [
    _FIX / "codex_approval_modal.txt",
    _FIX / "codex_approval_modal_raw.txt",
    _FIX / "codex_idle_output.txt",
    _FIX / "codex_processing_output.txt",
]


@pytest.mark.parametrize("path", _TRUE_FIXTURES, ids=lambda p: p.name)
def test_helper_true_on_live_spinner_fixtures(path):
    assert new_tui_box_spinner_live(_read(path)) is True


@pytest.mark.parametrize("path", _FALSE_FIXTURES, ids=lambda p: p.name)
def test_helper_false_on_idle_and_composer_fixtures(path):
    assert new_tui_box_spinner_live(_read(path)) is False


@pytest.mark.parametrize("path", _NONE_FIXTURES, ids=lambda p: p.name)
def test_helper_none_on_no_box_fixtures(path):
    assert new_tui_box_spinner_live(_read(path)) is None


def test_helper_none_on_plain_text_without_box():
    assert new_tui_box_spinner_live("just some scrollback\nno rail no prompt") is None


# ---- ≥3 distinct gerunds incl. one bare (fixture provenance guard) -------
def test_distinct_spinner_verbs_present():
    """Guards the fixture set: ≥3 distinct gerunds, one of them bare."""
    import re

    verbs = set()
    bare_seen = False
    for p in _TRUE_FIXTURES:
        for line in _read(p).splitlines():
            m = re.search(r"[✶✢✽✻✳·*]\s+([A-Za-z]+ing)…", line)
            if m:
                verbs.add(m.group(1))
                # bare = glyph + gerund + … with nothing else after the ellipsis
                if re.search(r"[✶✢✽✻✳·*]\s+[A-Za-z]+ing…\s*$", line):
                    bare_seen = True
    assert len(verbs) >= 3, f"need >=3 distinct gerunds, saw {sorted(verbs)}"
    assert bare_seen, "need at least one bare <glyph> <Gerund>… fixture"


# ---- ClaudeCodeProvider.rule3a_busy_marker delegates to the helper -------
def test_base_provider_rule3a_busy_marker_returns_none():
    """The default (every non-claude provider) is byte-identical no-signal.

    Called unbound with ``self=None`` — the default implementation ignores self
    and BaseProvider is abstract (cannot be instantiated directly)."""
    assert BaseProvider.rule3a_busy_marker(None, _read(_F568 / "spinner-cascading.txt")) is None
    assert BaseProvider.rule3a_busy_marker(None, _read(_F568 / "idle-subagent-churn-a.txt")) is None
    assert BaseProvider.rule3a_busy_marker(None, "anything") is None


def test_claude_provider_rule3a_busy_marker_matches_helper():
    """ClaudeCodeProvider.rule3a_busy_marker returns exactly the helper verdict.

    Called unbound so no live session/construction is needed — the method is a
    pure delegation to the shared module-level helper.
    """
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    for path in _TRUE_FIXTURES:
        text = _read(path)
        assert ClaudeCodeProvider.rule3a_busy_marker(None, text) is new_tui_box_spinner_live(text)
    for path in _FALSE_FIXTURES:
        text = _read(path)
        assert ClaudeCodeProvider.rule3a_busy_marker(None, text) is new_tui_box_spinner_live(text)
    for path in _NONE_FIXTURES:
        text = _read(path)
        assert ClaudeCodeProvider.rule3a_busy_marker(None, text) is new_tui_box_spinner_live(text)


# ---- get_status behaviour change: `›`-push positive returns PROCESSING ----
def test_get_status_processing_on_push_row_positive():
    """The window-6 + `›`-skip change fixes a latent false-COMPLETED: the
    supervisor `›`-push capture (spinner five rows above the top rail behind a
    `⎿` hint AND a `›` teammate-push row) now reads PROCESSING in get_status."""
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    provider = ClaudeCodeProvider("test123", "test-session", "window-0")
    text = _read(_F568 / "supervisor-pane-working-033435.txt")
    assert provider.get_status(text) is TerminalStatus.PROCESSING


def test_get_status_not_processing_on_idle_subagent_churn():
    """idle-subagent-churn (no spinner above the composer) is NOT PROCESSING —
    the seat's own turn is over; only a background Agent is active."""
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    provider = ClaudeCodeProvider("test123", "test-session", "window-0")
    text = _read(_F568 / "idle-subagent-churn-a.txt")
    assert provider.get_status(text) is not TerminalStatus.PROCESSING
