"""Config commands for CLI Agent Orchestrator CLI (issue #357)."""

import json
import sys
from dataclasses import asdict

import click

from cli_agent_orchestrator.cli.commands.config_reconcile import reconcile
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.systemd_tmux_preflight import run_activation_preflight
from cli_agent_orchestrator.utils.sandbox_guard import require_not_sandbox_mutation


def _coerce(value: str):
    """Best-effort coercion of a CLI string value to bool/int/float/JSON list."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


@click.group()
def config():
    """Inspect and edit CAO configuration."""


@config.command(name="get")
@click.argument("key")
def get_cmd(key):
    """Get the resolved value for a dotted config KEY, e.g. terminal.backend."""
    value = ConfigService.get(key)
    click.echo(json.dumps(value))


_ENV_ONLY_SECTIONS = ("network.", "auth.")


@config.command(name="set")
@click.argument("key")
@click.argument("value")
def set_cmd(key, value):
    """Set config KEY to VALUE, persisting it to settings.json."""
    require_not_sandbox_mutation("config set")
    try:
        result = ConfigService.set(key, _coerce(value))
    except (ValueError, KeyError) as exc:
        # settings_service setters raise ValueError/KeyError for invalid keys
        # or out-of-range values (e.g. memory.flush_threshold). Surface a clean
        # CLI error instead of an unhandled Python traceback.
        raise click.ClickException(str(exc))
    click.echo(json.dumps(result))
    if key.startswith(_ENV_ONLY_SECTIONS):
        click.echo(
            f"warning: '{key}' is stored but has no runtime effect yet — "
            "only its CAO_* env var is read (see docs/configuration.md).",
            err=True,
        )


@config.command(name="list")
def list_cmd():
    """List every known config key with its resolved value."""
    for key, value in ConfigService.list_all().items():
        click.echo(f"{key} = {json.dumps(value)}")


@config.command(name="path")
def path_cmd():
    """Print the absolute path to the unified settings.json file."""
    click.echo(str(ConfigService.path()))


config.add_command(reconcile)


# ---------------------------------------------------------------------------
# F137 activation preflight
# ---------------------------------------------------------------------------


def _serialize_check(check) -> dict:
    """Convert an ActivationPreflightCheck to a JSON-friendly dict."""
    d = asdict(check)
    # Convert tuple observed/expected to list for JSON
    for key in ("observed", "expected"):
        if isinstance(d[key], tuple):
            d[key] = list(d[key])
    return d


@config.command(name="preflight")
@click.option("--activation", is_flag=True, required=True, help="Run activation safety preflight.")
@click.option("--json", "use_json", is_flag=True, default=False, help="Output as stable JSON.")
def preflight_cmd(activation: bool, use_json: bool):
    """Run activation safety preflight (F137).

    Validates systemd configuration is safe for CAO activation.
    Read-only — never mutates the system.

    Exit 0 = all checks pass. Nonzero = activation blocked.
    """
    result = run_activation_preflight()

    if use_json:
        output = {
            "ok": result.ok,
            "mode": result.mode,
            "version": result.version,
            "version_policy": result.version_policy,
            "enabled_prefix_dropins": list(result.enabled_prefix_dropins),
            "checks": [_serialize_check(c) for c in result.checks],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        status = "PASS" if result.ok else "FAIL"
        click.echo(f"{status} — mode={result.mode} version={result.version} policy={result.version_policy}")
        for check in result.checks:
            marker = "✓" if check.ok else "✗"
            line = f"  {marker} {check.code}"
            if check.detail:
                line += f": {check.detail}"
            if not check.ok and check.observed is not None:
                line += f" (observed={check.observed})"
            if not check.ok and check.expected is not None:
                line += f" (expected={check.expected})"
            click.echo(line)
        if result.enabled_prefix_dropins:
            click.echo("  Enabled prefix drop-ins:")
            for path in result.enabled_prefix_dropins:
                click.echo(f"    - {path}")

    sys.exit(0 if result.ok else 1)
