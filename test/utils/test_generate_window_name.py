"""AC1 tests for generate_window_name (fx155)."""

import re

import pytest

from cli_agent_orchestrator.utils.terminal import generate_window_name, validate_tmux_name


class TestGenerateWindowNameWithTerminalId:
    """Tests for generate_window_name with terminal_id (fx155 format)."""

    def test_basic_composition(self):
        """generate_window_name("kiro_dev", "61d968e8") == "kiro_dev-61d968e8"."""
        result = generate_window_name("kiro_dev", "61d968e8")
        assert result == "kiro_dev-61d968e8"

    def test_long_profile_truncated_id_intact(self):
        """A 60-char profile yields a <=64-char name whose last 9 chars are -{terminal_id}."""
        long_profile = "a" * 60
        tid = "61d968e8"
        result = generate_window_name(long_profile, tid)
        assert len(result) <= 64
        assert result.endswith(f"-{tid}")
        # Profile was truncated — the id is intact
        assert result == f"{long_profile[:64 - 1 - len(tid)]}-{tid}"

    def test_exactly_64_chars(self):
        """Profile truncation produces exactly 64 when profile is long enough."""
        # 64 - 1 - 8 = 55 chars for profile
        long_profile = "x" * 100
        tid = "abcdef01"
        result = generate_window_name(long_profile, tid)
        assert len(result) == 64
        assert result.endswith(f"-{tid}")

    def test_passes_validate_tmux_name(self):
        """Result passes validate_tmux_name."""
        result = generate_window_name("kiro_dev", "61d968e8")
        assert validate_tmux_name(result, "window_name") == result

    def test_long_profile_passes_validate_tmux_name(self):
        """Truncated result also passes validate_tmux_name."""
        result = generate_window_name("a" * 60, "61d968e8")
        assert validate_tmux_name(result, "window_name") == result


class TestGenerateWindowNameLegacyFallback:
    """Tests for generate_window_name without terminal_id (legacy format)."""

    def test_legacy_format(self):
        """generate_window_name("kiro_dev") still matches ^kiro_dev-[0-9a-f]{4}$."""
        result = generate_window_name("kiro_dev")
        assert re.fullmatch(r"kiro_dev-[0-9a-f]{4}", result)

    def test_legacy_passes_validate_tmux_name(self):
        """Legacy result passes validate_tmux_name."""
        result = generate_window_name("kiro_dev")
        assert validate_tmux_name(result, "window_name") == result


class TestGenerateWindowNameEmptyProfile:
    """Tests for empty-profile ValueError (fx155)."""

    def test_empty_profile_with_terminal_id_raises(self):
        """generate_window_name("", "61d968e8") raises ValueError."""
        with pytest.raises(ValueError):
            generate_window_name("", "61d968e8")

    def test_empty_profile_without_terminal_id_raises(self):
        """generate_window_name("") raises ValueError."""
        with pytest.raises(ValueError):
            generate_window_name("")

    def test_empty_after_truncation_raises(self):
        """If terminal_id is so long that profile truncates to empty, raise."""
        # terminal_id of length 63 leaves max_profile_len = 64 - 1 - 63 = 0
        with pytest.raises(ValueError):
            generate_window_name("a", "a" * 63)
