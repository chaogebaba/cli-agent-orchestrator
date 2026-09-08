"""F702 (#557): a screenshot-equivalent snapshot of the `cao-fleet` main screen.

The pilot tests in :mod:`test.tui.test_fleet_app` each assert one widget. None
of them can see the *frame*: which section sits above which, how many blank
lines separate them, whether a column lines up with its header, whether the
peek's banner and rule land where the retiring script drew them. That is the
whole "the Textual TUI looks wrong" class of regression, and it is what this
file catches — the composited screen, row by row, against a committed golden.

It is a screenshot in the only form worth diffing: text. Textual's own
``export_screenshot`` produces SVG, which is unreadable in review and changes
whenever a theme colour does. :func:`test.tui.screen.screen_lines` reads the
compositor's strips instead — exactly the rows a terminal would receive.

**Regenerating.** After a deliberate layout change::

    CAO_FLEET_SNAPSHOT_UPDATE=1 uv run pytest test/tui/test_snapshot.py

then read the diff on ``fixtures/main_screen.txt`` before committing it: an
unexplained line moving is the bug this file exists to show you.

Everything behind the frame is frozen — a fixed screen size, the ``healthy``
payload, a fixed labels file and events log, a stopped clock and a canned tmux
— so the only wall-clock value on screen is the header's ``HH:MM:SS``, which
:func:`test.tui.screen.normalise` masks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from .screen import normalise, screen_lines

# The fakes and the frozen-clock app factory are the pilot suite's; this file
# adds a different *assertion* over the same rig, not a second rig.
from .test_fleet_app import FakeTmux, load_payload, make_app, settle

GOLDEN = Path(__file__).parent / "fixtures" / "main_screen.txt"

#: Wide enough that no parity column is squeezed, tall enough that every
#: section — table, recent, hints and the peek under them — is on screen.
#: F777 (#634) widened the default column set (PROVIDER/MODEL/EFFORT added
#: between PROFILE and TASK), so the width was raised from 100 to 130 to keep
#: the full row — through ELAPSED — on one line, which is what
#: ``test_the_golden_frame_carries_every_section_in_the_scripts_order`` checks.
SCREEN_SIZE = (130, 34)

LABELS = "term-0001\tsupervisor seat\nterm-0002\twp-arch phase 3 build\n"
EVENTS = "12:00:01 term-0002 assigned\n12:00:09 term-0003 completed\n"

UPDATE_ENV = "CAO_FLEET_SNAPSHOT_UPDATE"


def read_golden() -> List[str]:
    return GOLDEN.read_text().splitlines()


def write_golden(lines: List[str]) -> None:
    GOLDEN.write_text("\n".join(lines) + "\n")


@pytest.mark.asyncio
async def test_the_main_screen_matches_its_golden_frame(tmp_path: Path) -> None:
    app, feed, _ = make_app(
        [load_payload("healthy")],
        tmp_path,
        tmux=FakeTmux(activity={"0": 990, "2": 999, "3": 999}),
        labels=LABELS,
        events=EVENTS,
    )
    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await settle(pilot, feed)
        await pilot.pause()
        frame = normalise(screen_lines(app))

    if os.environ.get(UPDATE_ENV):
        write_golden(frame)

    expected = read_golden()
    assert frame == expected, (
        "the main screen changed.\n"
        f"  regenerate with {UPDATE_ENV}=1 uv run pytest {Path(__file__).name}\n"
        "  then read the diff on fixtures/main_screen.txt before committing it"
    )


@pytest.mark.asyncio
async def test_the_golden_frame_carries_every_section_in_the_scripts_order(
    tmp_path: Path,
) -> None:
    """The golden is only useful if it actually shows the sections.

    A frame that silently lost its peek would still match a golden regenerated
    from the same bug; this pins what the golden has to contain.
    """
    frame = read_golden()
    marks = [index for index, line in enumerate(frame) if line.startswith("▌")]
    titles = [frame[index] for index in marks]
    assert titles[0].startswith("▌ CAO fleet · ")
    assert "▌ recent" in titles
    assert any(title.startswith("▌ peek · ") for title in titles)
    # header first, peek last — the script's order (fleet-tui.py:336-450)
    assert marks == sorted(marks)
    assert titles[-1].startswith("▌ peek · ")
    # the parity headers, on one line, above the rows. F777 (#634) inserted
    # PROVIDER/MODEL/EFFORT between PROFILE and TASK.
    head = next(line for line in frame if line.lstrip().startswith("WIN"))
    for column in (
        "WIN",
        "ID",
        "PROFILE",
        "PROVIDER",
        "MODEL",
        "EFFORT",
        "TASK",
        "STATUS",
        "ELAPSED",
    ):
        assert column in head
    assert frame.index(head) < marks[1]
    # the selection gutter marks exactly one row
    assert sum(1 for line in frame if line.startswith("▶")) == 1


@pytest.mark.asyncio
async def test_the_frame_renders_literal_d1_marker_brackets(tmp_path: Path) -> None:
    """F826 (#683) B3 (r1): the D1/D6 marker brackets must render LITERALLY.

    ``Static.update`` on a plain str runs the text through Rich console markup,
    which consumes ``[L]``/``[R]``/``[S]``/``[C]``/``[?]`` as markup tags and
    strips them — the exact regression the r1 gate caught. This asserts, over the
    COMPOSITED frame (not a widget in isolation), that the legend, the selected-
    row detail, and the MODEL/EFFORT cells all carry their literal brackets.
    """
    app, feed, _ = make_app(
        [load_payload("healthy")],
        tmp_path,
        tmux=FakeTmux(activity={"0": 990, "2": 999, "3": 999}),
        labels=LABELS,
        events=EVENTS,
    )
    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await settle(pilot, feed)
        await pilot.pause()
        frame = normalise(screen_lines(app))
    blob = "\n".join(frame)
    # The legend keeps every literal marker bracket.
    assert "[L]ive" in blob
    assert "[R]ecorded" in blob
    assert "[S]tale" in blob
    assert "[C]onfigured" in blob
    assert "[?]unknown" in blob
    assert "!=conflict" in blob
    # The MODEL/EFFORT cells and the selected-row detail keep their [C]/[?].
    assert "claude-opus-5 [C]" in blob
    assert "- [?]" in blob
    assert "model: claude-opus-5 [C]" in blob
