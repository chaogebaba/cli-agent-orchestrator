#!/usr/bin/env bash
set -euo pipefail

: "${CAO_WP2S1_SCRATCH_SESSION:?operator-created scratch session required}"
: "${CAO_WP2S1_SCRATCH_TERMINAL:?scratch Codex terminal required}"
: "${CAO_WP2S1_CALLER_TERMINAL:?scratch caller terminal required}"
: "${CAO_WP2S1_TOKEN:?unique token already established in native context required}"

root="$(git rev-parse --show-toplevel)"
out="$root/tmp/orch/drain-wp2s1/live-${CAO_WP2S1_SCRATCH_TERMINAL}"
mkdir -p "$out"
auth="${CODEX_HOME:-$HOME/.codex}/auth.json"
mtime_ref="$out/auth-mtime.ref"
touch "$mtime_ref"
touch -r "$auth" "$mtime_ref"
trap 'touch -r "$mtime_ref" "$auth"' EXIT

cao session manifest --session "$CAO_WP2S1_SCRATCH_SESSION" --json >"$out/manifest-before.json"
fifo="${CAO_HOME:-$HOME/.cao}/fifos/${CAO_WP2S1_SCRATCH_TERMINAL}.fifo"
[[ -p "$fifo" ]]
stat -Lc '%d:%i:%F' "$fifo" >"$out/fifo-before.txt"

# Queue the delivery control while the target is processing so recovery must
# preserve and later deliver the existing PENDING row.
cao session send "$CAO_WP2S1_SCRATCH_SESSION" \
  "Hold this token while working for several seconds: pending-$CAO_WP2S1_TOKEN" \
  --terminal "$CAO_WP2S1_SCRATCH_TERMINAL" --async
python - "$CAO_WP2S1_SCRATCH_TERMINAL" "$CAO_WP2S1_CALLER_TERMINAL" \
  "pending-control-$CAO_WP2S1_TOKEN" "$out/pending-message-id.txt" <<'PY'
import json
import sys
import urllib.parse
import urllib.request
terminal, sender, message, output = sys.argv[1:]
query = urllib.parse.urlencode({"sender_id": sender, "message": message})
body = urllib.request.urlopen(
    f"http://127.0.0.1:9889/terminals/{terminal}/inbox/messages?{query}", timeout=10
).read()
payload = json.loads(body)
open(output, "w", encoding="utf-8").write(str(payload["message_id"]))
PY

touch "$auth"
cao session recover "$CAO_WP2S1_SCRATCH_SESSION" --reason provider-reauth \
  --provider codex --terminal "$CAO_WP2S1_SCRATCH_TERMINAL" --interrupt --json \
  >"$out/recover.json"
cao session manifest --session "$CAO_WP2S1_SCRATCH_SESSION" --json >"$out/manifest-after.json"
stat -Lc '%d:%i:%F' "$fifo" >"$out/fifo-after.txt"
cmp "$out/fifo-before.txt" "$out/fifo-after.txt"

jq -e --arg id "$CAO_WP2S1_SCRATCH_TERMINAL" \
  '.results[] | select(.terminal_id == $id and .status == "rebound" and
   .interrupted_turn == true and .requires_supervisor_reconciliation == true)' \
  "$out/recover.json" >/dev/null
jq -e --arg id "$CAO_WP2S1_SCRATCH_TERMINAL" \
  --arg caller "$CAO_WP2S1_CALLER_TERMINAL" \
  --slurpfile old "$out/manifest-before.json" '
  .terminals[] | select(.id == $id) as $now |
  ($old[0].terminals[] | select(.id == $id)) as $before |
  $now.tmux_window == $before.tmux_window and
  $now.provider_session_id == $before.provider_session_id and
  $now.caller_id == $caller and $before.caller_id == $caller and
  $now.recovery_state == "rebound"' "$out/manifest-after.json" >/dev/null

cao session send "$CAO_WP2S1_SCRATCH_SESSION" \
  "Quote exactly both the original context token and pending-control token: $CAO_WP2S1_TOKEN" \
  --terminal "$CAO_WP2S1_SCRATCH_TERMINAL" --timeout 300 >"$out/context-proof.txt"
rg -F "$CAO_WP2S1_TOKEN" "$out/context-proof.txt" >/dev/null
rg -F "pending-control-$CAO_WP2S1_TOKEN" "$out/context-proof.txt" >/dev/null

# Persist DB evidence for UUID, caller, PENDING settlement, and watchdog/callback
# continuity (the final response must return through the unchanged caller).
cao messages trace "$(<"$out/pending-message-id.txt")" --json >"$out/message-trace.json"
jq -e '.message.status == "delivered"' "$out/message-trace.json" >/dev/null
cao session status "$CAO_WP2S1_SCRATCH_SESSION" \
  --terminal "$CAO_WP2S1_CALLER_TERMINAL" --json >"$out/caller-after.json"
touch -r "$mtime_ref" "$auth"
trap - EXIT
