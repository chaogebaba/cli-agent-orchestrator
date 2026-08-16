#!/usr/bin/env bash
# F237 — fenced pytest wrapper for tcache.
# Called by: tcache run <this-script> [extra-pytest-args…]
# The flock + systemd-run resource fence (F169) lives here so the entire
# traced tree (including pytest workers) is captured by strace -f.
#
# INVARIANT (D4/DR-4): This script MUST live exactly one directory level below
# the intended repo_root (the fork's top-level directory containing pyproject.toml).
# tcache resolves repo_root as dirname(suite_path)/.. — if this script moves
# deeper, tcache's read-set relativization breaks silently. The guard below
# catches the violation loudly.
set -euo pipefail

# Verify repo_root assumption: the parent of this script's directory must
# contain pyproject.toml (the fork's build config). Die loud if not.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
    echo "[run-pytest] FATAL: repo_root guard failed." >&2
    echo "  Expected pyproject.toml at: $REPO_ROOT/pyproject.toml" >&2
    echo "  This script must be exactly one level below the fork root." >&2
    exit 1
fi

SUITE_LOCK="/tmp/cao-suite.lock"

# Acquire exclusive flock (F169 serialization)
exec 9>"$SUITE_LOCK"
if ! flock -n 9 2>/dev/null; then
    echo "waiting for suite lock (another suite is running)..." >&2
    flock 9
fi
echo "lock acquired — running pytest" >&2

# Resource fence: systemd-run --user --scope if available
if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope true >/dev/null 2>&1; then
    echo "[fence] systemd-run --user --scope -p CPUWeight=30 -p MemoryHigh=70% nice -n 10" >&2
    exec systemd-run --user --scope -p CPUWeight=30 -p MemoryHigh=70% nice -n 10 \
        uv run pytest "$@"
else
    echo "[fence] WARNING: systemd-run unavailable — falling back to nice -n 10" >&2
    exec nice -n 10 uv run pytest "$@"
fi
