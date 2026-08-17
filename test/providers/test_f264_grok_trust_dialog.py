"""F264: Unit tests for grok trust-directory dialog status detection.

Verifies that the first-run trust dialog footer ("Enter or y to trust")
is classified as WAITING_USER_ANSWER only when it appears in the bottom
viewport rows (WAITING_VIEWPORT_ROWS=5), and is NOT classified when quoted
in mid-scrollback prose with an idle composer below.
"""

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider


def _provider() -> GrokCliProvider:
    return GrokCliProvider(
        terminal_id="term-grok-f264",
        session_name="session",
        window_name="window",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )


# --- Synthetic screen: real trust dialog (question ~10 rows up, footer bottom) ---

TRUST_DIALOG_SCREEN = [
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "  Do you trust the contents of this directory?",
    "",
    "  Grok reads files and runs commands in the project",
    "  directory. Only trust directories with content you",
    "  understand and trust.",
    "",
    "  Directory: /home/user/project",
    "",
    "",
    "",
    "  Enter or y to trust · n or Esc to quit",
]


def test_trust_dialog_footer_classifies_waiting_user_answer():
    """Trust dialog footer in bottom viewport -> WAITING_USER_ANSWER."""
    provider = _provider()
    status = provider.get_status_from_screen(TRUST_DIALOG_SCREEN)
    assert status == TerminalStatus.WAITING_USER_ANSWER, (
        f"Expected WAITING_USER_ANSWER, got {status}"
    )


def test_trust_dialog_emits_waiting_signal():
    """emit_screen_signals produces a waiting signal for the trust dialog."""
    provider = _provider()
    signals = provider.emit_screen_signals(TRUST_DIALOG_SCREEN)
    waiting_signals = [s for s in signals if s.signal_class == "waiting"]
    assert len(waiting_signals) == 1, f"Expected 1 waiting signal, got {waiting_signals}"


# --- Negative: footer text quoted in mid-scrollback with idle composer below ---

QUOTED_FOOTER_SCREEN = [
    "  The user reported seeing the following dialog:",
    "  Enter or y to trust · n or Esc to quit",
    "  This was resolved by auto-answering.",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "   always-approve   ctrl+o transcript   Shift+Tab:mode   Ctrl+x:shortcuts",
    "",
    "   ❯",
    "",
    "",
]


def test_quoted_footer_in_scrollback_does_not_classify_waiting():
    """Footer text in mid-scrollback prose with idle composer -> NOT WAITING_USER_ANSWER."""
    provider = _provider()
    status = provider.get_status_from_screen(QUOTED_FOOTER_SCREEN)
    assert status != TerminalStatus.WAITING_USER_ANSWER, (
        f"Expected NOT WAITING_USER_ANSWER but got {status} (false positive on quoted prose)"
    )


def test_quoted_footer_no_waiting_signal():
    """Footer text high in scrollback should NOT emit waiting signal."""
    provider = _provider()
    signals = provider.emit_screen_signals(QUOTED_FOOTER_SCREEN)
    waiting_signals = [s for s in signals if s.signal_class == "waiting"]
    assert len(waiting_signals) == 0, (
        f"Expected 0 waiting signals for quoted prose, got {waiting_signals}"
    )
