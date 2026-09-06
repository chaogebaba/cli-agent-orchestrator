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


# ---- F782 (#639): the SECOND activity-marker class ------------------------
# A codex tool call that draws no "• Working (… esc to interrupt)" footer still
# prints activity bullets (the wait-for-agents loop and any "• <Verb>ing …"
# line). Keyed only on the footer, the pre-F782 detector read this as idle.

from cli_agent_orchestrator.providers.codex import codex_activity_marker_live

_WAIT_AGENTS_FIXTURE = _FIX / "busy-3-wait-agents.txt"


def test_activity_marker_true_on_wait_agents_fixture():
    """#639 shape: wait-for-agents loop with NO footer, idle composer buried
    under the loop → the activity marker is live."""
    text = _read(_WAIT_AGENTS_FIXTURE)
    assert codex_activity_marker_live(text) is True
    # And the fused veto hook agrees: this is BUSY, not an idle veto.
    assert codex_busy_marker_live(text) is True


def test_wait_agents_fixture_has_no_working_footer():
    """Provenance guard: the fixture is the footer-less shape, so it exercises
    the NEW class and not the old Working-footer path."""
    import re

    from cli_agent_orchestrator.utils.text import strip_terminal_escapes

    footer = re.compile(r"•.*\([^)]*\besc to interrupt\)")
    assert not footer.search(strip_terminal_escapes(_read(_WAIT_AGENTS_FIXTURE)))


def test_activity_marker_matches_generic_verb_bullets():
    for line in (
        "• Running exact shell command with sed",
        "• Reading providers/codex.py",
        "• Editing the report",
        "• Starting script creation",
    ):
        pane = f"› do the thing\n\n{line}\n\n› Ask Codex to do anything\n\n  ~/p · main"
        assert codex_activity_marker_live(pane) is True, line


def test_true_idle_prompt_last_no_activity_is_not_busy():
    """A genuinely idle pane — the composer is the LAST non-blank row and no
    activity bullet is newer than the last submitted prompt → not BUSY."""
    idle = "› fix the bug\n\n• Done — patch applied.\n  └ ok\n\n› Ask Codex to do anything"
    assert codex_activity_marker_live(idle) is False
    assert codex_busy_marker_live(idle) is False


def test_composer_buried_under_footer_falls_through_not_false():
    """When the idle composer is present but NOT the last non-blank row (the
    path/context footer trails it) and there is no live activity, the veto no
    longer fires — it falls through to None (legacy rule 3a)."""
    pane = "• Done.\n\n› Ask Codex to do anything\n\n  ~/p · main · Context 70% left"
    assert codex_busy_marker_live(pane) is None


def test_prior_turn_activity_above_a_newer_prompt_is_not_busy():
    """Activity bullets ABOVE a newer submitted prompt are prior-turn scrollback,
    not live work."""
    pane = (
        "• Waiting for agents\n"
        "• Finished waiting\n"
        "  └ No agents completed yet\n\n"
        "› a fresh user instruction\n\n"
        "› Ask Codex to do anything\n\n  ~/p · main"
    )
    assert codex_activity_marker_live(pane) is False


def test_activity_marker_anchored_not_bare_word():
    """A "Waiting for agents" phrase inside prose (no leading • bullet) does not
    flip the activity marker."""
    pane = "› go\n\n• I am not Waiting for agents right now, just narrating.\n"
    # The bullet line starts with "• I" (matches "\w+ing"? "narrating" is at end,
    # but the anchor requires the verb-ing token as the FIRST word after •).
    assert codex_activity_marker_live(pane) is False
