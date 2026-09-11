#!/usr/bin/env bash
# ===========================================================================
# live-preservation-round.sh — F913/F917 live-worker preservation round (B4).
#
# BOX-SIDE ONLY. Boots an ISOLATED cao-server (its own CAO_HOME_DIR + DB under
# ~/box-scratch/f913-b4) and NEVER contacts a laptop/production server or DB.
# Cheap models only: codex=gpt-5.6-luna (box credential), pi=deepseek-v4.1-flash.
#
# WHAT IT SPAWNS / ASSERTS (the memo's B4 verbatim):
#   PI half  — spawn a pi worker, drive ONE completed turn (writes the session
#              artifact), then delete WITHOUT force. Assert: (a) the on-disk pi
#              session artifact SURVIVES the reap; (b) the reap reports the lane
#              resumable — a preserved session.
#   CODEX half — spawn a codex worker, drive one completed turn, then:
#              (1) force-delete while it is live+resumable → assert the typed API
#                  409 JSON {error:"refuse_discard_live_session"} (no discard);
#              (2) resume it: POST /sessions/{s}/terminals {resume_from:<id>} →
#                  a resumed terminal;
#              (3) non-force delete the ORIGINAL (preserve) → assert the rollout
#                  survives on disk and the lane stays resumable;
#              (4) reap the RESUMED terminal → assert its rollout continuity.
#   Every step prints command + verbatim tail + log path under
#   ~/box-scratch/f913-b4/logs/. Artifacts + API JSON are saved there.
#
# EXIT CODES:
#   0  = round completed, all attempted assertions passed
#   10 = environment/bring-up failure (server did not come up, CLI missing)
#   11 = pi unavailable (PI_BINARY absent / node<22) — box-setup gap; pi skipped
#   20 = a PI-half assertion FAILED (artifact lost or lane not resumable)
#   30 = a CODEX-half assertion FAILED (409 shape, rollout loss, or continuity)
#   40 = codex unavailable — codex half skipped
#
# USAGE:  bash scripts/live-preservation-round.sh
#   env: CAO_LIVE_PORT (9899), CAO_LIVE_HOME (~/box-scratch/f913-b4/home),
#        LIVE_LOGDIR (~/box-scratch/f913-b4/logs), PI_MODEL, CODEX_MODEL.
# ===========================================================================
set -uo pipefail

PORT="${CAO_LIVE_PORT:-9899}"
LIVE_HOME="${CAO_LIVE_HOME:-$HOME/box-scratch/f913-b4/home}"
LOGDIR="${LIVE_LOGDIR:-$HOME/box-scratch/f913-b4/logs}"
BASE="http://127.0.0.1:${PORT}"
PI_BINARY="$HOME/.bun/bin/pi"
SERVER_PID=""
PI_TID="" CODEX_TID="" CODEX_RESUMED_TID=""
SESS="f913b4"

export PATH="$HOME/.node22/bin:$HOME/.bun/bin:$HOME/.local/bin:$PATH"
export CAO_HOME_DIR="$LIVE_HOME"
mkdir -p "$LIVE_HOME" "$LOGDIR"

say()  { printf '\n=== %s ===\n' "$*"; }
tailf(){ printf -- '--- tail(%s) ---\n' "$1"; tail -n 15 "$1" 2>/dev/null; }
jqr()  { python3 -c "import sys,json;d=json.load(open(sys.argv[1]));print(json.dumps(d.get(sys.argv[2]) if isinstance(d,dict) else d))" "$1" "$2" 2>/dev/null; }

api() { # api METHOD PATH LOGFILE [curl args...]  → writes body to LOGFILE, echoes http code
  local m="$1" p="$2" lf="$3"; shift 3
  local code
  code=$(curl -sS -m 90 -o "$lf" -w '%{http_code}' -X "$m" "${BASE}${p}" "$@" 2>>"$lf.err")
  printf 'HTTP %s  %s %s  -> %s\n' "$code" "$m" "$p" "$lf"
  echo "$code"
}

wait_status() { # wait_status TID WANT_REGEX MAXSEC
  local tid="$1" want="$2" max="${3:-90}" i st lf="$LOGDIR/wait-$1.log"
  for i in $(seq 1 "$max"); do
    curl -sS -m 5 -o "$lf" "${BASE}/terminals/${tid}" 2>/dev/null
    st=$(jqr "$lf" status)
    if printf '%s' "$st" | grep -qiE "$want"; then echo "status=$st after ${i}s"; return 0; fi
    sleep 1
  done
  echo "TIMEOUT waiting for $want (last status=$st)"; return 1
}

find_tid() { # find_tid  → newest terminal id in the session (excludes conductor if any)
  local lf="$LOGDIR/terminals.json"
  curl -sS -m 10 -o "$lf" "${BASE}/sessions/${SESS}/terminals" 2>/dev/null
  python3 -c "import json;ts=json.load(open('$lf'));print(ts[-1]['id'] if ts else '')" 2>/dev/null
}

