"""HOTFIX F114: recover CAO_TERMINAL_ID when kiro drops pane env at MCP spawn.

kiro-cli launches cao-mcp-server through a node intermediary that does not
inherit the tmux pane environment, so ``CAO_TERMINAL_ID`` is unset in the MCP
process (tid=NONE) while the pane and kiro-cli process still carry the correct
id. Every MCP tool then fails with "no CAO terminal context".

This module:

1. Writes a well-known per-pane (and per-window) identity file at terminal spawn.
2. At MCP server startup, recovers the terminal id from ancestor ``/proc``
   environ and/or those files, then applies it into ``os.environ`` so all
   existing ``os.environ.get("CAO_TERMINAL_ID")`` call sites work unchanged.

Remove or replace when the proper per-terminal MCP env passthrough lands.
Mark every call site with ``# HOTFIX F114`` so the later fix can find them.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.utils.temp_path import cao_tmp_dir

logger = logging.getLogger(__name__)

# HOTFIX F114
_TERMINAL_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_IDENTITY_KEYS = ("CAO_TERMINAL_ID", "CAO_INSTANCE_ID", "CAO_ENDPOINT")
_MAX_ANCESTOR_DEPTH = 32


def f114_tid_dir() -> Path:
    """HOTFIX F114: directory for per-pane / per-window terminal-id files."""
    path = cao_tmp_dir() / "f114-tid"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _pane_file_key(pane_id: str) -> str:
    # tmux pane ids look like "%42"; strip sigil for a safe filename.
    return re.sub(r"[^A-Za-z0-9._-]", "_", pane_id.lstrip("%"))


def _window_file_key(session_name: str, window_name: str) -> str:
    raw = f"{session_name}__{window_name}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw)


def write_terminal_id_fallback(
    *,
    terminal_id: str,
    pane_id: Optional[str] = None,
    session_name: Optional[str] = None,
    window_name: Optional[str] = None,
) -> None:
    """HOTFIX F114: persist terminal id for MCP processes that lose pane env."""
    if not terminal_id or not _TERMINAL_ID_RE.fullmatch(terminal_id):
        return
    base = f114_tid_dir()
    payload = terminal_id + "\n"
    targets: list[Path] = []
    if pane_id:
        targets.append(base / f"pane-{_pane_file_key(pane_id)}")
    if session_name and window_name:
        targets.append(base / f"win-{_window_file_key(session_name, window_name)}")
    if not targets:
        # Still write a terminal-keyed file (lookup needs another key, but keeps
        # a durable record for operators inspecting the hotfix dir).
        targets.append(base / f"tid-{terminal_id}")
    for path in targets:
        path.write_text(payload, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _read_proc_environ(pid: int) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return result
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key_b, value_b = item.split(b"=", 1)
        try:
            result[key_b.decode("utf-8")] = value_b.decode("utf-8")
        except UnicodeError:
            continue
    return result


def _read_ppid(pid: int) -> Optional[int]:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def _read_tid_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _TERMINAL_ID_RE.fullmatch(text):
        return text
    return None


def _lookup_file_by_pane(pane_id: str) -> Optional[str]:
    if not pane_id:
        return None
    return _read_tid_file(f114_tid_dir() / f"pane-{_pane_file_key(pane_id)}")


def _lookup_file_by_window(session_name: str, window_name: str) -> Optional[str]:
    if not session_name or not window_name:
        return None
    return _read_tid_file(
        f114_tid_dir() / f"win-{_window_file_key(session_name, window_name)}"
    )


def _walk_ancestors_for_identity() -> tuple[Optional[str], dict[str, str]]:
    """Walk parent processes for CAO identity env (skip self — env already checked)."""
    pid = _read_ppid(os.getpid())
    extras: dict[str, str] = {}
    depth = 0
    while pid is not None and pid > 1 and depth < _MAX_ANCESTOR_DEPTH:
        depth += 1
        env = _read_proc_environ(pid)
        tid = (env.get("CAO_TERMINAL_ID") or "").strip()
        if tid and _TERMINAL_ID_RE.fullmatch(tid):
            for key in _IDENTITY_KEYS:
                if key == "CAO_TERMINAL_ID":
                    continue
                val = (env.get(key) or "").strip()
                if val:
                    extras[key] = val
            return tid, extras
        # Also try file keyed by this ancestor's TMUX_PANE.
        pane = (env.get("TMUX_PANE") or "").strip()
        if pane:
            file_tid = _lookup_file_by_pane(pane)
            if file_tid:
                return file_tid, extras
        pid = _read_ppid(pid)
    return None, {}


def recover_and_apply_terminal_identity() -> Optional[str]:
    """HOTFIX F114: if CAO_TERMINAL_ID is missing, recover and set os.environ.

    Returns the resolved terminal id, or None if recovery failed.
    Safe to call multiple times; no-ops when env is already valid.
    """
    existing = (os.environ.get("CAO_TERMINAL_ID") or "").strip()
    if existing and _TERMINAL_ID_RE.fullmatch(existing):
        return existing

    # 1) File keyed by this process's TMUX_PANE (if any env survived).
    pane = (os.environ.get("TMUX_PANE") or "").strip()
    tid = _lookup_file_by_pane(pane) if pane else None
    extras: dict[str, str] = {}

    # 2) Ancestor /proc environ + ancestor TMUX_PANE files.
    if not tid:
        tid, extras = _walk_ancestors_for_identity()

    if not tid:
        logger.warning(
            "HOTFIX F114: could not recover CAO_TERMINAL_ID "
            "(env missing; ancestor walk and pane files empty)"
        )
        return None

    os.environ["CAO_TERMINAL_ID"] = tid
    for key, value in extras.items():
        if value and not (os.environ.get(key) or "").strip():
            os.environ[key] = value
    logger.info("HOTFIX F114: recovered CAO_TERMINAL_ID=%s", tid)
    return tid
