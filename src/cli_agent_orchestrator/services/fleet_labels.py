"""F483: Fleet-labels TSV management — write/remove rows atomically.

The fleet TUI (scripts/fleet-tui.py) reads ``/data/cao-scratch/fleet-labels.tsv``
with lines of exactly ``<terminal_id>\\t<label>`` (split("\\t", 1), id first).

This module provides the server-side write counterpart:
  - upsert_label(terminal_id, label) — append or update a row atomically.
  - remove_label(terminal_id) — remove the row for a terminal atomically.

All operations are fail-safe: a missing or unwritable TSV file must NEVER fail
an assign or delete_terminal call.

Path is configurable via ``CAO_FLEET_LABELS_PATH`` env (default
``/data/cao-scratch/fleet-labels.tsv``).
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

FLEET_LABELS_PATH = Path(
    os.environ.get("CAO_FLEET_LABELS_PATH", "/data/cao-scratch/fleet-labels.tsv")
)

# Max label length enforced at the API layer (spec: <=40 chars).
MAX_LABEL_LENGTH = 40


def _sanitize_label(label: str) -> str:
    """Clamp and sanitize the label (no tabs, no newlines, max 40 chars)."""
    # Strip tabs and newlines which would break the TSV format
    label = label.replace("\t", " ").replace("\n", " ").replace("\r", "")
    return label[:MAX_LABEL_LENGTH]


def upsert_label(terminal_id: str, label: str) -> None:
    """Append or update a label row for the given terminal.

    Atomic via write-to-temp + rename under an advisory flock.
    Never raises — logs and returns on any failure.
    """
    try:
        label = _sanitize_label(label)
        if not label:
            return
        tsv = FLEET_LABELS_PATH
        tsv.parent.mkdir(parents=True, exist_ok=True)
        _atomic_upsert(tsv, terminal_id, label)
    except Exception as exc:
        logger.debug("fleet_labels upsert_label failed (non-fatal): %s", exc)


def remove_label(terminal_id: str) -> None:
    """Remove the label row for a terminal.

    Atomic via write-to-temp + rename under an advisory flock.
    Never raises — logs and returns on any failure.
    """
    try:
        tsv = FLEET_LABELS_PATH
        if not tsv.exists():
            return
        _atomic_remove(tsv, terminal_id)
    except Exception as exc:
        logger.debug("fleet_labels remove_label failed (non-fatal): %s", exc)


def _atomic_upsert(tsv: Path, terminal_id: str, label: str) -> None:
    """Read-modify-write the TSV under an advisory lock."""
    lock_path = tsv.with_suffix(".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lines = _read_lines(tsv)
            # Replace existing row or append
            new_line = f"{terminal_id}\t{label}\n"
            found = False
            for i, line in enumerate(lines):
                if line.startswith(terminal_id + "\t") or line.rstrip("\n") == terminal_id:
                    lines[i] = new_line
                    found = True
                    break
            if not found:
                lines.append(new_line)
            _write_atomic(tsv, lines)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_remove(tsv: Path, terminal_id: str) -> None:
    """Remove rows matching terminal_id under an advisory lock."""
    lock_path = tsv.with_suffix(".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lines = _read_lines(tsv)
            filtered = [
                line
                for line in lines
                if not (line.startswith(terminal_id + "\t") or line.rstrip("\n") == terminal_id)
            ]
            if len(filtered) != len(lines):
                _write_atomic(tsv, filtered)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_lines(tsv: Path) -> list[str]:
    """Read TSV lines, returning empty list on missing file."""
    try:
        return tsv.read_text().splitlines(keepends=True)
    except OSError:
        return []


def _write_atomic(tsv: Path, lines: list[str]) -> None:
    """Write lines to a temp file then rename into place."""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(tsv.parent), prefix=".fleet-labels-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
        os.replace(tmp_path, str(tsv))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