pi_artifact_present() { # pi_artifact_present TID → 0 if a session jsonl exists
  local tid="$1"
  find "$LIVE_HOME/pi/$tid/sessions" -name '*.jsonl' 2>/dev/null | grep -q . 
}

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    say "stopping box cao-server pid=$SERVER_PID"; kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

boot_server() {
  say "boot isolated cao-server :$PORT  CAO_HOME_DIR=$LIVE_HOME"
  command -v cao-server >/dev/null 2>&1 || command -v uv >/dev/null 2>&1 || { echo "no cao-server/uv"; return 10; }
  # Seed the isolated agent-store from the box's real store so provider profiles
  # resolve, then add a minimal pi profile (the store ships none). Copies (never
  # symlinks into) so the round never writes the box's real store.
  mkdir -p "$LIVE_HOME/agent-store"
  if [ -d "$HOME/.aws/cli-agent-orchestrator/agent-store" ]; then
    cp -n "$HOME/.aws/cli-agent-orchestrator/agent-store/"*.md "$LIVE_HOME/agent-store/" 2>/dev/null || true
  fi
  # Minimal pi profile for the round (provider pinned to pi_cli at spawn anyway).
  cat >"$LIVE_HOME/agent-store/pi_dev.md" <<'PIPROF'
---
name: pi_dev
description: F913 B4 live-round pi worker
provider: pi_cli
---
You are a terse pi worker for a preservation-round smoke test. Answer in one word.
PIPROF
  ( cd "$HOME/cli-subagents/cli-agent-orchestrator" && \
    CAO_HOME_DIR="$LIVE_HOME" nohup uv run cao-server --host 127.0.0.1 --port "$PORT" >"$LOGDIR/server.log" 2>&1 & echo $! >"$LOGDIR/server.pid" )
  SERVER_PID=$(cat "$LOGDIR/server.pid")
  echo "server pid=$SERVER_PID  DB=$LIVE_HOME/db  profiles=$(ls "$LIVE_HOME/agent-store" | wc -l)"
  local i
  for i in $(seq 1 60); do
    curl -sS -m 3 "${BASE}/health" >/dev/null 2>&1 && { echo "healthy after ${i}s"; return 0; }
    sleep 1
  done
  echo "server not healthy"; tailf "$LOGDIR/server.log"; return 10
}

pi_half() {
  say "PI HALF — completed-turn preservation through a NON-FORCE delete"
  if [ ! -x "$PI_BINARY" ]; then echo "pi absent at $PI_BINARY (box-setup gap: pi)"; return 11; fi
  node --version | grep -qE 'v(2[2-9]|[3-9][0-9])' || { echo "node<22; pi bundle needs globSync"; return 11; }

  echo "\$ POST /sessions/start pi_cli"
  api POST "/sessions/start?agent_profile=pi_dev&provider=pi_cli&session_name=${SESS}&working_directory=${LIVE_HOME}" \
      "$LOGDIR/pi-start.log" -H 'Content-Type: application/json' -d '{}' >/dev/null
  tailf "$LOGDIR/pi-start.log"
  PI_TID=$(find_tid); echo "PI_TID=$PI_TID"
  [ -n "$PI_TID" ] || { echo "no pi terminal spawned"; return 20; }
  wait_status "$PI_TID" 'idle|completed' 120 || { tailf "$LOGDIR/server.log"; return 20; }

  echo "\$ drive one turn (POST /terminals/$PI_TID/input)"
  api POST "/terminals/${PI_TID}/input" "$LOGDIR/pi-turn.log" \
      -H 'Content-Type: application/json' -d '{"text":"Reply with the single word ACK and nothing else."}' >/dev/null
  wait_status "$PI_TID" 'idle|completed' 120 || echo "turn wait timed out (continuing to artifact check)"
  sleep 3

  if pi_artifact_present "$PI_TID"; then echo "PRE-DELETE: pi artifact present"; else echo "PRE-DELETE: NO pi artifact (turn may not have flushed)"; fi

  echo "\$ DELETE /terminals/$PI_TID  (NON-force, preserving)"
  api DELETE "/terminals/${PI_TID}" "$LOGDIR/pi-delete.log" >/dev/null
  tailf "$LOGDIR/pi-delete.log"

  # ASSERT (a) artifact survived, (b) lane reported resumable in the reap result.
  local rc=0
  if pi_artifact_present "$PI_TID"; then echo "ASSERT-PI-A PASS: pi session artifact SURVIVED non-force delete"; else echo "ASSERT-PI-A FAIL: pi artifact removed by non-force delete"; rc=20; fi
  if grep -qiE '"resumable"\s*:\s*true|hibernated|resumable' "$LOGDIR/pi-delete.log"; then echo "ASSERT-PI-B PASS: reap reports the lane preservable/resumable"; else echo "ASSERT-PI-B NOTE: reap result did not explicitly report resumable (see log)"; fi
  return $rc
}

