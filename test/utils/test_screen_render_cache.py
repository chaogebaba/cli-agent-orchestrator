"""ScreenRenderCache must be byte-identical to ``list(pyte.Screen.display)``.

Property-style: feed random ANSI streams (draw, cursor moves, erase, scroll,
insert/delete lines, wide chars, resize, reset) through a pyte screen and
assert the incremental cache renders exactly what a full ``display`` renders,
after every chunk. Then the cheap-path guarantees: dirty rows only, identity
change and row-count change force a full render, test doubles pass through.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Callable

import pyte
import pytest

from cli_agent_orchestrator.utils.terminal_render import ScreenRenderCache

_WORDS = [
    "ok",
    "─",
    "│",
    "▌",
    "✻",
    "…",
    "esc to interrupt",
    "中文",
    "日本語テキスト",
    "> ",
    "  ",
    # Combining marks (zero-width): pyte attaches them to the PREVIOUS cell, and
    # at column 0 to the last cell of the PREVIOUS ROW without a dirty mark —
    # gate r2 blocker 1. Keep them in the corpus so the fuzz can reach that path.
    "\u0301",
    "e\u0301",
    "\u0308\u0301",
    "a\u0300",
    "\u20de",
    "😀",
    "👍🏽",
]
_SEQS = [
    "\x1b[H",
    "\x1b[2J",
    "\x1b[K",
    "\x1b[1K",
    "\x1b[2K",
    "\x1b[J",
    "\x1b[1J",
    "\x1b[A",
    "\x1b[B",
    "\x1b[C",
    "\x1b[D",
    "\x1b[3L",
    "\x1b[2M",
    "\x1b[2P",
    "\x1b[3@",
    "\x1b[2X",
    "\x1b[?25l",
    "\x1b[?25h",
    "\x1b[1;31m",
    "\x1b[0m",
    "\x1b[7m",
    "\x1b[s",
    "\x1b[u",
    "\x1b7",
    "\x1b8",
    "\x1bM",
    "\r",
    "\n",
    "\r\n",
    "\x1b[?1049h",
    "\x1b[?1049l",
    "\x1b[2;10r",
    "\x1b[r",
    "\x1bc",
]


def _chunk(rng: random.Random, cols: int, rows: int) -> str:
    parts = []
    for _ in range(rng.randint(1, 12)):
        pick = rng.random()
        if pick < 0.45:
            parts.append(rng.choice(_WORDS) * rng.randint(1, 4))
        elif pick < 0.85:
            parts.append(rng.choice(_SEQS))
        else:
            parts.append(f"\x1b[{rng.randint(1, rows)};{rng.randint(1, cols)}H")
    return "".join(parts)


def _render(fn: "Callable[[], list[str]]") -> object:
    """Rows, or the exception type: pyte's own ``display`` raises IndexError on an
    orphaned wide-char stub (the "zero-length cell" case status_monitor already
    falls back on), and the cache must fail the same way, not paper over it."""
    try:
        return fn()
    except IndexError as exc:
        return type(exc)


@pytest.mark.parametrize("seed", list(range(40)))
def test_incremental_matches_full_display(seed: int) -> None:
    rng = random.Random(seed)
    cols, rows = rng.choice([(40, 8), (80, 24), (120, 30)])
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    cache = ScreenRenderCache()
    for step in range(120):
        stream.feed(_chunk(rng, cols, rows))
        if step % 37 == 36:
            screen.resize(rng.choice([rows, rows + 3, max(3, rows - 2)]), cols)
        got = _render(lambda: cache.rows(screen))
        want = _render(lambda: list(screen.display))
        assert got == want, f"seed={seed} step={step}"
        # A second call with nothing fed must be stable and cheap.
        assert _render(lambda: cache.rows(screen)) == want


def test_combining_char_at_column_zero_updates_previous_row() -> None:
    """Gate r2 blocker 1, deterministic: a combining mark drawn at column 0 lands on the
    last cell of the row above; pyte marks only the cursor row dirty."""
    screen = pyte.Screen(5, 3)
    stream = pyte.Stream(screen)
    cache = ScreenRenderCache()
    stream.feed("abc\r\n")
    assert cache.rows(screen) == list(screen.display)
    stream.feed("\u0301")
    assert sorted(screen.dirty) == [1]  # pyte's dirty set does not include row 0
    got = cache.rows(screen)
    assert got == list(screen.display)
    assert got[0].endswith("\u0301")
    # and it stays correct on the next (no-op) read and after more drawing
    assert cache.rows(screen) == list(screen.display)
    stream.feed("\u0301zz")
    assert cache.rows(screen) == list(screen.display)


def test_combining_char_at_column_zero_of_row_zero_is_harmless() -> None:
    screen = pyte.Screen(4, 2)
    stream = pyte.Stream(screen)
    cache = ScreenRenderCache()
    cache.rows(screen)
    stream.feed("\u0301")
    assert cache.rows(screen) == list(screen.display)


def test_incremental_branch_returns_a_copy() -> None:
    """G2 from the gate: the copy semantics of the INCREMENTAL branch, not just the full render."""
    screen = pyte.Screen(10, 3)
    stream = pyte.Stream(screen)
    cache = ScreenRenderCache()
    stream.feed("a\r\nb\r\nc")
    cache.rows(screen)  # full-render branch
    stream.feed("\x1b[2;1HB")  # one dirty row -> incremental branch
    got = cache.rows(screen)
    assert got[1].startswith("B")
    got[1] = "tampered"  # same length: a size change would mask the bug via the full-render path
    again = cache.rows(screen)
    assert again[1].startswith("B")
    assert again == list(screen.display)
    # and the copy handed out is not the copy handed out next time either
    assert again is not cache.rows(screen)


def test_only_dirty_rows_are_rerendered(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = pyte.Screen(20, 5)
    stream = pyte.Stream(screen)
    cache = ScreenRenderCache()
    stream.feed("\x1b[Hrow0\r\nrow1\r\nrow2\r\nrow3\r\nrow4")
    first = cache.rows(screen)
    assert first[0].startswith("row0")
    assert screen.dirty == set()

    rendered_rows: list[int] = []
    real = ScreenRenderCache._render_row

    def spy(line: object, columns: int, wcwidth: object) -> str:
        rendered_rows.append(id(line))
        return real(line, columns, wcwidth)

    monkeypatch.setattr(ScreenRenderCache, "_render_row", staticmethod(spy))
    stream.feed("\x1b[3;1HCHANGED")  # only row index 2
    got = cache.rows(screen)
    assert got[2].startswith("CHANGED")
    assert got[0].startswith("row0") and got[4].startswith("row4")
    # the dirty row and its neighbour above (the only cell pyte can touch unmarked)
    assert sorted(rendered_rows) == sorted([id(screen.buffer[1]), id(screen.buffer[2])])
    assert got == list(screen.display)


def test_returns_copies_not_the_cache() -> None:
    screen = pyte.Screen(10, 2)
    pyte.Stream(screen).feed("ab")
    cache = ScreenRenderCache()
    a = cache.rows(screen)
    a[0] = "tampered"
    assert cache.rows(screen)[0].startswith("ab")


def test_screen_identity_change_forces_full_render() -> None:
    cache = ScreenRenderCache()
    s1 = pyte.Screen(10, 2)
    pyte.Stream(s1).feed("one")
    assert cache.rows(s1)[0].startswith("one")
    s2 = pyte.Screen(10, 2)
    pyte.Stream(s2).feed("two")
    s2.dirty.clear()  # even with nothing marked dirty, a new screen renders fully
    assert cache.rows(s2)[0].startswith("two")


def test_row_count_change_forces_full_render() -> None:
    cache = ScreenRenderCache()
    screen = pyte.Screen(10, 3)
    stream = pyte.Stream(screen)
    stream.feed("a\nb\nc")
    cache.rows(screen)
    screen.resize(5, 10)
    screen.dirty.clear()  # simulate a missed dirty mark: the size check must still catch it
    got = cache.rows(screen)
    assert len(got) == 5
    assert got == list(screen.display)


def test_wide_chars_render_like_pyte() -> None:
    cache = ScreenRenderCache()
    screen = pyte.Screen(12, 2)
    stream = pyte.Stream(screen)
    stream.feed("中文ab")
    assert cache.rows(screen) == list(screen.display)
    stream.feed("\x1b[1;2Hx")  # overwrite inside the wide char
    assert cache.rows(screen) == list(screen.display)


def test_test_double_without_dirty_passes_through() -> None:
    cache = ScreenRenderCache()
    fake = SimpleNamespace(display=["r1", "r2"], columns=10, lines=2)
    assert cache.rows(fake) == ["r1", "r2"]
    fake.display = ["changed", "r2"]
    assert cache.rows(fake) == ["changed", "r2"]


def test_render_error_leaves_dirty_for_retry() -> None:
    """pyte can hold a zero-length cell transiently; the cache must not swallow the error
    nor lose the dirty mark (the caller falls back to the raw buffer and retries later)."""
    cache = ScreenRenderCache()
    screen = pyte.Screen(6, 2)
    pyte.Stream(screen).feed("abc")
    cache.rows(screen)
    screen.buffer[1][0] = screen.buffer[1][0]._replace(data="")  # orphan stub
    screen.dirty.add(1)
    with pytest.raises(IndexError):
        cache.rows(screen)
    assert 1 in screen.dirty
    screen.buffer[1][0] = screen.buffer[1][0]._replace(data="z")
    assert cache.rows(screen)[1].startswith("z")
