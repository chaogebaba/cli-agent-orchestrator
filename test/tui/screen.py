"""Read a running Textual app's composited screen back as plain text.

The pilot tests assert widget state; a few things can only be asserted about
the *frame* — that one captured line occupies one row, that the sections land
in the script's order with the right blank lines between them. Textual's own
``export_screenshot`` answers in SVG, which is neither readable in a diff nor
stable across a theme change, so those tests read the compositor's strips
instead: exactly what a terminal would have received, one string per row.
"""

from __future__ import annotations

import re
from typing import Any, List

#: Any ``HH:MM:SS``. The header carries a wall clock (``fleet-tui.py:338``),
#: which a golden file cannot contain — the app formats it in local time, so it
#: differs by timezone as well as by when the test runs.
CLOCK_PATTERN = re.compile(r"\d{2}:\d{2}:\d{2}")
CLOCK_PLACEHOLDER = "HH:MM:SS"


def screen_lines(app: Any) -> List[str]:
    """The whole screen as one string per row, right-trimmed.

    Trailing spaces are the compositor padding every row to the screen width;
    they carry nothing and would make a golden file diff on an unrelated width
    change.
    """
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def normalise(lines: List[str]) -> List[str]:
    """Replace the wall clock so a frame can be compared to a golden file."""
    return [CLOCK_PATTERN.sub(CLOCK_PLACEHOLDER, line) for line in lines]