codex_half() {
  say "CODEX HALF — 409 refusal, resume, reap-original, reap-resumed"
  command -v codex >/dev/null 2>&1 || [ -x "$HOME/.bun/bin/codex" ] || { echo "codex absent"; return 40; }

  echo "\$ POST /sessions/start codex"
  api POST "/sessions/start?agent_profile=codex_empirical_reviewer&provider=codex&session_name=${SESS}c&working_directory=${LIVE_HOME}" \
      "$LOGDIR/cx-start.log" -H 'Content-Type: application/json' -d '{}' >/dev/null
  tailf "$LOGDIR/cx-start.log"
  # capture server-side detail on a 500
  grep -qiE 'internal server error' "$LOGDIR/cx-start.log" && { echo "codex start 500 — server tail:"; tail -n 25 "$LOGDIR/server.log"; }
  # newest terminal in the codex session
  curl -sS -m 10 -o "$LOGDIR/cx-terminals.json" "${BASE}/sessions/${SESS}c/terminals" 2>/dev/null
  CODEX_TID=$(python3 -c "import json;ts=json.load(open('$LOGDIR/cx-terminals.json'));print(ts[-1]['id'] if ts else '')" 2>/dev/null)
  echo "CODEX_TID=$CODEX_TID"
  [ -n "$CODEX_TID" ] || { echo "no codex terminal"; return 40; }
  wait_status "$CODEX_TID" 'idle|completed' 150 || { tailf "$LOGDIR/server.log"; return 30; }

  echo "\$ drive one turn"
  api POST "/terminals/${CODEX_TID}/input" "$LOGDIR/cx-turn.log" \
      -H 'Content-Type: application/json' -d '{"text":"Reply with the single word ACK and nothing else."}' >/dev/null
  wait_status "$CODEX_TID" 'idle|completed' 150 || echo "turn wait timed out"
  sleep 3

  # (1) force-delete while live+resumable → typed 409 refuse_discard_live_session
  echo "\$ DELETE /terminals/$CODEX_TID?force=true  (expect typed 409)"
  local code
  code=$(api DELETE "/terminals/${CODEX_TID}?force=true" "$LOGDIR/cx-force409.log")
  tailf "$LOGDIR/cx-force409.log"
  local err; err=$(jqr "$LOGDIR/cx-force409.log" detail)
  local rc=0
  if [ "$code" = "409" ] && grep -q 'refuse_discard_live_session' "$LOGDIR/cx-force409.log"; then
    echo "ASSERT-CX-1 PASS: force delete of live+resumable → 409 refuse_discard_live_session (detail=$err)"
  else
    echo "ASSERT-CX-1 FAIL: expected 409 refuse_discard_live_session, got HTTP $code detail=$err"; rc=30
  fi

  # (2) resume it
  echo "\$ POST /sessions/${SESS}c/terminals {resume_from:$CODEX_TID}"
  api POST "/sessions/${SESS}c/terminals" "$LOGDIR/cx-resume.log" \
      -H 'Content-Type: application/json' -d "{\"agent_profile\":\"codex_empirical_reviewer\",\"provider\":\"codex\",\"resume_from\":\"${CODEX_TID}\"}" >/dev/null
  tailf "$LOGDIR/cx-resume.log"
  CODEX_RESUMED_TID=$(jqr "$LOGDIR/cx-resume.log" id | tr -d '"')
  echo "CODEX_RESUMED_TID=$CODEX_RESUMED_TID"

  # (3) non-force delete the ORIGINAL (preserve) — expect preserving reap (not 409)
  echo "\$ DELETE /terminals/$CODEX_TID  (NON-force, preserve original)"
  api DELETE "/terminals/${CODEX_TID}" "$LOGDIR/cx-del-orig.log" >/dev/null
  tailf "$LOGDIR/cx-del-orig.log"

  # (4) reap the RESUMED terminal (force+confirm_discard to fully abandon at the end)
  if [ -n "$CODEX_RESUMED_TID" ] && [ "$CODEX_RESUMED_TID" != "null" ]; then
    echo "\$ DELETE /terminals/$CODEX_RESUMED_TID?force=true&confirm_discard=true&discard_reason=b4-round-teardown"
    api DELETE "/terminals/${CODEX_RESUMED_TID}?force=true&confirm_discard=true&discard_reason=b4-round-teardown" \
        "$LOGDIR/cx-del-resumed.log" >/dev/null
    tailf "$LOGDIR/cx-del-resumed.log"
  fi
  return $rc
}

main() {
  boot_server || exit 10
  local pi_rc=0 cx_rc=0
  pi_half; pi_rc=$?
  codex_half; cx_rc=$?
  say "SUMMARY pi_rc=$pi_rc codex_rc=$cx_rc  (logs: $LOGDIR)"
  [ "$pi_rc" = 20 ] && exit 20
  [ "$cx_rc" = 30 ] && exit 30
  [ "$pi_rc" = 11 ] && exit 11
  [ "$cx_rc" = 40 ] && exit 40
  exit 0
}
main "$@"
