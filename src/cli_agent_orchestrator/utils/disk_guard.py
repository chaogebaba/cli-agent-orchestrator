"""Disk-space spawn guard (F619 / issue #475).

The 2026-08-30 incident: ``/`` filled to 100%, and the NEXT worker CAO spawned
truncated a source file mid-``str_replace`` (ENOSPC) and left git unable to
write ``index.lock``. A full disk should stop the fleet CLEANLY — refuse to
spawn — rather than let workers corrupt files as they hit ENOSPC.

This module is a thin, pure helper: it reads the ``[disk] min_free_gb`` knob
from ``providers.toml`` (via the shared settings loader) and calls
``shutil.disk_usage`` on the filesystem holding the worktree root and the logs
dir. It imports NO services beyond the settings loader and holds no state, so it
is safe to call from the MCP-server process (assign/handoff) and from the API
process (startup warning) alike.

Contract: :func:`check_spawn_disk` returns ``None`` when there is enough free
space, or a typed error string beginning ``E_DISK_LOW:`` (naming the offending
path and its free GB) when either checked filesystem is below the floor. Callers
refuse the spawn and surface that string verbatim.
"""

import logging
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR
from cli_agent_orchestrator.services.settings_service import get_disk_settings

logger = logging.getLogger(__name__)

E_DISK_LOW_PREFIX = "E_DISK_LOW:"

_BYTES_PER_GB = 1024 * 1024 * 1024


def _min_free_gb() -> float:
    """Resolve the configured spawn floor (GB) from ``[disk] min_free_gb``."""
    return float(get_disk_settings()["min_free_gb"])


def _existing_ancestor(path: Path) -> Optional[Path]:
    """Return ``path`` or its nearest existing ancestor, else ``None``.

    ``shutil.disk_usage`` requires an existing path. A worktree root that does
    not exist yet (about to be created) still lives on SOME mounted filesystem,
    so we walk up to the first existing ancestor to measure the right mount.
    """
    try:
        candidate = Path(path).resolve()
    except (OSError, RuntimeError):
        candidate = Path(path)
    for p in (candidate, *candidate.parents):
        if p.exists():
            return p
    return None


def _free_gb(path: Path) -> Optional[float]:
    """Free space (GB) on the filesystem holding ``path``; ``None`` if unknown.

    A failure to stat the mount (permissions, a vanished path) returns ``None``
    so the caller treats that path as unmeasurable and does NOT block on it —
    the guard must never refuse a spawn because it could not read a mount.
    """
    ancestor = _existing_ancestor(path)
    if ancestor is None:
        return None
    try:
        usage = shutil.disk_usage(str(ancestor))
    except OSError as e:
        logger.warning("disk_guard: cannot stat filesystem for %s: %s", path, e)
        return None
    return usage.free / _BYTES_PER_GB


def _low_paths(worktree_root: Optional[str]) -> List[Tuple[Path, float, float]]:
    """Return ``(path, free_gb, floor_gb)`` for each checked path below the floor.

    Checks the filesystem holding the worktree root (when given) AND the one
    holding the terminal-logs dir. Deduplicates by resolved path so a co-located
    worktree + logs dir is only reported once.
    """
    floor = _min_free_gb()
    checked: List[Path] = []
    if worktree_root:
        checked.append(Path(worktree_root))
    checked.append(TERMINAL_LOG_DIR)

    low: List[Tuple[Path, float, float]] = []
    seen: set = set()
    for path in checked:
        ancestor = _existing_ancestor(path)
        key = str(ancestor) if ancestor is not None else str(path)
        if key in seen:
            continue
        seen.add(key)
        free = _free_gb(path)
        if free is not None and free < floor:
            low.append((path, free, floor))
    return low


def check_spawn_disk(worktree_root: Optional[str]) -> Optional[str]:
    """Return an ``E_DISK_LOW:`` error string, or ``None`` when disk is fine.

    Refuses (returns the typed string) when free space on the filesystem holding
    ``worktree_root`` OR the logs dir is below ``[disk] min_free_gb``. The string
    names the offending path and its free GB so the operator can act:

        ``E_DISK_LOW: /home/user/proj has 3.1GB free (< 5GB floor)``

    Multiple offending paths are joined; the whole string still starts with the
    ``E_DISK_LOW:`` prefix so callers can branch on it programmatically.
    """
    low = _low_paths(worktree_root)
    if not low:
        return None
    parts = [f"{path} has {free:.1f}GB free (< {floor:g}GB floor)" for path, free, floor in low]
    return f"{E_DISK_LOW_PREFIX} " + "; ".join(parts)


def warn_if_disk_low_at_startup(worktree_root: Optional[str] = None) -> Optional[str]:
    """Log a WARNING (and return the string) if disk is already below the floor.

    Same check as :func:`check_spawn_disk`, but for the server-startup path: it
    only surfaces the condition in the boot log rather than refusing anything,
    so the operator sees a full disk at boot instead of first learning about it
    when the next assign/handoff is refused. Returns the ``E_DISK_LOW:`` string
    when low (for tests/callers), else ``None``.
    """
    result = check_spawn_disk(worktree_root)
    if result is not None:
        logger.warning("Startup disk check: %s", result)
    return result
