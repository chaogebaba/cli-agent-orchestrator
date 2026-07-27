"""Shared resolver for CAO-owned transient files."""

from __future__ import annotations

import os
from pathlib import Path

from cli_agent_orchestrator import constants


def cao_tmp_dir() -> Path:
    # constants.CAO_HOME_DIR, not a from-import: binding the value at THIS
    # module's import time freezes the home override for the process, so a
    # later importlib.reload(constants) (how the env override is exercised)
    # would not reach here. Late attribute lookup keeps the resolver honest.
    # Same module-local-binding trap recorded in blueprints/f63.
    path = Path(os.environ.get("CAO_TMP_DIR", str(constants.CAO_HOME_DIR / "tmp")))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path
