from pathlib import Path

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


FIXTURES = Path(__file__).parents[1] / "fixtures" / "fx2"


def _provider():
    return ClaudeCodeProvider("term", "session", "window")


def test_stashed_chip_and_dim_placeholder_fixtures():
    provider = _provider()
    stashed = (FIXTURES / "stashed-chip-sgr.txt").read_text().splitlines()
    placeholder = (FIXTURES / "dim-placeholder-sgr.txt").read_text().splitlines()
    assert provider.composer_stashed_chip_pattern.search("\n".join(stashed))
    assert provider.read_composer_draft(stashed) == ""
    assert provider.read_composer_draft(placeholder) == ""


def test_multiline_fixture_preserves_line_order():
    draft = _provider().read_composer_draft(
        (FIXTURES / "multiline-draft-sgr.txt").read_text().splitlines()
    )
    assert draft == "FX2_MULTI_line1\nFX2_MULTI_line2"


def test_composer_accepts_more_than_four_continuation_rows():
    lines = ["─" * 20, "❯ first", "  second", "  third", "  fourth", "  fifth", "  sixth", "─" * 20]
    assert _provider().read_composer_draft(lines) == "first\nsecond\nthird\nfourth\nfifth\nsixth"
