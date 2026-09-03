"""Dead-code tombstone instrumentation — F261.

Records per-site execution evidence to a persistent JSONL ledger outside
any repository tree. Fail-open: never raises, never returns a value,
never slows the host.  Stdlib only.

Kill switch: ``CAO_TOMBSTONES=0`` disarms at import.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────

_ARMED: bool = os.environ.get("CAO_TOMBSTONES", "1") != "0"

_LEDGER: Path = (
    Path(
        os.environ.get("CAO_TOMBSTONE_DIR")
        or os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "cao-tombstones",
        )
    )
    / "fired.jsonl"
)

# ── Build identity (observed from the running module) ──────────────────

_MOD: Path = Path(__file__).resolve()
_BUILD: dict = {"root": str(_MOD.parents[1]), "mt": int(_MOD.stat().st_mtime)}

# ── Dedup (one record per site per process) ────────────────────────────

_seen: set[str] = set()

# ── Helpers ────────────────────────────────────────────────────────────


def _now() -> str:
    """ISO 8601 UTC timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ctx() -> str:
    """Classify the execution context as 'test' or 'prod'."""
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return "test"
    return "prod"


def _write(rec: dict) -> None:
    """Append one JSON line to the ledger.  Never raises."""
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(str(_LEDGER), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except BaseException:
        pass  # a tombstone never changes the program's behaviour


# ── Public API ─────────────────────────────────────────────────────────


def tombstone(site_id: str) -> None:
    """Record that *site_id* executed.  Returns None always; never raises."""
    if not _ARMED or site_id in _seen:
        return
    _seen.add(site_id)
    _write(
        {
            "k": "fire",
            "id": site_id,
            "ts": _now(),
            "ctx": _ctx(),
            "build": _BUILD,
            "pid": os.getpid(),
        }
    )


# ── Import-time exec witness (D8) ─────────────────────────────────────

if _ARMED:
    _write(
        {
            "k": "exec",
            "ts": _now(),
            "ctx": _ctx(),
            "build": _BUILD,
            "argv0": (sys.argv[0] if sys.argv else "")[:120],
        }
    )
