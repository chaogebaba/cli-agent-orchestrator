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


class ScreenRenderCache:
    """Incremental ``pyte.Screen.display`` (perf side lane, 2026-09-02).

    ``Screen.display`` re-renders every cell of every row (``wcwidth`` per
    character, ~10k cells for a 200x50 pane) on each call. The status monitor
    renders on every rising edge and every quiescence, and an idle Claude TUI
    repaints a row or two several times a second, so rendering dominated the
    server's Python CPU (pyte ``render``/``display`` frames were ~24% of
    GIL-holding samples measured live on 2026-09-02).

    pyte maintains ``Screen.dirty`` — the set of row indexes touched since the
    caller last cleared it — for exactly this purpose. The cache keeps the last
    rendered rows and re-renders only the dirty ones, using pyte's own row
    algorithm (wide-char stubs skipped), then clears ``dirty``.

    Safety: any screen that does not expose ``dirty``/``lines``/``columns``/
    ``buffer`` (test doubles) is rendered via ``display`` unchanged; a screen
    identity change or a row-count change (resize) forces a full render.
    Callers must hold whatever lock guards ``feed`` — ``dirty`` is not
    thread-safe.
    """

    def __init__(self) -> None:
        self._screen: object | None = None
        self._rows: list[str] = []

    @staticmethod
    def _render_row(line: object, columns: int, wcwidth: object) -> str:
        # Verbatim port of pyte.screens.Screen.display's inner ``render``
        # (0.8.2), minus its debug ``assert``.
        chars: list[str] = []
        is_wide_char = False
        for x in range(columns):
            if is_wide_char:  # skip the stub cell after a wide char
                is_wide_char = False
                continue
            char = line[x].data  # type: ignore[index]
            is_wide_char = wcwidth(char[0]) == 2  # type: ignore[operator]
            chars.append(char)
        return "".join(chars)

    def rows(self, screen: object) -> list[str]:
        """Return the screen's rows as ``list(screen.display)`` would, re-rendering only dirty rows."""
        dirty = getattr(screen, "dirty", None)
        lines = getattr(screen, "lines", None)
        columns = getattr(screen, "columns", None)
        buffer = getattr(screen, "buffer", None)
        if (
            not isinstance(dirty, set)
            or not isinstance(lines, int)
            or not isinstance(columns, int)
            or buffer is None
        ):
            return list(screen.display)  # type: ignore[attr-defined]

        if self._screen is not screen or len(self._rows) != lines:
            rows = list(screen.display)  # type: ignore[attr-defined]
            dirty.clear()
            self._screen = screen
            self._rows = rows
            return list(rows)

        if dirty:
            # pyte marks only ``cursor.y`` dirty in ``draw``, but its zero-width
            # combining-character branch at column 0 mutates the LAST cell of the
            # PRECEDING row (``buffer[cursor.y - 1][columns - 1]``) without marking
            # it (gate r2 blocker 1). That is the only cross-row write pyte makes
            # without a dirty mark, so re-render the row above every dirty row too.
            stale = dirty | {y - 1 for y in dirty if y > 0}
            if len(stale) >= lines:
                self._rows = list(screen.display)  # type: ignore[attr-defined]
            else:
                import pyte.screens

                wcwidth = pyte.screens.wcwidth
                for y in stale:
                    if 0 <= y < lines:
                        self._rows[y] = self._render_row(buffer[y], columns, wcwidth)
            dirty.clear()
        return list(self._rows)
