"""F117 busy-pane classifier tests.

Covers AC1-AC4 from blueprint fx-f117-busy-pane-classifier.md:
  AC1: fixture row → PROCESSING (blocking)
  AC2: same-row spinner + footer → progress only
  AC3: scrollback waiting ignored (viewport restriction)
  AC4: genuine bottom dialog → WAITING_USER_ANSWER (negative control)
  AC5: classification.py unchanged (verified by CI diff gate, not a unit test)
  + midrow braille spinner matches (test plan item 5)
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider


@pytest.fixture(autouse=True)
def provider_defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.toml"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.PROVIDER_DEFAULTS_FILE",
        path,
    )
    return path


def _provider() -> GrokCliProvider:
    return GrokCliProvider(
        terminal_id="term-f117",
        session_name="session",
        window_name="window",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "error-pane-samples"
    / "f115-autoresponder-fires-on-thinking-terminal-2026-08-08.txt"
)


class TestF117FixtureRowIsProcessing:
    """AC1 (BLOCKING): fixture row fed to emit_screen_signals yields progress, no waiting."""

    def test_fixture_emits_progress_no_waiting(self):
        fixture_text = FIXTURE_PATH.read_text(encoding="utf-8")
        # Take non-comment lines as the screen
        screen = [
            line for line in fixture_text.splitlines() if not line.startswith("#")
        ]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        signal_classes = {s.signal_class for s in signals}
        assert "progress" in signal_classes
        assert "waiting" not in signal_classes

    def test_fixture_classifies_processing(self):
        fixture_text = FIXTURE_PATH.read_text(encoding="utf-8")
        screen = [
            line for line in fixture_text.splitlines() if not line.startswith("#")
        ]
        provider = _provider()
        assert provider.get_status_from_screen(screen) == TerminalStatus.PROCESSING


class TestF117SameRowSpinnerAndFooterPrefersProgress:
    """AC2: row with spinner AND Enter:submit emits progress only."""

    def test_spinner_and_footer_on_same_row(self):
        # Synthetic row combining ┃◆ Thinking… and Enter:submit footer
        screen = [
            "some scrollback",
            "more scrollback",
            "┃◆ Thinking… press Enter:submit to continue",
        ]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        signal_classes = {s.signal_class for s in signals}
        assert "progress" in signal_classes
        assert "waiting" not in signal_classes

    def test_braille_spinner_and_navigate_on_same_row(self):
        screen = [
            "output",
            "output",
            "⠙ Responding… 2.5s  ↑/↓ navigate something",
        ]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        signal_classes = {s.signal_class for s in signals}
        assert "progress" in signal_classes
        assert "waiting" not in signal_classes


class TestF117ScrollbackWaitingIsIgnored:
    """AC3: Enter:submit in upper scrollback + live spinner at bottom → progress only."""

    def test_scrollback_waiting_not_emitted(self):
        # Enter:submit in row 0 (well above bottom 5), spinner in last row
        screen = [
            "The dialog said Enter:submit to confirm",
            "some prose row 1",
            "some prose row 2",
            "some prose row 3",
            "some prose row 4",
            "some prose row 5",
            "some prose row 6",
            "some prose row 7",
            "some prose row 8",
            "⠹ Thinking… 3.1s",
            "❯",
            "Grok 4.5 (high) · always-approve · ctrl+o transcript",
        ]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        waiting_signals = [s for s in signals if s.signal_class == "waiting"]
        progress_signals = [s for s in signals if s.signal_class == "progress"]
        assert len(waiting_signals) == 0
        assert len(progress_signals) >= 1

    def test_scrollback_waiting_classifies_processing(self):
        screen = [
            "The dialog said Enter:submit to confirm",
            "some prose row 1",
            "some prose row 2",
            "some prose row 3",
            "some prose row 4",
            "some prose row 5",
            "some prose row 6",
            "some prose row 7",
            "some prose row 8",
            "⠹ Thinking… 3.1s",
            "❯",
            "Grok 4.5 (high) · always-approve · ctrl+o transcript",
        ]
        provider = _provider()
        assert provider.get_status_from_screen(screen) == TerminalStatus.PROCESSING


class TestF117BottomDialogStillWaiting:
    """AC4 (negative control): genuine bottom dialog emits waiting → WAITING_USER_ANSWER."""

    def test_genuine_dialog_in_bottom_rows(self):
        screen = [
            "some output",
            "more output",
            "Run Grok Build in a project directory?",
            "  Yes",
            "  No",
        ]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        waiting_signals = [s for s in signals if s.signal_class == "waiting"]
        assert len(waiting_signals) >= 1

    def test_genuine_dialog_classifies_waiting(self):
        screen = [
            "some output",
            "more output",
            "Run Grok Build in a project directory?",
            "  Yes",
            "  No",
        ]
        provider = _provider()
        assert provider.get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER

    def test_navigate_hint_in_bottom_rows(self):
        screen = [
            "some output",
            "more output",
            "more output 2",
            "↑/↓ navigate",
            "Enter:submit",
        ]
        provider = _provider()
        assert provider.get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER


class TestF117MidrowBrailleSpinnerMatches:
    """Test plan item 5: ┃ ┃... ⠙ Thinking… 2.7s matches and emits corroborable progress."""

    def test_midrow_pipe_braille_thinking(self):
        row = "┃ ┃... ⠙ Thinking… 2.7s 1m19s ⇣74.7k"
        screen = [row]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        progress_signals = [s for s in signals if s.signal_class == "progress"]
        assert len(progress_signals) >= 1
        assert progress_signals[0].temporal_policy == "corroborable"

    def test_diamond_spinner_thinking(self):
        row = "┃◆ Thinking…"
        screen = [row]
        provider = _provider()
        signals = provider.emit_screen_signals(screen)
        progress_signals = [s for s in signals if s.signal_class == "progress"]
        assert len(progress_signals) >= 1
        assert progress_signals[0].temporal_policy == "corroborable"
