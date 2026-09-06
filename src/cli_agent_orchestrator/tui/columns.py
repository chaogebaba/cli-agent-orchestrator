"""F702 (#557) D3: the fleet table's column names, as constants.

Two disjoint groups:

* :data:`PARITY_COLUMNS` — the default-visible columns (AC5). The first six are
  the retiring stdlib script's headers (root repo ``scripts/fleet-tui.py:344``),
  in its order, except ``ELAPSED`` was renamed from the script's ``IDLE`` (see
  :data:`ELAPSED_COLUMN`). F777 (#634) inserted three more default-visible
  columns — ``PROVIDER``, ``MODEL``, ``EFFORT`` — between ``PROFILE`` and
  ``TASK``; ``MODEL`` moved here out of :data:`NEW_COLUMNS`.
* :data:`NEW_COLUMNS` — the remaining keys ``build_fleet()`` publishes that are
  hidden by default and toggled with ``c`` (blueprint B7, minus ``MODEL``).

Names only. Widths, styles, and the ``DataTable`` wiring belong to J2's app
module; this module has no Textual dependency so it stays importable anywhere.
"""

from __future__ import annotations

from typing import Final, Tuple

#: The last parity column, renamed from the script's ``IDLE`` (F702 #557
#: "elapsed" round, user request 2026-09-03).
#:
#: The script's column was seconds since the pane last produced output, under a
#: header that asserted the seat was idle. Both halves misread a busy worker: it
#: was headed ``IDLE`` whatever the row's status, so a worker six hours into a
#: task read as "idle 6h". The column now carries time in the CURRENT status —
#: working, idle, errored or completed alike — so the header has to be
#: status-neutral. ``ELAPSED`` says what the number is without claiming what the
#: seat is doing; the STATUS column one place left already says that.
#:
#: Position is unchanged: still the sixth and last parity column, still the one
#: stretched to the screen edge, so no other column moves.
ELAPSED_COLUMN: Final[str] = "ELAPSED"

#: The parity headers, in render order (F702 #557 order, extended by F777 #634).
#:
#: F777 (#634) makes PROVIDER, MODEL and EFFORT default-visible: the operator
#: sees each seat's CLI, model and thinking effort without pressing ``c``. They
#: sit between PROFILE and TASK so the seat-identity columns group together, and
#: ELAPSED stays the last (screen-edge-stretched) column. MODEL moved here from
#: :data:`NEW_COLUMNS` — it is no longer behind the toggle.
PARITY_COLUMNS: Final[Tuple[str, ...]] = (
    "WIN",
    "ID",
    "PROFILE",
    "PROVIDER",
    "MODEL",
    "EFFORT",
    "TASK",
    "STATUS",
    ELAPSED_COLUMN,
)

#: The four remaining new columns (blueprint D3 minus MODEL, which F777 promoted
#: to the default parity set): ``condition``, ``delegating``/``children_count``,
#: ``fusion_changed``, ``lifecycle``. Hidden by default, toggled with ``c``.
NEW_COLUMNS: Final[Tuple[str, ...]] = (
    "COND",
    "DELEG",
    "*",
    "LIFE",
)

#: Parity first, then the new columns — the order used when ``c`` reveals them.
ALL_COLUMNS: Final[Tuple[str, ...]] = PARITY_COLUMNS + NEW_COLUMNS

#: The header-less selection gutter, drawn left of ``WIN``.
#:
#: The retiring script reserves exactly two leading columns for this and writes
#: ``"▶ "`` there on the selected row (``fleet-tui.py:374,399``). A ``DataTable``
#: cannot prefix a row, so the gutter is a real column with an empty header —
#: the same two visible cells, and the six parity headers stay verbatim (AC5).
SELECTION_COLUMN: Final[str] = ""
#: What the gutter holds on the selected row / on every other row.
MARKER_SELECTED: Final[str] = "▶"
MARKER_BLANK: Final[str] = " "

#: The column order the table actually installs, gutter included.
PARITY_VIEW: Final[Tuple[str, ...]] = (SELECTION_COLUMN,) + PARITY_COLUMNS
ALL_VIEW: Final[Tuple[str, ...]] = (SELECTION_COLUMN,) + ALL_COLUMNS
#: Index of the gutter in either view.
MARKER_INDEX: Final[int] = 0

#: The parity column that carries :func:`~cli_agent_orchestrator.tui.status_cell.status_cell`.
STATUS_COLUMN: Final[str] = "STATUS"

#: The parity column used as the ``DataTable`` row key (jump flashes it).
KEY_COLUMN: Final[str] = "ID"
