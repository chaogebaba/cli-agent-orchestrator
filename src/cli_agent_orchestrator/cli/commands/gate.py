"""``cao gate show`` — a read-only view of a gate run (WP-ARCH A, slice 2a).

This module parses arguments, asks the composition root for a READ-ONLY gate
store, and hands the rows to ``app/gate/render.py``.  Every judgement about what
an operator sees lives there, so it is tested without a Click runner and without
a database.

Two constraints, the same two ``cao diag`` keeps:

* **It may not import ``adapters``.**  The ``adapters-only-via-composition-root``
  contract puts ``cli`` on the same side of the line as ``app``; the store arrives
  through :func:`bootstrap.build_readonly_gate_store`, never a ``sqlite3.connect``
  here.
* **It never writes.**  The database it opens is the LIVE server's, over a
  ``mode=ro`` connection, so a view command can never take a write lock on the
  coordination database.

Slice 2a ships ``show`` only: the runner's write commands (dispatch, accept,
merge) are 2b/2c.  ``show`` is the read half the records-and-rendering increment
needs to prove AC-A1's restart arm at the CLI — a round re-rendered from rows.
"""

from __future__ import annotations

import json
from typing import Any

import click

__all__ = ["gate"]


def _readonly_store(db_path: str | None) -> Any:
    """Ask the composition root for a read-only gate store.

    Imported inside the function, not at module import: ``cli/main.py`` imports
    every command module at startup, so a failure here would take the whole CLI
    down.  Failing at invocation keeps the blast radius to this one command.
    """
    from cli_agent_orchestrator.bootstrap import build_readonly_gate_store

    return build_readonly_gate_store(db_path)


@click.group()
def gate() -> None:
    """Inspect gate runs (WP-ARCH Amendment A)."""


@gate.command("show")
@click.argument("run_id")
@click.option("--db", "db_path", default=None, help="Override the database path.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def show(run_id: str, db_path: str | None, as_json: bool) -> None:
    """Show a run and its rounds, projected from rows (read-only).

    Every sha shown is DERIVED from the stored manifest, and every scratch root is
    GENERATED from the round's host — the CLI carries no free-text sha slot, so it
    inherits AC-A1/AC-A3 from the renderer it calls.
    """
    from cli_agent_orchestrator.app.gate.render import render_run_show, run_show_payload

    store = _readonly_store(db_path)
    run = store.get_run(run_id)
    if run is None:
        raise click.ClickException(f"no such gate run: {run_id}")
    projections = []
    for round_ in store.rounds_for_run(run_id):
        projection = store.project_round(round_.round_id)
        if projection is not None:
            projections.append(projection)
    projections_t = tuple(projections)
    if as_json:
        click.echo(json.dumps(run_show_payload(run, projections_t), indent=2, default=str))
    else:
        click.echo(render_run_show(run, projections_t), nl=False)
