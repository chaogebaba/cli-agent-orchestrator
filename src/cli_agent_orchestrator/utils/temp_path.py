"""Shared resolver for CAO-owned transient files."""

from __future__ import annotations

import os
from pathlib import Path

from cli_agent_orchestrator.constants import CAO_HOME_DIR


def cao_tmp_dir() -> Path:
    path = Path(os.environ.get("CAO_TMP_DIR", str(CAO_HOME_DIR / "tmp")))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path
