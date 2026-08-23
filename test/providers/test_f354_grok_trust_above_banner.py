"""F354: Regression test for grok trust dialog rendered ABOVE an ASCII banner.

Incident geometry (pane 134x49):
  - Question "Do you trust..." at row 32  (17 rows from bottom)
  - Options (y/n) at rows 38-39
  - Footer "Enter or y to trust" at row 41  (8 rows from bottom)
  - ASCII banner occupies rows 42-48

With WAITING_VIEWPORT_ROWS=10 (≥8), the footer at row 41 is inside the
viewport window and emits a waiting signal → WAITING_USER_ANSWER.
With DIALOG_REGION_LINES=20 (≥17), the question at row 32 is inside the
dialog region → rule matches on the region.
"""

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider, WAITING_VIEWPORT_ROWS
from cli_agent_orchestrator.services import auto_responder as ar


def _provider() -> GrokCliProvider:
    return GrokCliProvider(
        terminal_id="term-grok-f354",
        session_name="session",
        window_name="window",
        agent_profile="grok_dev",
        allowed_tools=["*"],
    )


# Synthetic 49-row screen reproducing F354 geometry:
# Rows 0-31: scrollback / empty
# Row 32: question
# Rows 33-37: description lines
# Rows 38-39: options
# Row 40: blank
# Row 41: footer (Enter or y to trust)
# Rows 42-48: ASCII banner (7 rows)

F354_SCREEN = (
    [""] * 32  # rows 0-31: empty scrollback
    + [
        "  Do you trust the contents of this directory?",  # row 32
        "",  # row 33
        "  Directory: /home/user/project",  # row 34
        "",  # row 35
        "  Grok reads files and runs commands in the project",  # row 36
        "  directory. Only trust directories you understand.",  # row 37
        "  y  Yes, proceed",  # row 38
        "  n  No, quit",  # row 39
        "",  # row 40
        "  Enter or y to trust · n or Esc to quit",  # row 41
        "   ╔══════════════════════════════════════════╗",  # row 42
        "   ║        Welcome to Grok CLI v3.1         ║",  # row 43
        "   ║   AI-powered development assistant      ║",  # row 44
        "   ║                                         ║",  # row 45
        "   ║   Type /help to get started             ║",  # row 46
        "   ╚══════════════════════════════════════════╝",  # row 47
        "",  # row 48
    ]
)

assert len(F354_SCREEN) == 49, f"Screen must be 49 rows, got {len(F354_SCREEN)}"


def test_f354_footer_in_viewport_classifies_waiting():
    """F354: trust footer 8 rows from bottom is inside WAITING_VIEWPORT_ROWS=10."""
    provider = _provider()
    status = provider.get_status_from_screen(F354_SCREEN)
    assert status == TerminalStatus.WAITING_USER_ANSWER, (
        f"Expected WAITING_USER_ANSWER, got {status}"
    )


def test_f354_emits_waiting_signal():
    """F354: emit_screen_signals produces a waiting signal for row 41."""
    provider = _provider()
    signals = provider.emit_screen_signals(F354_SCREEN)
    waiting_signals = [s for s in signals if s.signal_class == "waiting"]
    assert len(waiting_signals) == 1, f"Expected 1 waiting signal, got {waiting_signals}"
    # The footer is at row index 41
    assert waiting_signals[0].row_index == 41


def test_f354_dialog_region_includes_question():
    """F354: dialog_region with DIALOG_REGION_LINES=20 includes the question at row 32."""
    region = ar.dialog_region(F354_SCREEN)
    # The question should be inside the region (17 rows from bottom ≤ 20)
    assert "do you trust" in region.normalized.lower(), (
        f"Question not found in dialog region: {region.normalized[:80]}..."
    )


def test_f354_viewport_rows_geometry_requirement():
    """Verify the WAITING_VIEWPORT_ROWS constant satisfies F354 geometry (≥8)."""
    assert WAITING_VIEWPORT_ROWS >= 8, (
        f"WAITING_VIEWPORT_ROWS={WAITING_VIEWPORT_ROWS} too small for F354 (footer at row 41 in 49-row pane)"
    )
