"""F702 (#557): the `cao-fleet` Textual TUI package.

Modules here are deliberately import-light: :mod:`status_cell` and
:mod:`columns` are pure and depend only on ``rich``, so they can be unit-tested
without a Textual app, a server, or the fetcher loop. J1 ships the read-only
model (:mod:`fleet_state`) and the fetcher loop (:mod:`fetcher`); the Textual
app, widgets and key bindings are J2.
"""

from cli_agent_orchestrator.tui.fleet_state import FleetState, TerminalState

__all__ = ["FleetState", "TerminalState"]
