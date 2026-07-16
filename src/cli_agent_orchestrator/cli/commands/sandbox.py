"""Post-activation convenience wrapper for the import-clean sandbox bootstrap."""

from __future__ import annotations

import os
import sys

import click


def _exec_bootstrap(*args: str) -> None:
    command = [
        sys.executable,
        "-B",
        "-m",
        "cli_agent_orchestrator.sandbox_bootstrap",
        *args,
    ]
    os.execv(sys.executable, command)


@click.group()
def sandbox() -> None:
    """Manage an isolated working-tree CAO server."""


@sandbox.command("up")
@click.option("--root", required=True, type=click.Path())
@click.option("--port", required=True, type=int)
def up(root: str, port: int) -> None:
    _exec_bootstrap("up", "--root", root, "--port", str(port))


@sandbox.command("status")
@click.option("--root", required=True, type=click.Path())
def status(root: str) -> None:
    _exec_bootstrap("status", "--root", root)


@sandbox.command("down")
@click.option("--root", required=True, type=click.Path())
@click.option("--purge", is_flag=True)
def down(root: str, purge: bool) -> None:
    args = ["down", "--root", root]
    if purge:
        args.append("--purge")
    _exec_bootstrap(*args)
