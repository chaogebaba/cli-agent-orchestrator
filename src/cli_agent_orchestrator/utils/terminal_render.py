"""Pure helpers for compositing terminal output streams."""

from __future__ import annotations


def compose_ansi_to_lines(buf: str, cols: int, rows: int) -> list[str]:
    """Compose an ANSI terminal stream into its rendered viewport rows."""
    if (
        isinstance(cols, bool)
        or isinstance(rows, bool)
        or not isinstance(cols, int)
        or not isinstance(rows, int)
        or cols <= 0
        or rows <= 0
    ):
        raise ValueError("terminal geometry must be a positive integer pair")

    import pyte

    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed(buf)
    return list(screen.display)
