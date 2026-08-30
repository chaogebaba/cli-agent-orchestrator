"""F581 D16 — codex rule3a_busy_marker from a byte-exact fixture corpus (AC3).

Same contract as claude_code's D12d spinner veto and the kiro leg. The codex
busy marker is the Working/Thinking spinner line with the stable "esc to
interrupt" hint (``• Working (28s • esc to interrupt)``), proven from the
byte-exact captured panes under ``test/providers/fixtures/busy_marker/codex/``
(see the sibling .json provenance; sha256 of busy-1.txt = the condition-corpus
INDEX value ``08c21768…``). grok/cline keep the BaseProvider default None (no
corpus, no override) this WP.
"""

from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    codex_busy_marker_live,
)

_FIX = Path(__file__).parent / "fixtures" / "busy_marker" / "codex"
_BUSY_FIXTURES = [_FIX / "busy-1.txt", _FIX / "busy-2.txt"]


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


# ---- AC3: WITH the Working spinner → True (the marker) --------------------


@pytest.mark.parametrize("path", _BUSY_FIXTURES, ids=lambda p: p.name)
def test_helper_true_on_live_codex_busy_fixtures(path):
    assert codex_busy_marker_live(_read(path)) is True


@pytest.mark.parametrize("path", _BUSY_FIXTURES, ids=lambda p: p.name)
def test_provider_delegates_to_helper(path):
    text = _read(path)
    assert CodexProvider.rule3a_busy_marker(None, text) is codex_busy_marker_live(text)


def test_fixtures_carry_the_expected_markers():
    """Provenance guard: each fixture actually contains the Working spinner row."""
    import re

    from cli_agent_orchestrator.utils.text import strip_terminal_escapes

    pat = re.compile(r"•.*\([^)]*\besc to interrupt\)")
    for p in _BUSY_FIXTURES:
        assert pat.search(strip_terminal_escapes(_read(p))), p.name


# ---- AC3: WITHOUT marker → False (idle composer present) ------------------


def test_helper_false_on_idle_composer_without_marker():
    assert codex_busy_marker_live("› Ask Codex to do anything") is False


def test_helper_none_on_unidentifiable_pane():
    assert codex_busy_marker_live("just some scrollback\nno prompt no marker") is None


def test_marker_is_anchored_not_bare_mention():
    """A quoted mention of "Working" in agent output without the interrupt-hint
    structure of the spinner line does not flip to True on that word alone."""
    assert codex_busy_marker_live("the CI job is Working through the queue") is None


# ---- AC3: grok / cline keep the BaseProvider default None -----------------


def test_base_default_is_none_for_non_overriders():
    assert BaseProvider.rule3a_busy_marker(None, _read(_BUSY_FIXTURES[0])) is None
    assert BaseProvider.rule3a_busy_marker(None, "anything") is None
