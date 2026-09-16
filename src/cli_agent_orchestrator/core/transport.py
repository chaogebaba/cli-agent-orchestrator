"""D20 — which message plane owns a terminal, as one closed vocabulary.

WP-ACP-PLANE D20 gives a terminal row a ``transport`` column and makes the two
tmux coordinate columns nullable for ``transport='acp'``.  The decision's second
half is the one that needs a module: **every consumer branches on ``transport``,
never on NULL.**

The difference is not stylistic.  ``tmux_window IS NULL`` is ambiguous between
"this terminal never had a pane" and "this terminal's pane is gone", and the
fleet projection already treats the second as ``ERROR``.  A NULL test would
therefore stamp every healthy ACP seat ``ERROR`` the moment it was created —
AC-S1.10's named failure, and the reason its grep fails a NULL check on either
coordinate column.

So the predicates live here, take the ROW (not the column), and are total: a row
with no ``transport`` key at all — a projection written before D20, or a test
fixture — reads as ``pane``, which is what every pre-D20 row is.  Defaulting the
other way would make an unrelated missing key look like an ACP seat and silently
suppress a real pane-absence error.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

__all__ = [
    "Transport",
    "is_acp_terminal",
    "is_pane_terminal",
    "transport_of",
]


class Transport(StrEnum):
    """D20's two values.  There is no third, and no NULL.

    ``PANE`` is the column default and the back-fill for every row that existed
    before the migration, so the vocabulary never has to represent "unknown".
    """

    PANE = "pane"
    ACP = "acp"


def transport_of(row: Mapping[str, Any] | Any) -> Transport:
    """The transport of a terminal row, whether it is a mapping or an ORM object.

    Both shapes reach the consumers this guards: ``fleet_service`` works on the
    dict projections ``clients/database.py`` returns, while
    ``terminal_service`` holds ``TerminalModel`` instances.  One reader for both
    keeps the branch identical on either side rather than leaving each caller to
    remember which it has.

    An unrecognised value reads as ``PANE`` rather than raising.  The column has
    a CHECK constraint, so an unrecognised value means a row written by a
    different build, and degrading it to the pane behaviour is the conservative
    answer: the worst case is a stale coordinate check on a row that has
    coordinates.
    """
    if isinstance(row, Mapping):
        raw = row.get("transport")
    else:
        raw = getattr(row, "transport", None)
    if raw == Transport.ACP:
        return Transport.ACP
    return Transport.PANE


def is_acp_terminal(row: Mapping[str, Any] | Any) -> bool:
    """True when the ACP plane owns this terminal, so it has no pane to inspect."""
    return transport_of(row) is Transport.ACP


def is_pane_terminal(row: Mapping[str, Any] | Any) -> bool:
    """True when a backend pane is this terminal's carrier, coordinates and all."""
    return transport_of(row) is Transport.PANE
