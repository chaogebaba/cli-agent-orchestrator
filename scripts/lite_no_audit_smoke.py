"""Isolated import/CLI-help/health smoke; NOT AC-LITE-1/2/4/5 completion.

The project must be empty. CAO_HOME_DIR is private; the user's HOME and hook
configuration are never changed. No server lifespan, live agent, model request,
or production launch is run. Knowledge I/O is denied before runtime imports.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import importlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from lite_boundary import knowledge_domain


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--probe-knowledge", help="Adversarial guard proof, not a runtime mode.")
    args = parser.parse_args()
    if list(args.project.iterdir()):
        parser.error("--project must be empty")
    os.environ["CAO_HOME_DIR"] = str(args.home.resolve())
    os.environ["CAO_AGENTS_DIR"] = str(args.home.resolve() / "agents")
    os.chdir(args.project)
    calls: list[str] = []

    def guarded(original: Callable[..., Any]) -> Callable[..., Any]:
        def invoke(path: Any, *positional: Any, **keyword: Any) -> Any:
            if not isinstance(path, int) and knowledge_domain(str(path)):
                calls.append(str(path))
                raise AssertionError(f"forbidden knowledge I/O: {path}")
            return original(path, *positional, **keyword)

        return invoke

    import contextlib

    with contextlib.ExitStack() as stack:
        for name in ("open", "stat", "lstat", "glob", "rglob", "iterdir", "mkdir"):
            stack.enter_context(patch.object(Path, name, guarded(getattr(Path, name))))
        stack.enter_context(patch.object(builtins, "open", guarded(builtins.open)))
        for name in ("stat", "lstat", "listdir", "scandir", "mkdir", "makedirs", "open", "access"):
            stack.enter_context(patch.object(os, name, guarded(getattr(os, name))))

        def no_external_work(*positional: Any, **keyword: Any) -> None:
            raise AssertionError("unexpected network/model/subprocess work during startup smoke")

        stack.enter_context(patch.object(socket.socket, "connect", no_external_work))
        stack.enter_context(patch.object(socket, "create_connection", no_external_work))
        stack.enter_context(patch.object(subprocess, "Popen", no_external_work))
        if args.probe_knowledge:
            Path(args.probe_knowledge).exists()
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.main import cli

        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0, result.output
        api = importlib.import_module("cli_agent_orchestrator.api.main")
        health = asyncio.run(api.health_check())
        assert health["status"] == "ok", health
        # Re-importing the optional live harness must not mkdir scratch on import.
        importlib.import_module("cli_agent_orchestrator.chatgpt_web_runner.live_spike")
    assert not calls, calls
    assert not list(args.project.iterdir()), "startup/help/health changed the empty project"
    print(
        json.dumps(
            {
                "knowledge_io": calls,
                "project_unchanged": True,
                "cli_help": "ok",
                "health": health["status"],
                "network_model_subprocess": "denied",
                "scope": "imports/help/health only; lifespan and lifecycle untested",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
