"""Resolve a CAO session from an explicit name or the current terminal."""

from __future__ import annotations

import os
import re
from typing import Optional

from cli_agent_orchestrator.utils.http import cao_http

# TerminalId in models/terminal.py:11 is the canonical terminal-id declaration.
_TERMINAL_ID_PATTERN = re.compile(r"^[a-f0-9]{8}$")


def resolve_session_name(session_name: Optional[str], *, timeout: float) -> str:
    """Resolve ``session_name`` using the current terminal when needed."""
    if session_name:
        return session_name

    terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if not terminal_id or not _TERMINAL_ID_PATTERN.fullmatch(terminal_id):
        raise ValueError("session_name required outside a CAO terminal")

    response = cao_http.get(f"/terminals/{terminal_id}", timeout=timeout)
    response.raise_for_status()
    return response.json()["session_name"]
