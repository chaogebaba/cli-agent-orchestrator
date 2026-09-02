"""``cao diag`` — one timeline for one worker (WP-ARCH phase 1, AC7).

This module is AC11's hook point 4 and nothing more: it parses arguments, asks
the composition root for read-only stores, and hands the rows to
``app/diag/report.py``.  Every judgement about what an operator should see lives
there, so it can be tested without a Click runner and without a database.

Two constraints shape the file:

* **It may not import ``adapters``.**  AC9's fifth contract puts ``cli`` on the
  same side of the line as ``app``; only ``bootstrap.py`` names an adapter.  That
  is why the stores arrive through :func:`build_readonly_diag_stores` rather than
  from a ``sqlite3.connect`` here.
* **It never writes.**  The database it opens is the LIVE server's, and WAL makes
  a concurrent reader safe only as long as it stays a reader.  A diagnostic
  command that could take a write lock would be able to stall the very server it
  was called to diagnose.

``cao diag <terminal_id>`` works as the blueprint writes it, without a
subcommand, through :class:`_DiagGroup` below.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import click

from cli_agent_orchestrator.app.diag.report import (
    DiagSources,
    findings_payload,
    render_agreement,
    render_findings,
    render_timeline,
    render_why,
    timeline_payload,
    why_payload,
)
from cli_agent_orchestrator.app.worker_truth.agreement import build_agreement_report
from cli_agent_orchestrator.core.events import AnyKind, parse_kind
from cli_agent_orchestrator.core.findings import FindingCode

INGEST_ENV_VAR = "CAO_WORKER_TRUTH_INGEST"

_RELATIVE = re.compile(r"^(\d+)([smhd])$")
_RELATIVE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _ingest_on() -> bool:
    return os.environ.get(INGEST_ENV_VAR) == "1"


def _parse_since(value: str | None, now: datetime) -> datetime | None:
    """Accept either ``30m`` or an ISO-8601 timestamp.

    The relative form is first because it is the one an operator types.  "What
    happened in the last ten minutes" is the actual question; converting it to a
    wall-clock time by hand is a step that only ever introduces mistakes.
    """
    if value is None:
        return None
    match = _RELATIVE.match(value.strip())
    if match is not None:
        amount, unit = match.groups()
        return now - timedelta(seconds=int(amount) * _RELATIVE_UNITS[unit])
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter(
            f"{value!r} is neither a duration like 30m nor an ISO-8601 timestamp"
        ) from exc
    # A naive timestamp is read as UTC rather than rejected: every stored stamp
    # is UTC, so that is the only reading that can be right, and rejecting it
    # would fail an operator for omitting a suffix that has one legal value.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_kinds(values: tuple[str, ...]) -> frozenset[AnyKind] | None:
    if not values:
        return None
    kinds: set[AnyKind] = set()
    for value in values:
        try:
            kinds.add(parse_kind(value))
        except ValueError as exc:
            raise click.BadParameter(f"unknown event kind {value!r}") from exc
    return frozenset(kinds)


def _sources(db_path: str | None) -> DiagSources:
    """Ask the composition root for read-only stores.

    Imported inside the function, not at module import.  ``cli/main.py`` imports
    every command module at startup, and a phase-1 import failure here would take
    the whole CLI down — including ``cao doctor``, which is what an operator
    would reach for next.  Failing at invocation keeps the blast radius to this
    one command.
    """
    from cli_agent_orchestrator.bootstrap import build_readonly_diag_stores

    return build_readonly_diag_stores(db_path)


def _emit(payload: Any, text: str, as_json: bool) -> None:
    click.echo(json.dumps(payload, indent=2, default=str) if as_json else text)


class _DiagGroup(click.Group):
    """Lets ``cao diag <terminal_id>`` work without naming a subcommand.

    The blueprint spells the command that way and it is the right shape: the
    terminal timeline is what the command is FOR, and the other views are
    variations on it.  An unrecognised first argument is therefore routed to
    ``terminal`` rather than rejected.

    A leading dash is left alone so ``cao diag --help`` and ``cao diag --why``
    still resolve normally, and so does anything that really is a subcommand.
    """

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            return super().resolve_command(ctx, ["terminal", *args])
        return super().resolve_command(ctx, args)


@click.group(cls=_DiagGroup, invoke_without_command=True)
@click.option("--why", "why_event_id", default=None, help="Print the evidence chain for an event.")
@click.option("--db", "db_path", default=None, help="Database path (defaults to the server's).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_context
def diag(
    ctx: click.Context, why_event_id: str | None, db_path: str | None, as_json: bool
) -> None:
    """Reconstruct what happened to a worker from stored rows.

    \b
    cao diag <terminal-id>          the timeline and the projection header
    cao diag --why <event-id>       the evidence chain behind one decision
    cao diag findings               open invariant findings
    cao diag agreement              the shadow projection vs the legacy status
    """
    if ctx.invoked_subcommand is not None:
        return
    if why_event_id is None:
        click.echo(ctx.get_help())
        return
    sources = _sources(db_path)
    _emit(
        why_payload(sources, why_event_id),
        render_why(sources, why_event_id),
        as_json,
    )


@diag.command("terminal")
@click.argument("terminal_id")
@click.option("--since", default=None, help="Only rows newer than this (e.g. 30m, or ISO-8601).")
@click.option("--kind", "kinds", multiple=True, help="Only these event kinds; repeatable.")
@click.option("--db", "db_path", default=None, help="Database path (defaults to the server's).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def diag_terminal(
    terminal_id: str,
    since: str | None,
    kinds: tuple[str, ...],
    db_path: str | None,
    as_json: bool,
) -> None:
    """Print one worker's timeline, newest last."""
    now = datetime.now(UTC)
    since_at = _parse_since(since, now)
    kind_filter = _parse_kinds(kinds)
    sources = _sources(db_path)
    ingest_on = _ingest_on()
    _emit(
        timeline_payload(
            sources, terminal_id, now=now, since=since_at, kinds=kind_filter, ingest_on=ingest_on
        ),
        render_timeline(
            sources, terminal_id, now=now, since=since_at, kinds=kind_filter, ingest_on=ingest_on
        ),
        as_json,
    )


