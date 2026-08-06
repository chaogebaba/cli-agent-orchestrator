#!/usr/bin/env bash
# V5 LIVE SANDBOX GATE — boot G7 sandbox on merged worktree, drive seams, purge.
# Hard wall: only :9890. NEVER production :9889.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
VENV_PY="$ROOT/.venv/bin/python"
VENV_CAO="$ROOT/.venv/bin/cao"
SANDBOX_ROOT="${SANDBOX_ROOT:-/home/chao/cao-sandbox-v5-gate}"
PORT="${PORT:-9890}"
: "${CAO_ARTIFACTS_DIR:?CAO_ARTIFACTS_DIR required}"
ART="$CAO_ARTIFACTS_DIR/v5-gate"
mkdir -p "$ART/evidence"
LOG="$ART/run.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== V5 GATE START $(date -Is) ==="
echo "ROOT=$ROOT SANDBOX_ROOT=$SANDBOX_ROOT PORT=$PORT ART=$ART"

cleanup() {
  local rc=$?
  echo "=== CLEANUP rc=$rc $(date -Is) ==="
  if [[ -d "$SANDBOX_ROOT" ]]; then
    "$VENV_PY" -B -m cli_agent_orchestrator.sandbox_bootstrap down --root "$SANDBOX_ROOT" --purge \
      >"$ART/sandbox-down.json" 2>"$ART/sandbox-down.err" || true
    echo "down:"; cat "$ART/sandbox-down.json" 2>/dev/null || true
    cat "$ART/sandbox-down.err" 2>/dev/null || true
  fi
  # Assert production still up and we never bound it
  if ss -ltn 2>/dev/null | rg -q ':9889\b'; then
    echo "production :9889 still listening (good)"
  else
    echo "WARN: production :9889 not listening after gate"
  fi
  exit "$rc"
}
trap cleanup EXIT

if ss -ltn 2>/dev/null | rg -q ":${PORT}\\b"; then
  echo "FATAL: port $PORT already in use"
  exit 2
fi
if [[ -d "$SANDBOX_ROOT" ]]; then
  echo "FATAL: sandbox root already exists: $SANDBOX_ROOT (purge manually if stale)"
  exit 2
fi

echo "=== SANDBOX UP (worktree venv) ==="
"$VENV_PY" -B -m cli_agent_orchestrator.sandbox_bootstrap up --root "$SANDBOX_ROOT" --port "$PORT" \
  | tee "$ART/sandbox-up.json"
"$VENV_PY" -B -m cli_agent_orchestrator.sandbox_bootstrap status --root "$SANDBOX_ROOT" \
  | tee "$ART/sandbox-status.json"

MANIFEST_PATH=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifest"])' "$ART/sandbox-up.json")
INSTANCE_ID=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["instance_id"])' "$MANIFEST_PATH")
ENDPOINT=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["endpoint"])' "$MANIFEST_PATH")
TMUX_SOCK=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["tmux_socket"])' "$MANIFEST_PATH")
echo "INSTANCE_ID=$INSTANCE_ID ENDPOINT=$ENDPOINT TMUX_SOCK=$TMUX_SOCK MANIFEST=$MANIFEST_PATH"

# Client env → sandbox only
export CAO_ENDPOINT="$ENDPOINT"
export CAO_INSTANCE_ID="$INSTANCE_ID"
export CAO_HOME_DIR="$SANDBOX_ROOT"
export CAO_TMUX_SOCKET="$TMUX_SOCK"
export CAO_SANDBOX_MANIFEST="$MANIFEST_PATH"
export CAO_TMP_DIR="$SANDBOX_ROOT/scratch"
export TMPDIR="$SANDBOX_ROOT/scratch"
export CAO_GRAPH_EXPORT_ROOT="$SANDBOX_ROOT/graph-exports"
eval "$(python3 - <<'PY' "$MANIFEST_PATH"
import tomllib, sys
m = tomllib.load(open(sys.argv[1], "rb"))
for row in m.get("providers", {}).values():
    if row.get("classification") == "shared-auth-read-only":
        print(f'export {row["home_env"]}={row["home"]!r}')
PY
)"

echo "=== HEALTH + CLI SMOKE ==="
curl -sfS "$ENDPOINT/health" | tee "$ART/health.json"
echo
"$VENV_CAO" --version | tee "$ART/cao-version.txt"
# session list against sandbox endpoint
"$VENV_CAO" session list 2>&1 | tee "$ART/session-list.txt" || true

# Isolated git project with CLAUDE.md that @includes a path OUTSIDE the project.
# Claude Code's "Allow external CLAUDE.md file imports?" dialog fires on external
# @includes (not merely parent CLAUDE.md files). Live seam1 needs that arm.
WORKDIR="$ART/claude-workspace/proj"
EXTERNAL_MD="$ART/claude-workspace/OUTSIDE/external-instructions.md"
mkdir -p "$WORKDIR" "$(dirname "$EXTERNAL_MD")"
printf '# external instructions — MUST NOT be imported after reject\nV5_EXTERNAL_MARKER_DO_NOT_APPLY\n' >"$EXTERNAL_MD"
printf '# project readme\n' >"$WORKDIR/README.md"
# CLAUDE.md with external @include (absolute path outside project)
printf '# project CLAUDE.md\n@%s\n' "$EXTERNAL_MD" >"$WORKDIR/CLAUDE.md"
# Fresh git root so Claude treats this as the project (not monorepo parent)
git -C "$WORKDIR" init -q
git -C "$WORKDIR" config user.email "v5gate@local"
git -C "$WORKDIR" config user.name "v5gate"
git -C "$WORKDIR" add -A
git -C "$WORKDIR" commit -q -m "v5 seam1 fixture"

echo "=== DRIVE SEAMS ==="
set +e
"$VENV_PY" "$ROOT/probes/v5-live-sandbox-gate/drive_v5_gate.py" \
  --endpoint "$ENDPOINT" \
  --session "v5gate" \
  --tmux-socket "$TMUX_SOCK" \
  --workdir "$WORKDIR" \
  --art "$ART" \
  --sandbox-root "$SANDBOX_ROOT" \
  --manifest "$MANIFEST_PATH"
RC=$?
set -e
echo "driver_rc=$RC"
exit "$RC"
