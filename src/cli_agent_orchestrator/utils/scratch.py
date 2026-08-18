"""F273: Central scratch-directory resolver — /data/cao-scratch/tmp.

All project scratch (TMPDIR, suite locks, test artifacts, workflow output) MUST
route through this helper. /tmp is BANNED (7.7G RAM-backed tmpfs that eats swap
and OOM-kills lanes when bloated by parallel pytest runs).

The helper:
  1. Verifies /data is mounted (findmnt check).
  2. mkdir -p /data/cao-scratch/tmp.
  3. Returns the path.

When /data is absent, raises ``ScratchUnavailableError`` with a clear message
instructing the user to plug in /data. Never falls back to /tmp.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ScratchUnavailableError(RuntimeError):
    """Raised when /data is not mounted and scratch cannot be provisioned."""


_SCRATCH_ROOT = Path("/data/cao-scratch")
_SCRATCH_TMP = _SCRATCH_ROOT / "tmp"
_DATA_MOUNT = "/data"


def _is_data_mounted() -> bool:
    """Check if /data is mounted via findmnt (NOT df)."""
    try:
        result = subprocess.run(
            ["findmnt", "-rno", "TARGET", _DATA_MOUNT],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and _DATA_MOUNT in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def scratch_dir() -> Path:
    """Return /data/cao-scratch/tmp, creating it if needed.

    Raises:
        ScratchUnavailableError: When /data is not mounted.
    """
    if not _is_data_mounted():
        raise ScratchUnavailableError(
            "F273: /data is not mounted — ask the user to plug in /data "
            "(sudo systemctl start data.mount). /tmp fallback is banned."
        )
    _SCRATCH_TMP.mkdir(parents=True, exist_ok=True)
    return _SCRATCH_TMP


def scratch_dir_str() -> str:
    """Convenience: scratch_dir() as a string for env/subprocess use."""
    return str(scratch_dir())
