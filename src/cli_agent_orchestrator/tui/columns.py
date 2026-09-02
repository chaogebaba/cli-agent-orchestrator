"""F702 (#557) D3: the fleet table's column names, as constants.

Two disjoint groups:

* :data:`PARITY_COLUMNS` — the six headers the retiring stdlib script draws,
  verbatim and in order (root repo ``scripts/fleet-tui.py:344``). They are the
  default view (AC5).
* :data:`NEW_COLUMNS` — the five keys ``build_fleet()`` publishes that no code
  renders today (blueprint B7). Hidden by default, toggled with ``c``.

Names only. Widths, styles, and the ``DataTable`` wiring belong to J2's app
module; this module has no Textual dependency so it stays importable anywhere.
"""

from __future__ import annotations

from typing import Final, Tuple

#: The six parity headers, in the order of ``scripts/fleet-tui.py:344``.
PARITY_COLUMNS: Final[Tuple[str, ...]] = (
    "WIN",
    "ID",
    "PROFILE",
    "TASK",
    "STATUS",
    "IDLE",
)

#: The five new columns (blueprint D3): ``condition``, ``delegating``/
#: ``children_count``, ``fusion_changed``, ``lifecycle``, ``resolved_model``.
NEW_COLUMNS: Final[Tuple[str, ...]] = (
    "COND",
    "DELEG",
    "*",
    "LIFE",
    "MODEL",
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
