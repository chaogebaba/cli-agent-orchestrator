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


# ---------------------------------------------------------------------------
# The question verbs (slice B1; §10.2 P2, A2).
#
# Standing invariant #535/F680: every new MCP tool ships with a ``cao gate``
# verb over the identical route.  ``ask`` and ``answer`` below POST to exactly
# the handlers ``ask_supervisor`` and ``answer_question`` call, so the two
# surfaces cannot drift — there is one service call underneath both.
#
# ``--db`` is the second path, and it exists for one reason: an acceptance run
# (and an offline inspection) must be able to work a SCRATCH database with no
# server at all, and pointing the live server at a scratch file is not something
# a verb should be able to do.  With ``--db`` the verb builds the service
# in-process through the composition root; without it, it goes over HTTP to the
# running server, which is the path the invariant is about.
# ---------------------------------------------------------------------------


def _question_service(db_path: str) -> Any:
    from cli_agent_orchestrator.bootstrap import build_gate_question_service

    return build_gate_question_service(db_path)


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    import requests

    from cli_agent_orchestrator.cli.http import bearer_headers
    from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT
    from cli_agent_orchestrator.utils.http import CAOHttpClient

    client = CAOHttpClient(lambda: requests)
    try:
        response = client.post(
            path, json=payload, headers=bearer_headers(), timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        raise click.ClickException(f"could not reach cao-server: {exc}")
    if response.status_code >= 400:
        raise click.ClickException(f"{path} refused: {response.text}")
    body: dict[str, Any] = response.json()
    return body


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    import requests

    from cli_agent_orchestrator.cli.http import bearer_headers
    from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT
    from cli_agent_orchestrator.utils.http import CAOHttpClient

    client = CAOHttpClient(lambda: requests)
    try:
        response = client.get(
            path, params=params, headers=bearer_headers(), timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        raise click.ClickException(f"could not reach cao-server: {exc}")
    if response.status_code >= 400:
        raise click.ClickException(f"{path} refused: {response.text}")
    body: dict[str, Any] = response.json()
    return body


def _question_line(payload: dict[str, Any]) -> str:
    options = payload.get("options") or []
    suffix = f"  options: {' | '.join(options)}" if options else ""
    return (
        f"{payload['question_id']}  [{payload['state']}] "
        f"dispatch={payload['dispatch_id']} expires={payload['expires_at']}\n"
        f"  {payload['question']}{suffix}"
    )


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
    else:
        click.echo(_question_line(payload))


@gate.command("ask")
@click.argument("question")
@click.option("--dispatch", "dispatch_id", required=True, help="Asking dispatch/terminal id.")
@click.option("--owner", default="supervisor", show_default=True, help="Owning conversation.")
@click.option("--owner-epoch", type=int, default=0, show_default=True)
@click.option("--option", "options", multiple=True, help="An acceptable answer; repeatable.")
@click.option("--round", "round_id", default=None, help="Bind the question to a gate round.")
@click.option(
    "--request-id",
    "client_request_id",
    default=None,
    help="Idempotency key; a repeat returns the same question.",
)
@click.option("--expires-in", "expires_in_s", type=int, default=3600, show_default=True)
@click.option(
    "--non-blocking/--blocking",
    "non_blocking",
    default=False,
    help="A non-blocking ask needs --default; the caller continues at once.",
)
@click.option("--default", "default_answer", default=None, help="Default for a non-blocking ask.")
@click.option(
    "--position", default="dev", show_default=True, help="Position, if the dispatch is new."
)
@click.option("--db", "db_path", default=None, help="Work this database directly, no server.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def ask(
    question: str,
    dispatch_id: str,
    owner: str,
    owner_epoch: int,
    options: tuple[str, ...],
    round_id: str | None,
    client_request_id: str | None,
    expires_in_s: int,
    non_blocking: bool,
    default_answer: str | None,
    position: str,
    db_path: str | None,
    as_json: bool,
) -> None:
    """Record a durable question and suspend the asking dispatch."""
    request_id = client_request_id or f"cli-{dispatch_id}-{abs(hash(question)) % (10**12)}"
    if db_path is not None:
        from cli_agent_orchestrator.core.gate import GateError

        try:
            record = _question_service(db_path).ask(
                dispatch_id=dispatch_id,
                question=question,
                client_request_id=request_id,
                owner_conversation=owner,
                owner_epoch=owner_epoch,
                round_id=round_id,
                options=tuple(options),
                blocking=not non_blocking,
                expires_in_s=expires_in_s,
                default_answer=default_answer,
                position=position,
            )
        except GateError as exc:
            raise click.ClickException(f"{getattr(exc, 'code', 'E_GATE')}: {exc}")
        _emit(_payload_from_record(record), as_json)
        return
    body = _post(
        "/gate/questions",
        {
            "dispatch_id": dispatch_id,
            "question": question,
            "client_request_id": request_id,
            "owner_conversation": owner,
            "owner_epoch": owner_epoch,
            "round_id": round_id,
            "options": list(options),
            "blocking": not non_blocking,
            "expires_in_s": expires_in_s,
            "default_answer": default_answer,
            "position": position,
        },
    )
    _emit(body, as_json)


@gate.command("answer")
@click.argument("question_id")
@click.argument("answer_text")
@click.option("--by", "answered_by", default="supervisor", show_default=True)
@click.option("--caller", "caller_conversation", default="supervisor", show_default=True)
@click.option("--caller-epoch", type=int, default=0, show_default=True)
@click.option("--request-id", "client_request_id", default=None)
@click.option("--db", "db_path", default=None, help="Work this database directly, no server.")
@click.option("--json", "as_json", is_flag=True)
def answer(
    question_id: str,
    answer_text: str,
    answered_by: str,
    caller_conversation: str,
    caller_epoch: int,
    client_request_id: str | None,
    db_path: str | None,
    as_json: bool,
) -> None:
    """Answer a durable question by id (refused if settled, expired or superseded)."""
    request_id = client_request_id or f"cli-{question_id}-{abs(hash(answer_text)) % (10**12)}"
    if db_path is not None:
        from cli_agent_orchestrator.core.gate import GateError

        try:
            record, _event = _question_service(db_path).answer(
                question_id=question_id,
                answer=answer_text,
                answered_by=answered_by,
                client_request_id=request_id,
                caller_conversation=caller_conversation,
                caller_epoch=caller_epoch,
            )
        except GateError as exc:
            raise click.ClickException(f"{getattr(exc, 'code', 'E_GATE')}: {exc}")
        _emit(_payload_from_record(record), as_json)
        return
    body = _post(
        f"/gate/questions/{question_id}/answer",
        {
            "answer": answer_text,
            "answered_by": answered_by,
            "client_request_id": request_id,
            "caller_conversation": caller_conversation,
            "caller_epoch": caller_epoch,
        },
    )
    _emit(body["question"] if "question" in body else body, as_json)


@gate.command("escalate")
@click.argument("question_id")
@click.option("--db", "db_path", default=None, help="Work this database directly, no server.")
@click.option("--json", "as_json", is_flag=True)
def escalate(question_id: str, db_path: str | None, as_json: bool) -> None:
    """Raise a PENDING question to ESCALATED, keeping the same id."""
    if db_path is not None:
        from cli_agent_orchestrator.core.gate import GateError

        try:
            record = _question_service(db_path).escalate(question_id)
        except GateError as exc:
            raise click.ClickException(f"{getattr(exc, 'code', 'E_GATE')}: {exc}")
        _emit(_payload_from_record(record), as_json)
        return
    _emit(_post(f"/gate/questions/{question_id}/escalate", {}), as_json)


@gate.command("questions")
@click.option("--owner", default=None, help="Filter by owning conversation.")
@click.option("--state", "states", multiple=True, help="Filter by state; repeatable.")
@click.option("--round", "round_id", default=None, help="Filter by gate round.")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--db", "db_path", default=None, help="Work this database directly, no server.")
@click.option("--json", "as_json", is_flag=True)
def questions(
    owner: str | None,
    states: tuple[str, ...],
    round_id: str | None,
    limit: int,
    db_path: str | None,
    as_json: bool,
) -> None:
    """List questions, newest first; open ones unless --state says otherwise."""
    if db_path is not None:
        from cli_agent_orchestrator.core.gate import QuestionState

        selected = (
            tuple(QuestionState(s) for s in states)
            if states
            else (QuestionState.PENDING, QuestionState.ESCALATED)
        )
        records = _question_service(db_path).list_open(
            owner_conversation=owner, states=selected, round_id=round_id, limit=limit
        )
        items = [_payload_from_record(r) for r in records]
    else:
        params: dict[str, Any] = {"limit": limit}
        if owner is not None:
            params["owner"] = owner
        if states:
            params["state"] = list(states)
        if round_id is not None:
            params["round_id"] = round_id
        items = _get("/gate/questions", params)["items"]
    if as_json:
        click.echo(json.dumps({"items": items}, indent=2, default=str))
        return
    if not items:
        click.echo("no questions")
        return
    for item in items:
        click.echo(_question_line(item))


@gate.command("question")
@click.argument("question_id")
@click.option("--db", "db_path", default=None, help="Work this database directly, no server.")
@click.option("--json", "as_json", is_flag=True)
def question(question_id: str, db_path: str | None, as_json: bool) -> None:
    """Show one question and whether it has settled."""
    if db_path is not None:
        record = _question_service(db_path).get(question_id)
        if record is None:
            raise click.ClickException(f"no such question: {question_id}")
        _emit(_payload_from_record(record), as_json)
        return
    _emit(_get(f"/gate/questions/{question_id}"), as_json)


def _payload_from_record(record: Any) -> dict[str, Any]:
    """The same JSON shape the route returns, for the ``--db`` path.

    Built from ``app/gate``'s serialiser — the one the route also calls — so the
    two paths cannot print different field names for the same row.  A CLI that
    renamed a field offline would make every ``--db`` transcript unusable as
    evidence about the served surface.
    """
    from cli_agent_orchestrator.app.gate.render import question_payload

    return dict(question_payload(record))