@diag.command("why")
@click.argument("event_id")
@click.option("--db", "db_path", default=None, help="Database path (defaults to the server's).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def diag_why(event_id: str, db_path: str | None, as_json: bool) -> None:
    """Print the evidence chain behind one event."""
    sources = _sources(db_path)
    _emit(why_payload(sources, event_id), render_why(sources, event_id), as_json)


@diag.command("findings")
@click.option("--state", default="open", help="open, resolved, or all.")
@click.option("--code", "code_value", default=None, help="Only this DIAG-* code.")
@click.option("--db", "db_path", default=None, help="Database path (defaults to the server's).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def diag_findings(
    state: str, code_value: str | None, db_path: str | None, as_json: bool
) -> None:
    """List typed invariant findings, loudest first."""
    code: FindingCode | None = None
    if code_value is not None:
        try:
            code = FindingCode(code_value)
        except ValueError as exc:
            raise click.BadParameter(f"unknown finding code {code_value!r}") from exc
    wanted = None if state == "all" else state
    sources = _sources(db_path)
    _emit(
        findings_payload(sources, state=wanted, code=code),
        render_findings(sources, now=datetime.now(UTC), state=wanted, code=code),
        as_json,
    )


@diag.command("agreement")
@click.option("--session", default=None, help="Restrict to one tmux session.")
@click.option("--db", "db_path", default=None, help="Database path (defaults to the server's).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def diag_agreement(session: str | None, db_path: str | None, as_json: bool) -> None:
    """Compare the shadow projection against the legacy published status (AC10)."""
    from cli_agent_orchestrator.bootstrap import build_terminal_scope

    now = datetime.now(UTC)
    sources = _sources(db_path)
    report = build_agreement_report(
        sources.events.read(),
        scope=build_terminal_scope(db_path),
        session=session,
        generated_at=now,
    )
    payload = {
        "valid": report.valid,
        "invalid_reasons": report.invalid_reasons,
        "generated_at": now.isoformat(),
        "totals": {
            "terminals": len(report.terminals),
            "codex_terminals": report.codex_terminals,
            "events": report.total_events,
            "transitions": report.total_transitions,
            "legacy_publishes": report.total_legacy_publishes,
            "comparisons": report.total_comparisons,
            "agreements": report.total_agreements,
            "agreement_rate": report.fleet_agreement_rate,
        },
        "classifications": report.classification_counts(),
        "terminals": [
            {
                "terminal_id": terminal.terminal_id,
                "session": terminal.session,
                "provider": terminal.provider,
                "is_codex": terminal.is_codex,
                "events": terminal.events,
                "transitions": terminal.transitions,
                "legacy_publishes": terminal.legacy_publishes,
                "comparisons": terminal.comparisons,
                "agreements": terminal.agreements,
                "agreement_rate": terminal.agreement_rate,
                "disagreements": [
                    {
                        "projected": d.projected.value,
                        "legacy": d.legacy.value,
                        "started_at": d.started_at.isoformat(),
                        "ended_at": d.ended_at.isoformat() if d.ended_at else None,
                        "duration_s": d.duration_s,
                        "classification": d.classification,
                        "opened_by": d.opened_by,
                        "sample_event_id": d.sample_event_id,
                    }
                    for d in terminal.disagreements
                ],
            }
            for terminal in report.terminals
        ],
    }
    _emit(payload, render_agreement(report), as_json)
    if not report.valid:
        # A non-zero exit so a script cannot mistake an INVALID report for a
        # passing one.  AC10 makes this report the phase gate; a gate that exits
        # 0 on "no evidence" is not a gate.
        raise SystemExit(2)
