"""Transactional Markdown editing with mandatory post-conditions."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import click

from cli_agent_orchestrator.services.fold_service import (
    FoldHunk,
    FoldPostconditionError,
    FoldUsageError,
    check_file,
    fold_file,
    parse_hunks_document,
)

_T = TypeVar("_T")


def _single(name: str, values: tuple[_T, ...]) -> _T | None:
    if len(values) > 1:
        raise click.UsageError(f"{name} may appear at most once")
    return values[0] if values else None


def _operand(value: str, option: str) -> str:
    if not value.startswith("@"):
        return value
    if value == "@":
        raise click.UsageError(f"{option}: @ must name a file")
    path = Path(value[1:])
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise click.UsageError(f"{option}: cannot read {path}: {exc}") from exc


def _usage_message(file: Path, observed: object) -> str:
    return (
        f"{file}: expected a valid fold request; observed {observed}; "
        "hint: correct the edit spec and retry"
    )


def _flag_hunk(
    anchors: tuple[str, ...],
    replacements: tuple[str, ...],
    afters: tuple[str, ...],
    strikes: tuple[bool, ...],
    expected_counts: tuple[int, ...],
) -> tuple[FoldHunk | None, bool]:
    anchor = _single("--anchor", anchors)
    replacement = _single("--replace-with", replacements)
    after = _single("--after", afters)
    strike = _single("--strike", strikes)
    expected = _single("--expect-count", expected_counts)
    any_flag = any(value is not None for value in (anchor, replacement, after, strike, expected))
    if not any_flag:
        return None, False
    operations = sum(value is not None for value in (replacement, after, strike))
    if anchor is None or operations != 1:
        raise click.UsageError(
            "a flag edit requires --anchor and exactly one of "
            "--replace-with/--after/--strike; --expect-count is only a modifier"
        )
    if anchor == "":
        raise click.UsageError("--anchor must not be empty")
    count = int(expected) if expected is not None else 1
    if count < 1:
        raise click.UsageError("--expect-count must be at least 1")
    if replacement is not None:
        return (
            FoldHunk(str(anchor), "replace", _operand(str(replacement), "--replace-with"), count),
            True,
        )
    if after is not None:
        return FoldHunk(str(anchor), "after", _operand(str(after), "--after"), count), True
    return FoldHunk(str(anchor), "strike", expect_count=count), True


@click.command()
@click.argument("file", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--anchor", "anchors", multiple=True, help="Literal anchor text.")
@click.option(
    "--replace-with",
    "replacements",
    multiple=True,
    help="Replacement text, or @FILE.",
)
@click.option("--after", "afters", multiple=True, help="Text to insert after the anchor, or @FILE.")
@click.option("--strike", "strikes", is_flag=True, multiple=True, help="Wrap the anchor in ~~.")
@click.option(
    "--expect-count", "expected_counts", type=int, multiple=True, help="Required match count."
)
@click.option("--check", "check_only", is_flag=True, help="Report file-global P5/P6 violations.")
def fold(
    file: Path,
    anchors: tuple[str, ...],
    replacements: tuple[str, ...],
    afters: tuple[str, ...],
    strikes: tuple[bool, ...],
    expected_counts: tuple[int, ...],
    check_only: bool,
) -> None:
    """Transactionally edit one UTF-8 Markdown FILE, or check its structure."""
    try:
        flag_hunk, flag_present = _flag_hunk(
            anchors, replacements, afters, strikes, expected_counts
        )
        if check_only:
            if flag_present:
                raise click.UsageError("--check is mutually exclusive with an edit spec")
            result = check_file(file)
            click.echo("skipped: P1, P2, P3 (no edit span under --check)")
            if result.violations:
                for violation in result.violations:
                    click.echo(violation)
            else:
                click.echo("P5/P6: no violations")
            return

        if flag_present:
            assert flag_hunk is not None
            hunks = [flag_hunk]
        else:
            stdin = click.get_text_stream("stdin")
            if stdin.isatty():
                raise click.UsageError("no edit spec supplied; refusing to read from a TTY")
            hunks = parse_hunks_document(click.get_binary_stream("stdin").read())
        result = fold_file(file, hunks)
    except FoldUsageError as exc:
        raise click.UsageError(_usage_message(file, exc)) from exc
    except click.UsageError as exc:
        raise click.UsageError(_usage_message(file, exc.message)) from exc
    except FoldPostconditionError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{file} -> WROTE ({result.old_sha} -> {result.new_sha})")
