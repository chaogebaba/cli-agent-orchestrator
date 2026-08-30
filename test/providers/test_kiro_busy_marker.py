"""F581 D16 — kiro rule3a_busy_marker from a byte-exact fixture corpus (AC3).

Same contract as claude_code's D12d spinner veto. The kiro busy markers are the
footer ghost text (``Kiro is working`` / ``Thinking...``) and the spinner status
line (``◐ N tasks remaining · …``), proven from the LIVE byte-exact panes under
``test/providers/fixtures/busy_marker/kiro_cli/`` (see the sibling .json
provenance). grok/cline keep the BaseProvider default None (no corpus, no
override) this WP.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.kiro_cli import (
    KiroCliProvider,
    kiro_busy_marker_live,
)

_FIX = Path(__file__).parent / "fixtures" / "busy_marker" / "kiro_cli"
_SPINNER_FIXTURES = [_FIX / "spinner-1.txt", _FIX / "spinner-2.txt"]


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


# ---- AC3: WITH spinner → True (the marker) --------------------------------


@pytest.mark.parametrize("path", _SPINNER_FIXTURES, ids=lambda p: p.name)
def test_helper_true_on_live_kiro_busy_fixtures(path):
    assert kiro_busy_marker_live(_read(path)) is True


@pytest.mark.parametrize("path", _SPINNER_FIXTURES, ids=lambda p: p.name)
def test_provider_delegates_to_helper(path):
    text = _read(path)
    assert KiroCliProvider.rule3a_busy_marker(None, text) is kiro_busy_marker_live(text)


def test_fixtures_carry_the_expected_markers():
    """Provenance guard: each fixture actually contains a busy marker row."""
    import re

    from cli_agent_orchestrator.utils.text import strip_terminal_escapes

    pat = re.compile(r"Kiro is working|Thinking\.\.\.|task(?:s)? remaining")
    for p in _SPINNER_FIXTURES:
        assert pat.search(strip_terminal_escapes(_read(p))), p.name


# ---- AC3: WITHOUT marker → False (idle prompt present) --------------------


def test_helper_false_on_idle_prompt_without_marker():
    assert kiro_busy_marker_live("Ask a question or describe a task ↵") is False


def test_helper_none_on_unidentifiable_pane():
    assert kiro_busy_marker_live("just some scrollback\nno prompt no marker") is None


def test_marker_is_anchored_not_bare_mention():
    """A quoted mention in agent output without the spinner-glyph structure of the
    tasks-remaining line does not flip to True on that line alone (the ghost-text
    phrases remain markers by contract, as in kiro get_status)."""
    # A tasks-remaining phrase WITHOUT the leading spinner glyph is not the
    # status-line marker.
    assert kiro_busy_marker_live("the plan has 3 tasks remaining to do") is None


# ---- AC3: grok / cline keep the BaseProvider default None -----------------


def test_base_default_is_none_for_non_overriders():
    assert BaseProvider.rule3a_busy_marker(None, _read(_SPINNER_FIXTURES[0])) is None
    assert BaseProvider.rule3a_busy_marker(None, "anything") is None
