#!/usr/bin/env bash
set -euo pipefail

# Operator-owned gate-2 harness. This artifact is intentionally dark in CI:
# the build never installs or launches herdr. An operator may provide a
# pre-installed binary and an output path when running the real sandbox.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
OUT=${1:-"$ROOT/tmp/orch/0b1-shadow-window-$(date -u +%F).json"}

if ! command -v herdr >/dev/null 2>&1; then
  echo "NOT-RUN: herdr unavailable (gate 2 is operator-owned)" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")"
START_MONO=$(python -c 'import time; print(time.monotonic())')
START_WALL=$(date -u +%FT%H:%M:%SZ)
cat >"$OUT" <<JSON
{
  "status": "NOT-RUN",
  "window_start_mono": $START_MONO,
  "window_start_wall": "$START_WALL",
  "window_end_mono": null,
  "window_end_wall": null,
  "forced_kill_mono": null,
  "reconnect_grace_s": 30.0,
  "samples": [],
  "summary": {
    "expected_reconnect": 0,
    "expected_flush": 0,
    "mismatch": 0,
    "store_superior": 0
  }
}
JSON
echo "Shadow harness prepared at $OUT; scripted-agent window remains operator-owned."
