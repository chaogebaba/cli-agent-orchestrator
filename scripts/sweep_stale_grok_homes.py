#!/usr/bin/env python3
"""F312 AC4: One-shot sweep of stale GROK_HOME directories.

Removes private GROK_HOME directories under ~/.aws/cli-agent-orchestrator/grok/terminals
that have no corresponding live CAO terminal row in the database.

Usage:
    uv run python scripts/sweep_stale_grok_homes.py [--dry-run]

Safety:
    - Only removes directories under the managed root (never follows symlinks).
    - Dry-run mode (default) prints what WOULD be removed without deleting.
    - Pass --execute to actually remove stale homes.
"""

import argparse
import shutil
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep stale GROK_HOME directories.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually remove stale homes (default is dry-run).",
    )
    args = parser.parse_args()

    # Determine managed root
    cao_home = Path.home() / ".aws" / "cli-agent-orchestrator"
    managed_root = cao_home / "grok" / "terminals"

    if not managed_root.exists():
        print(f"Managed root does not exist: {managed_root}")
        return

    # Get live terminal IDs from the database
    live_terminal_ids: set[str] = set()
    try:
        from cli_agent_orchestrator.clients.database import list_all_terminals

        for row in list_all_terminals():
            live_terminal_ids.add(row["id"])
    except Exception as exc:
        print(f"ERROR: could not query database for live terminals: {exc}", file=sys.stderr)
        print("Ensure cao-server is importable (uv run) and the DB is accessible.", file=sys.stderr)
        sys.exit(1)

    # Walk managed root — each child directory is a terminal's GROK_HOME
    stale: list[Path] = []
    for child in sorted(managed_root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        # Extract terminal_id from directory name (format: slug-sha12)
        # We check if ANY live terminal maps to this directory
        found = False
        import hashlib
        import re

        for tid in live_terminal_ids:
            slug = re.sub(r"[^A-Za-z0-9_-]", "", tid)[:48]
            sha12 = hashlib.sha256(tid.encode()).hexdigest()[:12]
            expected_name = f"{slug}-{sha12}"
            if child.name == expected_name:
                found = True
                break
        if not found:
            stale.append(child)

    if not stale:
        print("No stale GROK_HOME directories found.")
        return

    print(f"Found {len(stale)} stale GROK_HOME director{'y' if len(stale) == 1 else 'ies'}:")
    for p in stale:
        print(f"  {p}")

    if not args.execute:
        print("\nDry-run mode. Pass --execute to remove these directories.")
        return

    removed = 0
    for p in stale:
        try:
            shutil.rmtree(p)
            removed += 1
            print(f"  REMOVED: {p}")
        except Exception as exc:
            print(f"  FAILED: {p} — {exc}", file=sys.stderr)

    print(f"\nRemoved {removed}/{len(stale)} stale homes.")


if __name__ == "__main__":
    main()
