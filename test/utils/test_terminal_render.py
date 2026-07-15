"""Tests for the shared ANSI composition primitive."""

import pytest

from cli_agent_orchestrator.utils.terminal_render import compose_ansi_to_lines


def test_compose_ansi_to_lines_reassembles_cursor_positioned_focus():
    raw = (
        "Choose targets\r\n"
        "  1. Alpha\r\n"
        "  2. Beta\r\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\r\n"
        "\x1b[2;1H❯"
    )

    rendered = "\n".join(compose_ansi_to_lines(raw, 80, 10))

    assert "❯ 1. Alpha" in rendered
    assert "2. Beta" in rendered


@pytest.mark.parametrize("geometry", [(0, 10), (80, 0), (-1, 10), (80, -1), (True, 10)])
def test_compose_ansi_to_lines_rejects_invalid_geometry(geometry):
    with pytest.raises(ValueError, match="positive integer pair"):
        compose_ansi_to_lines("hello", *geometry)
