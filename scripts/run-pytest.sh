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

# F254 D26: honor CAO_TEST_WORKERS override (later -n wins over addopts).
WORKER_OVERRIDE=()
if [ -n "${CAO_TEST_WORKERS:-}" ]; then
    WORKER_OVERRIDE=(-n "$CAO_TEST_WORKERS")
    echo "[fence] CAO_TEST_WORKERS=$CAO_TEST_WORKERS — overriding addopts -n" >&2
fi

# TCACHE_EMITS_EFFECTIVE_ARGV — D23: tcache probes for this marker before key computation.
# Print effective argv (NUL-separated) and exit WITHOUT touching the suite lock.
if [[ "${1:-}" == "--tcache-print-effective-argv" ]]; then
    shift
    printf '%s\0' "$@" "${WORKER_OVERRIDE[@]}"
    exit 0
fi

# D4/AC4.2: flock removed — suite serialization now lives in the pytest layer
# (test/plugins/suite_slot.py). The wrapper is no longer the lock acquisition
# site; it remains only for the resource fence (systemd-run/nice).
echo "[fence] running pytest (slot lock held by pytest plugin)" >&2

# Resource fence: systemd-run --user --scope if available
if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope true >/dev/null 2>&1; then
    echo "[fence] systemd-run --user --scope -p CPUWeight=30 -p MemoryHigh=70% nice -n 10" >&2
    exec systemd-run --user --scope -p CPUWeight=30 -p MemoryHigh=70% nice -n 10 \
        uv run pytest "$@" "${WORKER_OVERRIDE[@]}"
else
    echo "[fence] WARNING: systemd-run unavailable — falling back to nice -n 10" >&2
    exec nice -n 10 uv run pytest "$@" "${WORKER_OVERRIDE[@]}"
fi
