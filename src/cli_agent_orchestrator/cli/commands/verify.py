"""Verification commands for suite artifacts, deploy state, diff scope, and identity."""

import ipaddress
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import click

from cli_agent_orchestrator.constants import DATABASE_FILE
from cli_agent_orchestrator.kernel.receiver_state.trace_manifest import regenerate_manifest
from cli_agent_orchestrator.services.identity_verify_service import ScanResult, scan_identity
from cli_agent_orchestrator.services.verification_service import (
    changed_files,
    cli_deploy_root,
    deployment_status,
    format_server_status,
    git_root,
    verify_suite_log,
)


@click.group()
def verify() -> None:
    """Verify repository and installed runtime state."""


def _loopback_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise click.BadParameter("must be a loopback http origin") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise click.BadParameter("must be a loopback http origin")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        raise click.BadParameter("must use a loopback host")
    try:
        parsed.port
    except ValueError as exc:
        raise click.BadParameter("has an invalid port") from exc
    return value[:-1] if value.endswith("/") else value


def _default_identity_endpoint() -> str:
    host = os.environ.get("CAO_API_HOST") or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = os.environ.get("CAO_API_PORT") or "9889"
    return _loopback_endpoint(f"http://{host}:{port}")


def _identity_db(value: Path | None) -> Path:
    path = value or DATABASE_FILE
    if not path.is_absolute():
        raise click.BadParameter("must be an absolute path", param_hint="--db")
    if not path.is_file() or not os.access(path, os.R_OK):
        raise click.BadParameter("must name a readable sqlite file", param_hint="--db")
    return path


def _render_identity(result: ScanResult) -> None:
    click.echo(f"scan: endpoint={result['scan_endpoint']} db={result['scan_db']}")
    warning = result["summary"]["scan_warning"]
    if warning:
        click.echo(f"\nWARN: {warning}")

    rows = result["rows"]
    click.echo(f"\nIN SCOPE ({len(rows)})")
    click.echo(
        "mcp_pid  mcp_tid   verdict  tid_in_db  window  pane_tid   "
        "pane_agrees  pane_reason  parent_kind  reasons"
    )
    click.echo(
        "-------  --------  -------  ---------  ------  ---------  "
        "-----------  -----------  -----------  -------"
    )
    for row in rows:
        reasons = ",".join(row["fail_reasons"]) or "-"
        click.echo(
            f"{row['mcp_pid']:<7}  {row['mcp_tid'] or '-':<8}  {row['verdict']:<7}  "
            f"{str(row['tid_in_db']):<9}  {str(row['window_live']):<6}  "
            f"{row['pane_tid'] or '-':<9}  {str(row['pane_agrees']):<11}  "
            f"{row['pane_reason'] or '-':<11}  {row['parent_kind']:<11}  {reasons}"
        )

    outside = result["out_of_scope"]
    click.echo(f"\nOUT OF SCOPE ({len(outside)})")
    click.echo("mcp_pid  mcp_tid   endpoint                 reason")
    for row in outside:
        click.echo(
            f"{row['mcp_pid']:<7}  {row['mcp_tid'] or '-':<8}  "
            f"{row['endpoint'] or '-':<23}  {row['reason']}"
        )

    vanished = ",".join(str(pid) for pid in result["vanished_pids"]) or "none"
    click.echo(f"\nVANISHED DURING SCAN: {vanished}")
    if result["window_authority"]:
        for item in result["window_authority"]:
            ids = item["db_ids"]
            authority = ids[0] if len(ids) == 1 else f"ambiguous[{','.join(ids)}]"
            click.echo(
                f"WINDOW AUTHORITY: {item['tmux_session']}:{item['tmux_window']} → {authority}"
            )
    else:
        click.echo("WINDOW AUTHORITY: none")
    summary = result["summary"]
    click.echo(
        "summary: "
        f"ok={summary['ok']} warn={summary['warn']} fail={summary['fail']} "
        f"out_of_scope={summary['out_of_scope']} vanished={summary['vanished']}"
    )


@verify.command("identity")
@click.option("--json", "json_output", is_flag=True, help="Emit the scan document as JSON.")
@click.option("--endpoint", help="Loopback HTTP origin selecting process scope.")
@click.option("--db", "db_path", type=click.Path(path_type=Path), help="Absolute sqlite path.")
def identity(json_output: bool, endpoint: str | None, db_path: Path | None) -> None:
    """Verify MCP, pane, and database identity using offline local sources."""

    scan_endpoint = (
        _loopback_endpoint(endpoint) if endpoint is not None else _default_identity_endpoint()
    )
    database = _identity_db(db_path)
    try:
        result = scan_identity(endpoint=scan_endpoint, db_path=database)
    except (OSError, sqlite3.Error) as exc:
        raise click.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        _render_identity(result)
    if result["summary"]["fail"]:
        raise click.exceptions.Exit(1)

@verify.command("manifest")
@click.option("--regen", is_flag=True, help="Regenerate the receiver-state trace manifest.")
def manifest(regen: bool) -> None:
    """Maintain the receiver-state trace manifest."""
    if not regen:
        raise click.UsageError("Pass --regen to regenerate the trace manifest.")
    hits, files_touched, changed = regenerate_manifest(git_root())
    click.echo(
        f"Trace manifest: hits={hits} files_touched={files_touched} "
        f"changed={'yes' if changed else 'no'}"
    )


@verify.command("suite-log")
@click.argument("path", type=click.Path(path_type=Path))
def suite_log(path: Path) -> None:
    """Verify a stamped suite log against the current tree."""
    passed, reasons, mtime = verify_suite_log(path)
    click.echo(f"{'PASS' if passed else 'FAIL'}: {path}")
    click.echo(f"mtime: {mtime}")
    for reason in reasons:
        click.echo(f"reason: {reason}")
    if not passed:
        raise click.exceptions.Exit(1)


@verify.command("deploy")
def deploy() -> None:
    """Compare the installed CLI and running server with this working tree."""
    try:
        root = cli_deploy_root(git_root())
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(str(exc))
    status = deployment_status(root)
    state = status["cli_path"]
    count = status["differing_files"]
    if count is None:
        click.echo(f"CLI path: {state}")
    else:
        click.echo(f"CLI path: {state} ({count} files differ)")
    server_state = status["server"]
    click.echo(format_server_status(server_state))
    if state != "current" or server_state != "current":
        raise click.exceptions.Exit(1)


@verify.command("scope")
@click.argument("files", nargs=-1, required=True, type=click.Path(path_type=Path))
def scope(files: tuple[Path, ...]) -> None:
    """Require working-tree changes to exactly match FILES."""
    root = git_root()
    actual = set(changed_files(root))
    expected = {
        (
            str((Path.cwd() / path).resolve().relative_to(root))
            if not path.is_absolute()
            else str(path.resolve().relative_to(root))
        )
        for path in files
    }
    unexpected, missing = sorted(actual - expected), sorted(expected - actual)
    exact = not unexpected and not missing
    click.echo("PASS: exact scope match" if exact else "FAIL: scope mismatch")
    click.echo(f"unexpected changes: {', '.join(unexpected) if unexpected else '(none)'}")
    click.echo(f"missing expected: {', '.join(missing) if missing else '(none)'}")
    if not exact:
        raise click.exceptions.Exit(1)
