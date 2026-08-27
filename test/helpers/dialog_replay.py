"""F516 commit 1: deterministic multi-frame dialog replay harness.

Feeds per-incident raw-escape frame files through the REAL pyte compositor
(``compose_ansi_to_lines`` — the same path status_monitor uses to build its
``_screens``) and advances an injectable fake clock by each frame's
monotonic-clock offset. This is the offline replay path the D4 backoff ACs
(AC1/AC3/AC5) drive: no wall-clock time, no event loop, deterministic.

Frame format (blueprint §4 commit 1, r2-B9):
    test/fixtures/codex_dialogs/<incident>-frame-NN.ansi.txt   raw escapes
    test/fixtures/codex_dialogs/<incident>-manifest.json       per-frame offsets

The manifest is::

    {
      "incident": "<id>",
      "cols": 220,
      "rows": 50,
      "frames": [
        {"file": "<incident>-frame-00.ansi.txt", "offset_s": 0.0},
        {"file": "<incident>-frame-01.ansi.txt", "offset_s": 0.2},
        ...
      ]
    }

``offset_s`` is the monotonic offset (seconds) at which the frame becomes
visible, relative to the first frame. Frames are cumulative raw byte streams
(each is the full pane byte history up to that point), so composing frame NN
yields the pane exactly as it looked at ``offset_s``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

from cli_agent_orchestrator.utils.terminal_render import compose_ansi_to_lines

CODEX_DIALOGS = Path(__file__).parents[1] / "fixtures" / "codex_dialogs"


@dataclass(frozen=True)
class ReplayFrame:
    offset_s: float
    rows: List[str]
    raw: str


class DialogReplay:
    """Load a frozen incident frame-sequence and its clock offsets."""

    def __init__(self, incident: str, fixtures_dir: Path | None = None):
        base = fixtures_dir or CODEX_DIALOGS
        manifest_path = base / f"{incident}-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.incident = manifest["incident"]
        self.cols = int(manifest["cols"])
        self.rows_geom = int(manifest["rows"])
        self._frames: List[ReplayFrame] = []
        for entry in manifest["frames"]:
            raw = (base / entry["file"]).read_text(encoding="utf-8")
            composed = compose_ansi_to_lines(raw, self.cols, self.rows_geom)
            self._frames.append(
                ReplayFrame(offset_s=float(entry["offset_s"]), rows=composed, raw=raw)
            )

    @property
    def frames(self) -> List[ReplayFrame]:
        return list(self._frames)

    def final_rows(self) -> List[str]:
        """Composited rows of the last frame — the settled dialog state."""
        return self._frames[-1].rows

    def rows_at(self, index: int) -> List[str]:
        return self._frames[index].rows

    def play(self, clock, sink) -> None:
        """Advance ``clock`` to each frame offset and hand its rows to ``sink``.

        ``clock`` is a test.helpers.fake_clock.FakeClock; ``sink`` is a callable
        ``(rows: list[str]) -> None`` invoked once per frame at that frame's
        monotonic offset. The clock is advanced by the delta between frames so a
        consumer reading ``clock.monotonic()`` sees each frame's true offset.
        """
        prev = 0.0
        base = clock.monotonic()
        for frame in self._frames:
            delta = frame.offset_s - prev
            if delta > 0:
                clock.advance(delta)
            prev = frame.offset_s
            sink(frame.rows)
        # Anchor the base so callers can compute absolute offsets if needed.
        _ = base
