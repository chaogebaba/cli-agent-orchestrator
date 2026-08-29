"""``cao auto-answers`` — inspect the whitelist auto-responder (F530).

The ``test`` subcommand replays a captured pane against a provider's rules and
prints the computed dialog region plus each rule's verdict (matched, or the
exact failing field). It sends no keys and mutates no state — it is a pure
diagnostic so a stalled auto-responder dialog can be root-caused from a saved
pane capture without a live supervisor.
"""

from pathlib import Path

import click

from cli_agent_orchestrator.providers.manager import get_provider_class
from cli_agent_orchestrator.services.auto_responder import diagnose_rules


@click.group(name="auto-answers", invoke_without_command=True)
@click.pass_context
def auto_answers(ctx: click.Context) -> None:
    """Inspect the whitelist auto-responder rules and matching."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@auto_answers.command("test")
@click.argument("provider")
@click.argument("pane_textfile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def auto_answers_test(provider: str, pane_textfile: Path) -> None:
    """Replay PANE_TEXTFILE against PROVIDER's rules; print region + verdicts.

    PROVIDER is the provider name (e.g. ``codex``). PANE_TEXTFILE is a plain-text
    capture of a rendered pane (one screen row per line). Nothing is sent and no
    state changes — this only reports why each rule does or does not match.
    """
    lines = pane_textfile.read_text(encoding="utf-8", errors="replace").splitlines()

    provider_cls = None
    try:
        provider_cls = get_provider_class(provider)
    except ValueError:
        click.echo(
            f"note: unknown provider '{provider}' — reporting without chrome filtering",
            err=True,
        )

    report = diagnose_rules(provider, lines, provider_cls)

    click.echo(f"provider: {report['provider']}")
    click.echo(f"chrome-filtered match region: {report['chrome_filtered']}")
    click.echo("")
    click.echo("dialog region (unfiltered tail rows):")
    for row in report["region_rows"]:
        click.echo(f"  | {row}")
    click.echo("")
    click.echo(f"region.normalized:  {report['region_normalized']!r}")
    if report["match_normalized"] != report["region_normalized"]:
        click.echo(f"match.normalized:   {report['match_normalized']!r}")
    click.echo("")

    rules = report["rules"]
    if not rules:
        click.echo("no rules configured for this provider")
        return

    any_match = False
    click.echo("rules:")
    for rule in rules:
        if rule["matched"]:
            any_match = True
            click.echo(f"  ✓ {rule['name']} (mode={rule['match_mode']}) → MATCH answer={rule['answer']!r}")
        else:
            click.echo(
                f"  ✗ {rule['name']} (mode={rule['match_mode']}) → reject: {rule['reject_reason']}"
            )
    click.echo("")
    click.echo("verdict: " + ("a rule MATCHES (would fire/wait)" if any_match else "NO rule matched (unknown-dialog push)"))
