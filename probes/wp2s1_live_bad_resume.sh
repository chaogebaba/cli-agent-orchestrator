#!/usr/bin/env bash
set -euo pipefail

: "${CAO_WP2S1_SCRATCH_SESSION:?operator-created disposable scratch session required}"
: "${CAO_WP2S1_SCRATCH_TERMINAL:?disposable scratch Codex terminal required}"
: "${CAO_WP2S1_INDUCE_BAD_RESUME:?operator must explicitly install bad-resume fixture}"

root="$(git rev-parse --show-toplevel)"
out="$root/tmp/orch/drain-wp2s1/bad-${CAO_WP2S1_SCRATCH_TERMINAL}"
mkdir -p "$out"
cao session manifest --session "$CAO_WP2S1_SCRATCH_SESSION" --json >"$out/manifest-before.json"
fifo="${CAO_HOME:-$HOME/.cao}/fifos/${CAO_WP2S1_SCRATCH_TERMINAL}.fifo"
stat -Lc '%d:%i:%F' "$fifo" >"$out/fifo-before.txt"

cao session recover "$CAO_WP2S1_SCRATCH_SESSION" --reason provider-reauth \
  --provider codex --terminal "$CAO_WP2S1_SCRATCH_TERMINAL" --json >"$out/recover.json" || true
cao session manifest --session "$CAO_WP2S1_SCRATCH_SESSION" --json >"$out/manifest-after.json"
cao session status "$CAO_WP2S1_SCRATCH_SESSION" \
  --terminal "$CAO_WP2S1_SCRATCH_TERMINAL" --json >"$out/projected-status.json"
stat -Lc '%d:%i:%F' "$fifo" >"$out/fifo-after.txt"
cmp "$out/fifo-before.txt" "$out/fifo-after.txt"

jq -e --arg id "$CAO_WP2S1_SCRATCH_TERMINAL" '
  .results[] | select(.terminal_id == $id and .status == "resume_failed" and
  (.error_code != null))' "$out/recover.json" >/dev/null
jq -e '.conductor.status == "error"' "$out/projected-status.json" >/dev/null
jq -e --arg id "$CAO_WP2S1_SCRATCH_TERMINAL" --slurpfile old "$out/manifest-before.json" '
  .terminals[] | select(.id == $id) as $now |
  ($old[0].terminals[] | select(.id == $id)) as $before |
  $now.tmux_window == $before.tmux_window and
  $now.provider_session_id == $before.provider_session_id and
  $now.recovery_state == "rebind_failed"' "$out/manifest-after.json" >/dev/null

# A second explicit call proves raw retry preflight remains reachable; projected
# ERROR must not cause automatic teardown, approval, or quota interpretation.
cao session recover "$CAO_WP2S1_SCRATCH_SESSION" --reason provider-reauth \
  --provider codex --terminal "$CAO_WP2S1_SCRATCH_TERMINAL" --json \
  >"$out/retry.json" || true
