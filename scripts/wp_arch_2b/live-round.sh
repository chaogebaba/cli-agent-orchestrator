#!/usr/bin/env bash
# live-round.sh — WP-ARCH 2b slices 3+4, the box live round (plan §3 recipe).
#
# Runs the same scripted workload twice on one grok box, once with the status
# cutover OFF and once with it ON, and decides FLIP-READY-BOX: YES/NO from the two
# arms' coordination databases.
#
#   live-round.sh --box 007 --sha <fork-sha> [--out DIR] [--turns N] [--min-seconds S]
#                 [--providers codex] [--lanes "codex claude_code kiro_cli"]
#
# --providers is CAO_WORKER_TRUTH_STATUS_PROVIDERS on the on arm: the allowlist,
# and therefore which lanes are projected.  --lanes is which worker terminals to
# create.  They are separate because the round needs BOTH: at least one lane the
# allowlist names (the projection publishes for it) and at least one it does not
# (the control that proves the fallback was demoted rather than damaged).
#
# Both arms run on ONE box, in sequence, under one lease.  One box because the
# comparison is between the arms and a second box would put a different root
# checkout, a different fleet and a different clock into it; in sequence because
# the two arms share a port and a CAO home.
#
# What the caller gets back, under --out (default
# /data/claude-scratch/cli-subagents/2b/live-round-<sha>):
#   off/{server.log,fleet.json,db}   the off arm's artefacts
#   on/{server.log,fleet.json,fleet-series.jsonl,db}
#   report.txt                       one PASS/FAIL/SKIP line per check, verdict last
#
# Exit status is the verdict: 0 for FLIP-READY-BOX: YES, 1 for NO, 2 for a harness
# failure (the round did not run).  A SKIP is never a pass — a criterion whose
# workload did not happen has not been met, and the verdict says NO.
#
# NOT RUN BY THE BUILDER.  The checks and the analyser are exercised (see
# report-s4.md); the round itself is the lead's to run once the reviews clear.
set -uo pipefail

BOX=""
SHA=""
OUT=""
TURNS=40
MIN_SECONDS=1800
SESSION="cao-2b-live"
PROVIDERS="codex"
LANE_PROVIDERS="codex claude_code kiro_cli"
# D9: the position whose (position, provider) cells the certification rows
# certify.  Only used when --certification-dir is given; the flat built-in
# profiles have no position at all (see the launch comment below).
POSITION="developer"
CERT_DIR=""
# How long a lane gets to answer ONE probe turn before the arm is abandoned.
READY_SECONDS=300
# THE UNSOURCED CONTROL (AC-2b case 6).  One extra lane of this provider, spawned
# under the FLAT built-in profile, which can never be certified: a flat name has
# no position, so split_effective_name returns None and the D9 predicate is never
# even consulted (utils/agent_profiles.py:1333, measured).  It is therefore
# unsourced BY CONSTRUCTION rather than by an accident of credentials.
#
# It used to be the kiro lane, which made the control hostage to kiro's login on
# whichever box the round landed on -- and kiro was logged out on two of them.
# The control does not need to be certified or allowlisted; it needs to be ALIVE
# and unsourced, so the provider that is already working on the box is the right
# one to use twice.  Empty disables it.
FLAT_CONTROL="claude_code"
PORT=9889
BOXHOME=/workspace/cao/home
REMOTE_SCRATCH='/workspace/cao/home/box-scratch/2b-live'
ANALYSER="$(dirname "$0")/live-round-analyse.py"

die() { printf '%s\n' "$*" >&2; exit 2; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --box) BOX="${2:-}"; shift 2 ;;
    --sha) SHA="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --turns) TURNS="${2:-}"; shift 2 ;;
    --min-seconds) MIN_SECONDS="${2:-}"; shift 2 ;;
    --providers) PROVIDERS="${2:-}"; shift 2 ;;
    --lanes) LANE_PROVIDERS="${2:-}"; shift 2 ;;
    --position) POSITION="${2:-}"; shift 2 ;;
    --certification-dir) CERT_DIR="${2:-}"; shift 2 ;;
    --ready-seconds) READY_SECONDS="${2:-}"; shift 2 ;;
    --flat-control) FLAT_CONTROL="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$BOX" ] || usage
[ -n "$SHA" ] || usage
[ "$BOX" != "001" ] || die "grok-box-001 is frozen (another repo owns it)"
[ -f "$ANALYSER" ] || die "analyser not found beside this script: $ANALYSER"
findmnt /data >/dev/null 2>&1 || die "/data is not mounted; plug the SSD in before running a round"
command -v grokfleet >/dev/null 2>&1 || die "grokfleet is not on PATH"

# NO BACKTICK COMMAND SUBSTITUTION IN THIS FILE.  The remote payload is built by
# an UNQUOTED heredoc, because it has to expand the box paths and the sha on the
# laptop.  An unquoted heredoc also expands backquotes, so a single backtick in a
# COMMENT inside that heredoc is not punctuation: it is a command, and it runs
# HERE, on the laptop, every time a payload is built.  One did.  An RST comment
# reading "cao launch" in single backticks executed ``cao launch`` with no
# arguments on the laptop during the 2026-09-16 round 3, which is where that
# round's unexplained "Error: Missing option '--agents'" came from.  A doubled
# backtick is an empty substitution and is safe; a lone one is not, and the next
# one may not be a command that merely prints a usage message.  Refuse to run.
_tick=$(printf '\140')
if grep -qP "(?<!$_tick)$_tick(?!$_tick)" "$0" 2>/dev/null; then
  die "$0 contains a backtick that is not part of a doubled pair; it would EXECUTE when the payload heredoc expands. Use \$(...) for substitution and double backticks in prose."
fi

# THE CERTIFIED COHORT (D9).  Without this directory the round cannot observe
# the projected path at all, and that is not a tuning detail — it is the whole
# criterion.  Publication happens only for a terminal whose source the herdr
# backend registered authoritative (backends/herdr_backend.py:188), which needs
# ``terminal_certified``, which needs BOTH of the things this option supplies:
#
#   * a spawn name the D9 resolver can split into <position>-<provider>
#     (utils/agent_profiles.py:split_effective_name) — the built-in flat
#     ``developer`` / ``code_supervisor`` names split to None and are therefore
#     never certified, which is correct and is why round 3 (2026-09-16) recorded
#     an ON arm identical to its OFF arm, producer=pane in both; and
#   * a positions store carrying a PASS ``herdr_certification:`` row for that cell
#     at the CURRENT position_sha/overlay_sha, pinned to the sha256 of the herdr
#     binary on the box (utils/routing.py:herdr_cell_certified).
#
# The rows are written from a certification probe's evidence, never by this
# script: a round that certified its own cells in order to observe the certified
# path would be certifying by fiat, and its YES would mean nothing.
COMPOSED=""
if [ -n "$CERT_DIR" ]; then
  [ -d "$CERT_DIR" ] || die "--certification-dir $CERT_DIR is not a directory"
  [ -d "$CERT_DIR/positions" ] || die "--certification-dir $CERT_DIR has no positions/ subdirectory"
  [ -f "$CERT_DIR/positions/$POSITION.md" ] ||
    die "--certification-dir $CERT_DIR has no positions/$POSITION.md for --position $POSITION"
  grep -q 'herdr_certification' "$CERT_DIR/positions/$POSITION.md" ||
    die "positions/$POSITION.md carries no herdr_certification rows; the round would run uncertified"
  COMPOSED=1
else
  echo "NOTE: no --certification-dir given. No lane can be herdr-certified, so the" >&2
  echo "      ON arm cannot publish through the projection and every projected" >&2
  echo "      criterion will SKIP. This round cannot answer FLIP-READY-BOX: YES." >&2
fi

# The spawn names each arm will use, decided ONCE here so the payload, the Kiro
# manifest seeding and the composed-profile materialisation cannot disagree.
if [ -n "$COMPOSED" ]; then
  SUPERVISOR_PROFILE="$POSITION-claude_code"
  KIRO_PROFILE="$POSITION-kiro_cli"
else
  SUPERVISOR_PROFILE="code_supervisor"
  KIRO_PROFILE="developer"
fi

OUT="${OUT:-/data/claude-scratch/cli-subagents/2b/live-round-$SHA}"
mkdir -p "$OUT/on" "$OUT/off" || die "cannot create $OUT"

# ---------------------------------------------------------------------------
# The remote payload.
#
# One arm: a clean CAO home, the fork at $SHA, a server with this arm's switch
# position, three lanes, the scripted workload, then a stop and a copy of the
# coordination database.  The two arms differ ONLY in the two environment
# variables, which is what makes the comparison mean anything.
#
# The fleet is captured three ways: once at the end (status_since), as a series
# during the run (the condition label's lifetime), and implicitly in the event
# log (everything else).
# ---------------------------------------------------------------------------
remote_arm() {
  local arm="$1" status_env="$2" providers_env="$3"
  cat <<REMOTE
set -uo pipefail
ARM=$arm
ARM_SESSION=$SESSION-$arm
# Decided on the laptop (see the --certification-dir block) and carried in, so
# the manifest seeding, the launch and the lane POSTs cannot drift apart.
COMPOSED_NAMES=$COMPOSED
POSITION=$POSITION
SUPERVISOR_PROFILE=$SUPERVISOR_PROFILE
FLAT_CONTROL=$FLAT_CONTROL
# The profile a lane spawns under.  A composed <position>-<provider> name is the
# ONLY shape D9 can certify; the flat built-in name is the uncertified fallback
# for a round run without a certification store.
lane_profile() {
  if [ -n "\$COMPOSED_NAMES" ]; then printf '%s-%s' "\$POSITION" "\$1"; else printf 'developer'; fi
}
ROUND=$REMOTE_SCRATCH/\$ARM
rm -rf "\$ROUND"; mkdir -p "\$ROUND"
export CAO_HOME_DIR="\$ROUND/home"
export CAO_WORKER_TRUTH_INGEST=1
# The analyser must distinguish a herdr-backed cohort from an ordinary tmux
# arm; record the seam precondition explicitly rather than infer it later.
export CAO_HERDR_RUNTIME=1
export CAO_WORKER_TRUTH_STATUS=$status_env
export CAO_WORKER_TRUTH_STATUS_PROVIDERS=$providers_env
mkdir -p "\$CAO_HOME_DIR"

# THE WORKLOAD HOME.  A box's login home (/home/box) is not where its work
# lives: box-setup puts the repo, the provider credentials, the CLI install dirs
# and the ``.provisioned`` sentinel under $BOXHOME=/workspace/cao/home, and
# box-run.sh sets HOME to it for exactly this reason.  Without this line every
# probe reads the wrong home — the CLIs look MISSING on a fully provisioned box,
# ``uv tool install`` puts ``cao-server`` somewhere the round then cannot find,
# and the provider panes have no credentials.  One line, and it is the
# difference between a round and an afternoon.
export HOME=$BOXHOME

# The PATH the provider spawns need, and the reason this line exists at all:
# ``cao-server`` creates each provider terminal in a tmux shell that inherits the
# SERVER's environment, and the provider binaries live in three per-user dirs
# that a non-login shell does not have.  Without this the server starts, the
# launch is accepted, and the provider pane sits at ``command not found`` until
# the 180 s init timeout — which is what "the round produced no lanes" looks
# like.  Lifted from scripts/box-e2e-launch.sh, where five gate rounds put it.
# herdr is installed in the box login prefix, while CAO and provider CLIs
# are installed under the workload home.  Keep both prefixes: the round sets
# HOME to the workload home for credentials, but herdr itself remains at
# /home/box/.local/bin on provisioned images.
export PATH="\$HOME/.bun/bin:\$HOME/.local/bin:\$HOME/.grok/bin:/home/box/.local/bin:/home/box/.bun/bin:\$PATH"
# The built-in CAO ``developer`` profile is enough for most providers, but
# Kiro also requires a matching base agent manifest.  Provisioned boxes carry
# the repo's kiro_dev manifest; seed the generic name used by this harness.
# The manifest is looked up by the AGENT PROFILE NAME the provider was spawned
# under (providers/kiro_cli.py:602 falls back to kiro_default and says so), so a
# composed spawn name needs a manifest under the COMPOSED name.  Seeding only
# "developer" here is how a composed kiro lane would quietly run as kiro_default.
for _kiro_name in $KIRO_PROFILE developer; do
  if [ ! -f "\$HOME/.kiro/agents/\$_kiro_name.json" ] &&
     [ -f "\$HOME/.kiro/agents/kiro_dev.json" ]; then
    mkdir -p "\$HOME/.kiro/agents"
    cp "\$HOME/.kiro/agents/kiro_dev.json" "\$HOME/.kiro/agents/\$_kiro_name.json"
  fi
done

cd $BOXHOME/cli-subagents/cli-agent-orchestrator || exit 2
git fetch origin >/dev/null 2>&1
git switch --detach $SHA >/dev/null 2>&1 || { echo "HARNESS: sha $SHA not found"; exit 2; }
uv tool install --force --python 3.14 . >/dev/null 2>&1 || { echo "HARNESS: install failed"; exit 2; }

# One arm at a time on one port: kill anything already bound, or this arm reads
# the PREVIOUS arm's server and the A/B compares a build with itself.
for pid in \$(pgrep -f cao-server 2>/dev/null || true); do kill "\$pid" 2>/dev/null || true; done
sleep 2
# The arm's OWN session by name first, then the server.  A bare ``kill-server``
# alone left a session from a previous round alive on the box, and the only
# symptom was ``cao launch`` answering 400 "Session already exists" — after
# which the arm carried on with NO supervisor terminal and the round looked like
# it ran.  Named kill, then server kill, then VERIFY.
tmux kill-session -t "\$ARM_SESSION" 2>/dev/null || true
tmux kill-server 2>/dev/null || true
sleep 2
if tmux has-session -t "\$ARM_SESSION" 2>/dev/null; then
  echo "HARNESS: session \$ARM_SESSION survived cleanup; refusing to run a polluted arm"
  exit 2
fi

# Herdr has persistent workspaces rather than tmux sessions.  Killing
# cao-server does not remove one, so an aborted arm otherwise makes the next
# ``cao launch`` answer "Session already exists" even though its port is clean.
# Close only this harness arm's exact label; never sweep another workspace.
python3 - "\$ARM_SESSION" <<'CLEAN_HERDR'
import json
import subprocess
import sys

target = sys.argv[1]
try:
    raw = subprocess.check_output(
        ["herdr", "--session", "cao", "workspace", "list"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    body = json.loads(raw)
except Exception:
    raise SystemExit(0)
for workspace in body.get("result", {}).get("workspaces", []):
    if workspace.get("label") == target:
        subprocess.run(
            ["herdr", "--session", "cao", "workspace", "close", workspace["workspace_id"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
CLEAN_HERDR

# Seed providers.toml the way install.sh does — copy the default only when the
# arm's fresh home has none — so the provider model defaults are the repo's
# rather than whatever a bare home implies.
if [ ! -f "\$CAO_HOME_DIR/providers.toml" ] && [ -f "$BOXHOME/cli-subagents/providers.toml.default" ]; then
  cp "$BOXHOME/cli-subagents/providers.toml.default" "\$CAO_HOME_DIR/providers.toml"
fi

# The certified cohort's inputs, into THIS arm's fresh home: positions/ and
# overlays/ under the agent store, exactly where positions_store_dir() and
# herdr_cell_certified() read them (constants.py:413-437).  Both arms install
# the same bytes, so the two arms differ only in the two switch variables.
if [ -n "\$COMPOSED_NAMES" ]; then
  [ -d "$REMOTE_SCRATCH-certification/positions" ] || {
    echo "HARNESS: the certification store is missing on the box"; exit 2; }
  mkdir -p "\$CAO_HOME_DIR/agent-store"
  cp -R "$REMOTE_SCRATCH-certification/." "\$CAO_HOME_DIR/agent-store/" || {
    echo "HARNESS: could not install the certification store"; exit 2; }

  # A composed name resolves through the composed STORE first
  # (utils/agent_profiles.py:748-762), and that store is written by the D8
  # materialiser at assign time.  This round passes the composed name straight
  # to the API, so nothing would materialise it and every spawn would 404 on a
  # profile that "exists" only as a position plus an overlay.  Build them with
  # the product's OWN writer rather than by hand: the composed body has to be
  # the one the certification shas were computed over, and a second composer
  # here is exactly how that drifts.
  : > "\$ROUND/composed-names.txt"
  printf '%s %s\n' "\$SUPERVISOR_PROFILE" "claude_code" >> "\$ROUND/composed-names.txt"
  for provider in $LANE_PROVIDERS; do
    printf '%s %s\n' "\$(lane_profile \$provider)" "\$provider" >> "\$ROUND/composed-names.txt"
  done
  sort -u "\$ROUND/composed-names.txt" -o "\$ROUND/composed-names.txt"
  uv run python - "\$ROUND/composed-names.txt" <<'MATERIALISE' >> "\$ROUND/launch.log" 2>&1
import sys
from pathlib import Path

from cli_agent_orchestrator.utils.agent_profiles import materialise_composed_profile

built, failed = [], []
for line in Path(sys.argv[1]).read_text().splitlines():
    parts = line.split()
    if len(parts) != 2:
        continue
    spawn_name, provider = parts
    try:
        target = materialise_composed_profile(spawn_name, provider)
    except Exception as exc:
        failed.append(f"{spawn_name}: {type(exc).__name__}: {exc}")
        continue
    if target is None:
        failed.append(f"{spawn_name}: no composed profile could be built from the store")
    else:
        built.append(f"{spawn_name} -> {target}")
print("composed profiles: " + ("; ".join(built) if built else "none"))
if failed:
    print("COMPOSE-FAIL: " + "; ".join(failed))
    raise SystemExit(1)
MATERIALISE
  if [ \$? -ne 0 ]; then
    echo "HARNESS: composed profiles could not be materialised; see \$ROUND/launch.log"
    exit 2
  fi
fi

# The server, without systemd (boxes have none).  ``cao-server`` is the console
# script the unit's ExecStart runs; ``cao server`` is not a command, and starting
# nothing is what "server never came up" looked like on the first attempt.
# Started WITHOUT a subshell so ``\$!`` is this arm's server pid.  The teardown
# below needs to stop exactly this process: the box is shared, and a pattern
# kill there has already taken out another lane's server by accident.
SERVER_PID=""
SERIES_PID=""
cleanup_arm() {
  [ -n "\$SERIES_PID" ] && kill "\$SERIES_PID" 2>/dev/null || true
  [ -n "\$SERVER_PID" ] && kill "\$SERVER_PID" 2>/dev/null || true
}
# A lane/auth failure is a harness failure, but it must not leave a server
# bound to the shared port for the next arm or another box user.
trap cleanup_arm EXIT
cao-server --terminal herdr >"\$ROUND/server.log" 2>&1 &
SERVER_PID=\$!
for _ in \$(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
health_json=\$(curl -sf "http://127.0.0.1:$PORT/health" 2>/dev/null || true)
[ -n "\$health_json" ] || { echo "HARNESS: server never came up"; exit 2; }
python3 -c 'import json,sys; data=json.loads(sys.argv[1]); expected="herdr"; actual=data.get("terminal_backend"); sys.exit(0 if actual == expected else 1)' "\$health_json" \
  || { echo "HARNESS: server backend is not herdr: \$health_json"; exit 2; }
echo "backend: herdr" >> "\$ROUND/server.log"

# Three lanes: a codex (the allowlisted provider and the only one with a
# rollout source), a claude_code (the second source), and a kiro — the
# UNSOURCED lane, which is the control that proves the fallback was demoted
# rather than damaged (AC-2b case 6).
# BUILT-IN profiles (``code_supervisor``, ``developer``), not the repo's.  A box
# has no agent-store, and the repo's composed profiles need ``cao install`` to
# probe a running server for resolver support — a bring-up of its own.  The
# built-ins ship inside the package and resolve with nothing installed, which is
# all this round needs: the lanes exist to produce transitions, not to work.
cao launch --agents "\$SUPERVISOR_PROFILE" --provider claude_code \\
  --session-name \$ARM_SESSION --headless --yolo >>"\$ROUND/launch.log" 2>&1

# The launch must actually have produced the session's first terminal.  It
# answered 400 once (a stale session, above) and the arm ran on regardless, with
# a lane list short by one and every comparison quietly narrower.  A launch that
# did not launch is a harness failure, not a smaller round.
if ! grep -q 'Terminal created:' "\$ROUND/launch.log" 2>/dev/null; then
  echo "HARNESS: cao launch created no terminal; see \$ROUND/launch.log"
  exit 2
fi

# Worker lanes through the API: ``assign`` is an MCP tool, not a CLI verb.
# codex is the allowlisted provider and the only one with a rollout source;
# kiro is the UNSOURCED control that proves the fallback was demoted rather than
# damaged (AC-2b case 6).
for provider in $LANE_PROVIDERS; do
  # ``POST /sessions/<name>/terminals`` ADDS a lane to the session the launch
  # created.  ``POST /sessions`` creates a session and answers
  # "Session already exists" here, which is what the first attempt hit.
  curl -s --max-time 420 -o "\$ROUND/lane-\$provider.json" -w "%{http_code}" -X POST \\
    "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/terminals?agent_profile=\$(lane_profile \$provider)&provider=\$provider&working_directory=$BOXHOME/cli-subagents/cli-agent-orchestrator" \\
    -H 'content-type: application/json' -d '{}' >>"\$ROUND/launch.log" 2>&1
  echo " <- \$provider lane" >>"\$ROUND/launch.log"
done

# THE UNSOURCED CONTROL, added only when the other lanes are composed: in a flat
# round every lane is already uncertified and a second one would prove nothing.
# Same provider as a working lane, different PROFILE -- that is the whole trick,
# and it is why this lane needs no credentials of its own.
if [ -n "\$COMPOSED_NAMES" ] && [ -n "\$FLAT_CONTROL" ]; then
  curl -s --max-time 420 -o "\$ROUND/lane-flat-control.json" -w "%{http_code}" -X POST \\
    "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/terminals?agent_profile=developer&provider=\$FLAT_CONTROL&working_directory=$BOXHOME/cli-subagents/cli-agent-orchestrator" \\
    -H 'content-type: application/json' -d '{}' >>"\$ROUND/launch.log" 2>&1
  echo " <- \$FLAT_CONTROL UNSOURCED CONTROL (flat profile 'developer')" >>"\$ROUND/launch.log"
fi
# Same rule for the worker lanes.  ``pi_cli`` answered 500 ("startup error
# banner") on a box where ``pi`` was installed but broken, and the round
# continued with the control lane missing — which is precisely the lane the
# unsourced comparisons need.
for provider in $LANE_PROVIDERS; do
  if ! grep -q '^201' "\$ROUND/lane-\$provider.json" 2>/dev/null &&
     ! python3 -c "import json,sys; json.load(open(sys.argv[1]))['id']" \\
       "\$ROUND/lane-\$provider.json" >/dev/null 2>&1; then
    echo "HARNESS: lane \$provider did not start; \$(head -c 200 "\$ROUND/lane-\$provider.json" 2>/dev/null)"
    exit 2
  fi
done
# The control is checked exactly like the others.  A control lane that silently
# failed to start is the same defect as a missing workload lane: the criterion it
# carries would SKIP, and a SKIP is never a pass.
if [ -n "\$COMPOSED_NAMES" ] && [ -n "\$FLAT_CONTROL" ]; then
  if ! grep -q '^201' "\$ROUND/lane-flat-control.json" 2>/dev/null &&
     ! python3 -c "import json,sys; json.load(open(sys.argv[1]))['id']" \\
       "\$ROUND/lane-flat-control.json" >/dev/null 2>&1; then
    echo "HARNESS: the unsourced control lane (\$FLAT_CONTROL, flat profile) did not start;"
    echo "HARNESS: \$(head -c 200 "\$ROUND/lane-flat-control.json" 2>/dev/null)"
    exit 2
  fi
fi
sleep 20

# The fleet series: one snapshot per heartbeat for the length of the workload,
# which is what bounds the condition label's lifetime (AC-2b case 3).
# Capture the raw fleet body first, then reduce it.  The reduction used to run
# in a pipeline whose failure was swallowed by ``|| echo "[]"``, so a fleet call
# that errored was indistinguishable from a fleet with no lanes — every row of
# the first round's series parsed as empty with no way to tell which.
( for _ in \$(seq 1 $((TURNS + 10))); do
    _raw=\$(curl -s --max-time 30 "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/fleet")
    printf '%s\n' "\$_raw" > "\$ROUND/fleet-last-raw.json"
    printf '{"at": "%s", "rows": %s}\n' "\$(date -Is)" \\
      "\$(printf '%s' "\$_raw" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    body = json.loads(raw)
except Exception as exc:
    print(json.dumps([{"series_error": str(exc), "raw": raw[:200]}]))
else:
    print(json.dumps(body["terminals"] if isinstance(body, dict) else body))
')" \\
      >> "\$ROUND/fleet-series.jsonl"
    sleep 20
  done ) &
SERIES_PID=\$!

# The workload.  Same script in both arms, and deliberately dull: turns that
# end, one search whose exit 1 must NOT become a condition (#613), and one
# message quoting an exit line, which must not be read back as the worker's own
# evidence (#545).
# ``/fleet`` answers an OBJECT with a ``terminals`` key, not a bare list — the
# first run of this round drove nothing at all because the parser assumed a list
# and silently produced no lanes.
LANES=\$(curl -sf "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/fleet" | python3 -c '
import json, sys
body = json.load(sys.stdin)
rows = body["terminals"] if isinstance(body, dict) else body
print(" ".join(r["id"] for r in rows))
' 2>/dev/null)
if [ -z "\$LANES" ]; then echo "HARNESS: the session has no lanes; see launch.log"; exit 2; fi
echo "lanes: \$LANES"

# id -> provider, because the cap drive below is provider-specific: the CAPPED
# classifier is banner-only and each provider has its OWN banner regex
# (providers/condition.py:760-772).  Driving the wrong one produces nothing and
# the check SKIPs on a round that looked like it ran.
curl -sf "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/fleet" | python3 -c '
import json, sys
body = json.load(sys.stdin)
rows = body["terminals"] if isinstance(body, dict) else body
for row in rows:
    print(row["id"], row.get("provider") or "")
' > "\$ROUND/lane-providers.txt" 2>/dev/null || true
cat "\$ROUND/lane-providers.txt"
ARM_STARTED=\$(date +%s)

# ``message`` on POST /terminals/{id}/input is a QUERY parameter, not a JSON
# body: FastAPI binds a bare ``str`` argument to the query string
# (api/main.py:5763-5770).  Posting it as JSON answers HTTP 422 and sends
# nothing — which is what silently made the first two rounds a no-op, six
# transitions in twenty minutes.  Send it urlencoded on the URL, and record any
# non-2xx rather than swallowing it with ``curl -sf``.
send() {
  _sid="\$1"; _smsg="\$2"
  _sq=\$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "\$_smsg")
  _scode=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST \
    "http://127.0.0.1:$PORT/terminals/\$_sid/input?message=\$_sq")
  case "\$_scode" in
    2*) echo "ok \$_scode \$_sid" >> "\$ROUND/send.log" ;;
    *)  echo "SEND-FAIL \$_scode \$_sid" >> "\$ROUND/send.log" ;;
  esac
}

# LANE READINESS.  A lane that exists is not a lane that works, and the round
# had no gate between the two: in round 3 (box 010, 2026-09-16) the kiro lane
# was created, accepted 43 sends, and produced ONE turn end against the claude
# lanes' 42 each — its pane had no kiro process in it at all
# (cao.log: detected_agent=<none>).  The arm ran its full hour anyway and
# ``capped-parity`` FAILed at the end on a lane that was never alive.  So: one
# probe turn per lane, first, and an arm whose lanes cannot answer it is
# abandoned in minutes instead of failing a criterion in an hour.
#
# The signal is an INCREASE in that terminal's own ``turn.ended`` count, not a
# non-zero count: a lane can accrue a stray server-produced turn end without
# ever running its provider, which is exactly what the broken kiro lane did.
# Read-only, on the live database — a torn COPY is the hazard this harness
# guards against, and a ``count(*)`` is not a copy.
READY_DB="\$CAO_HOME_DIR/db/cli-agent-orchestrator.db"
turns_ended() {
  _te=\$(sqlite3 "file:\$READY_DB?mode=ro" \
    "select count(*) from worker_event where terminal_id='\$1' and kind='turn.ended';" 2>/dev/null)
  case "\$_te" in
    ''|*[!0-9]*) printf '0' ;;
    *) printf '%s' "\$_te" ;;
  esac
}
: > "\$ROUND/lane-readiness.txt"
ready_baseline=""
for id in \$LANES; do
  ready_baseline="\$ready_baseline \$id:\$(turns_ended \$id)"
done
for id in \$LANES; do
  send "\$id" "say only: ready"
done
ready_failures=""
for id in \$LANES; do
  _base=0
  for _pair in \$ready_baseline; do
    case "\$_pair" in "\$id:"*) _base=\${_pair#*:} ;; esac
  done
  _deadline=\$(( \$(date +%s) + $READY_SECONDS ))
  _now=\$(turns_ended \$id)
  while [ "\$_now" -le "\$_base" ] && [ \$(date +%s) -lt \$_deadline ]; do
    sleep 5
    _now=\$(turns_ended \$id)
  done
  if [ "\$_now" -gt "\$_base" ]; then
    echo "ready \$id turn.ended \$_base -> \$_now" >> "\$ROUND/lane-readiness.txt"
  else
    echo "NOT-READY \$id turn.ended stuck at \$_base" >> "\$ROUND/lane-readiness.txt"
    ready_failures="\$ready_failures \$id"
  fi
done
cat "\$ROUND/lane-readiness.txt"
if [ -n "\$ready_failures" ]; then
  echo "HARNESS: lane(s)\$ready_failures never completed a probe turn in ${READY_SECONDS}s;"
  echo "HARNESS: abandoning this arm rather than measuring lanes that are not alive."
  exit 2
fi

# The floor starts when the WORKLOAD does.  Readiness can take minutes, and
# counting it against the floor shortens the window the condition label's
# lifetime is measured in.
ARM_STARTED=\$(date +%s)

# ROUND-ROBIN, not lane-by-lane: every lane makes progress on every turn, so the
# arm's wall clock is one lane's rather than the sum, and the fleet holds several
# terminals in flight at once — which is the shape the sweep and the probe see in
# production and the shape a serial workload never produces.
for turn in \$(seq 1 $TURNS); do
  for id in \$LANES; do
    send "\$id" "say only: turn \$turn done"
  done
  sleep 12
done

# The two classification-input cases, once per lane: an exit 1 from a search
# (#613) and a message quoting an exit line (#545).  Neither may become a
# condition on the on arm.
for id in \$LANES; do
  send "\$id" "run: rg zzz-no-such-pattern-zzz . ; then say done"
done
sleep 30
for id in \$LANES; do
  send "\$id" "ignore this quoted line, it is not yours: [Command exited with code 1] -- say ok"
done
sleep 30

# THE CAP DRIVE (AC-2b case 12).  Without it ``capped-parity`` SKIPs, and a SKIP
# is never a pass — so the round could not return YES however good the build was.
#
# The banner is written to a FILE on the box and the worker is asked to cat it.
# It is NOT sent in the message, and that is the whole point: slice 4's
# ``note_delivered_text`` records what the server sent and
# ``_without_delivered_text`` subtracts it from the pane read on the ON arm only
# (#545).  A banner quoted in the message would therefore be stripped on the on
# arm and not on the off arm, and ``capped-parity`` would report a difference
# the product did not cause.  Via a file, the banner is the worker's own output
# in both arms.
#
# LIMIT, stated because the report has to: the only lane with a cap banner here
# is the UNSOURCED one.  ``claude_code`` has no entry in the cap-pattern table
# at all, and codex — the one provider that is both projectable and cappable —
# cannot authenticate on any reachable box.  So this exercises the derived kind
# fleet-wide, not the projected half of it.
# One banner per provider, because ``_classify_capped`` dispatches on the
# provider and each has its own regex (providers/condition.py:760-772).
# ``claude_code`` is deliberately absent: it has NO entry in that table, so a
# claude lane cannot produce a cap however it is driven.
cap_banner_for() {
  case "\$1" in
    cline_cli) printf 'ClinePass limit reached\n' ;;
    pi_cli)    printf 'INFERENCE_CAP_ERROR\n' ;;
    codex)     printf "You've hit your usage limit\n" ;;
    kiro_cli)  printf 'reached your monthly usage limit\n' ;;
    grok_cli)  printf 'You hit your weekly limit\n' ;;
    *)         return 1 ;;
  esac
}
cappable_lanes=""
while read -r lane_id lane_provider; do
  # Redirect AFTER the lookup succeeds.  Writing straight to the file created an
  # empty ``cap-banner-claude_code.txt`` for every provider with no banner,
  # because the shell opens the redirect before the function runs and
  # ``continue`` cannot unmake it — an empty banner file in the artefacts reads
  # like a drive that was attempted and produced nothing.
  lane_banner=\$(cap_banner_for "\$lane_provider" 2>/dev/null) || continue
  printf '%s\n' "\$lane_banner" > "\$ROUND/cap-banner-\$lane_provider.txt"
  cappable_lanes="\$cappable_lanes \$lane_id"
  send "\$lane_id" "Run this exact shell command and nothing else, then stop: cat \$ROUND/cap-banner-\$lane_provider.txt"
  echo "cap drive -> \$lane_id (\$lane_provider)" >> "\$ROUND/send.log"
done < "\$ROUND/lane-providers.txt"
sleep 60

# THE PRECONDITIONS (N11).  Every ``N/A`` scope used to be INFERRED from an
# empty result, which made a harness regression indistinguishable from a
# criterion the box cannot reach: a workload that silently stopped driving
# produced the same empty table as a fleet with nothing cappable on it, and the
# verdict stayed YES.  Three of this harness's defects have now had that exact
# shape.  So the arm RECORDS what it actually set up, and the analyser reads the
# record instead of guessing from absence.  An empty result whose precondition
# says the workload SHOULD have produced something is a FAIL.
python3 - "\$ROUND" "\$cappable_lanes" "\$CAO_HOME_DIR" <<'PRECONDITIONS'
import json, os, sys

round_dir, cappable = sys.argv[1], sys.argv[2].split()
arm_home = sys.argv[3] if len(sys.argv) > 3 else ""
providers = {}
for line in open(os.path.join(round_dir, "lane-providers.txt")):
    parts = line.split()
    if len(parts) == 2:
        providers[parts[0]] = parts[1]

# Dialog capability is read from the PROCESS TABLE, not from the terminal row.
# The row's ``shell_command`` comes back EMPTY for every lane on a live box —
# measured — and an empty string is not evidence that a lane lacks a
# permissions-skip flag; it is evidence that we could not tell.  Treating it as
# "not capable" would be the same silent-inapplicable defect this record exists
# to remove, so the flags are read from the spawned processes themselves.
# Matched on a TOKEN's basename, exactly.  A substring match is not good
# enough and was measured wrong: "/pi" matches ``box-picom`` and
# ``/tmp/picom:1.log``, which put six unrelated desktop processes into the
# dialog-capable list and would have failed ``prompt-awaiting`` on a round that
# was fine.
PROVIDER_BASENAMES = {"claude", "cline", "codex", "kiro", "pi"}
SKIP_FLAGS = (
    "--dangerously-skip-permissions",
    "--yolo",
    "--skip-permissions",
    "--auto-approve",          # cline
    "--full-auto",             # codex
    "--trust-all-tools",       # kiro
)
# ...and scoped to THIS ARM's own lanes.  The first version matched every
# provider-shaped process on the box, and a box is shared and long-lived: a
# cline hub daemon and a stray pi that had been running for days were counted
# as lanes of this round, neither carried a permissions-skip flag, and
# ``prompt-awaiting`` FAILed on processes the round did not create and could not
# have raised a card in (measured, box 010, round 3 2026-09-16).  The same scan
# also swept in the OTHER arm's surviving panes, so an off-arm process decided
# an on-arm criterion.
#
# Two ownership tests, either of which is sufficient, because the two spawn
# shapes leave different fingerprints: a provider the server launches with
# per-terminal files carries THIS arm's round directory in its command line,
# while anything the server started as a child inherits this arm's
# ``CAO_HOME_DIR``.  A herdr pane is created by the long-lived herdr session
# server rather than by cao-server, so the environment test alone would miss it
# and the command-line test alone would miss a bare pane; both are checked.
arm_paths = {p for p in (round_dir, os.path.realpath(round_dir)) if p}
arm_homes = {p for p in (arm_home, os.path.realpath(arm_home) if arm_home else "") if p}


def _owned_by_this_arm(pid, command):
    if any(path in command for path in arm_paths):
        return True
    if not arm_homes:
        return False
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            entries = handle.read().split(b"\x00")
    except OSError:
        return False
    for entry in entries:
        if entry.startswith(b"CAO_HOME_DIR="):
            value = entry.split(b"=", 1)[1].decode("utf-8", "replace")
            return value in arm_homes or os.path.realpath(value) in arm_homes
    return False


processes = []
foreign = []
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            command = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        continue
    if not command:
        continue
    if not any(
        os.path.basename(token) in PROVIDER_BASENAMES for token in command.split()
    ):
        continue
    if _owned_by_this_arm(pid, command):
        processes.append(command[:200])
    else:
        # Kept rather than dropped: "we excluded these and why" is evidence, and
        # a silently shorter list is how the first version's defect hid.
        foreign.append(command[:200])

unguarded = [c for c in processes if not any(flag in c for flag in SKIP_FLAGS)]
unreadable = []
if not processes:
    # No provider process of THIS ARM visible: capability is UNKNOWN, not absent.
    unreadable.append("no provider process of this arm found in /proc")

json.dump(
    {
        "lane_providers": providers,
        "provider_processes": processes,
        # Provider-shaped processes on the box that are NOT this arm's, and so
        # cannot bear on any criterion here.  Recorded for the report, never read
        # by a check.
        "foreign_provider_processes": foreign,
        "spawn_unreadable": unreadable,
        # Lanes the cap drive actually targeted, i.e. whose provider has a
        # banner the CAPPED classifier knows.
        "cappable_lanes": cappable,
        # Provider processes running WITHOUT any permissions-skip flag.  A
        # non-empty list means a real card was reachable in this arm, so an
        # absent ``prompt.awaiting`` is a failure rather than an unreachable
        # criterion.
        "dialog_capable_lanes": unguarded,
        # A terminal can only be certified when the H1 seam is armed, so an
        # unarmed seam is proof no cohort could exist.  Certification itself
        # lives in server memory and is not readable from a post-mortem.
        "herdr_seam_armed": os.environ.get("CAO_HERDR_RUNTIME", "").strip().lower()
        in {"1", "true", "yes", "on"},
    },
    open(os.path.join(round_dir, "preconditions.json"), "w"),
    indent=2,
)
print("preconditions recorded")
PRECONDITIONS
cat "\$ROUND/preconditions.json" 2>/dev/null | head -40

# Hold the arm open to its floor.  Not padding: the sweep runs every
# PANE_HEARTBEAT_S and the condition label's lifetime is measured in sweeps, so
# an arm that stopped the moment the last turn landed would never exercise the
# leg slice 4 added.
while [ \$(( \$(date +%s) - ARM_STARTED )) -lt $MIN_SECONDS ]; do sleep 30; done

kill \$SERIES_PID 2>/dev/null || true
curl -sf "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/fleet" > "\$ROUND/fleet.json" 2>/dev/null

# AC-2b case 10 lives on the READ path, which no post-mortem of the database can
# see: the fused getters are a property of the running server.  ``/terminals/<id>``
# reads through ``get_status``, and the fleet row reads through ``fuse_status``;
# capturing both beside the projection's own state is the outside view of "the
# fused getter does not move".
for id in \$LANES; do
  printf '{"id": "%s", "terminal": %s, "fleet_status": %s}\n' "\$id" \\
    "\$(curl -sf "http://127.0.0.1:$PORT/terminals/\$id" || echo 'null')" \\
    "\$(curl -sf "http://127.0.0.1:$PORT/sessions/\$ARM_SESSION/fleet" | python3 -c "
import json,sys
rows = json.load(sys.stdin)
rows = rows['terminals'] if isinstance(rows, dict) else rows
print(json.dumps(next((r.get('status') for r in rows if r.get('id') == '\$id'), None)))
" 2>/dev/null || echo 'null')" >> "\$ROUND/read-path.jsonl"
done

# Stop the server BEFORE copying the database: the sweep and the tailers are
# still writing until it does, and a copy taken under them is a torn read.
# ``pkill -f "cao server"`` matched NOTHING — the console script is
# ``cao-server``, with a hyphen, as the launch comment twenty lines up already
# says.  So the server was never stopped and every database below was copied out
# from under a running sweep: a torn read, silently, in both arms.  Stop this
# arm's own pid and wait for the port to actually close.
kill "\$SERVER_PID" 2>/dev/null || true
for _ in \$(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && kill -9 "\$SERVER_PID" 2>/dev/null
sleep 3
# Checkpoint the WAL so the copy is a complete database rather than a main file
# whose recent writes are still in the sidecar.
sqlite3 "\$CAO_HOME_DIR/db/cli-agent-orchestrator.db" 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1 || true
# The ANNOUNCE log.  ``server.log`` is only the captured stdout/stderr, and
# utils/logging.py sends WARNING+ to stderr while INFO — which is where
# "Terminal <id> status changed: <status>" lives — goes to its OWN file under
# CAO_HOME_DIR/logs.  So ``announce-count``, the live detector for B1's defect
# class, read a file that structurally cannot contain the line it greps for and
# SKIPped on every round.
cp "\$CAO_HOME_DIR"/logs/cao_*.log "\$ROUND/cao.log" 2>/dev/null || true
cp "\$CAO_HOME_DIR/db/cli-agent-orchestrator.db" "\$ROUND/db" 2>/dev/null \\
  || cp "\$HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db" "\$ROUND/db" 2>/dev/null \\
  || { echo "HARNESS: no coordination database to copy"; exit 2; }
echo "ARM \$ARM OK \$ROUND"
REMOTE
}

echo "live round: box grok-box-$BOX, sha $SHA, $TURNS turns per lane, floor ${MIN_SECONDS}s per arm" | tee "$OUT/round.log"

# The lease id is validated rather than trusted.  A refusal — the box is held by
# another lane, or frozen — still answers with JSON, and its ``lease_id`` is
# null; a naive read turns that into the STRING "None" and every later
# ``grokfleet ssh --lease None`` fails one at a time instead of the round
# stopping here.  (Found by dry-running this script against a box another lane
# had leased, which is the only way that path is ever reached.)
LEASE_ID="$(
  grokfleet lease acquire --purpose "2b live round $SHA" --ttl 3h --box "$BOX" --json 2>/dev/null |
    python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get("lease_id")
except Exception:
    sys.exit(1)
if not isinstance(value, str) or not value.strip():
    sys.exit(1)
print(value.strip())
' 2>/dev/null || true
)"
case "$LEASE_ID" in
  ""|None|null) die "could not lease grok-box-$BOX (held, frozen, or unreachable)" ;;
esac
trap 'grokfleet lease release "$LEASE_ID" >/dev/null 2>&1 || true' EXIT

# PREFLIGHT: a box with no agent CLIs cannot run this round, and it fails late
# and confusingly if allowed to try — the supervisor "launches" after a 240 s
# timeout with a pane that has no binary in it, and every worker lane then
# answers ``seed_exec_failed`` fifteen minutes in.  ``.provisioned`` is the
# marker scripts/box-run.sh already uses for this (its rc 80), and codex is the
# binary that actually matters: it is the only provider with a rollout source,
# so without it nothing is PROJECTED and every check below would SKIP on a round
# that looked like it ran.
preflight="$(grokfleet ssh --lease "$LEASE_ID" "export HOME=$BOXHOME PATH=$BOXHOME/.bun/bin:$BOXHOME/.local/bin:$BOXHOME/.grok/bin:\$PATH; test -f \$HOME/.provisioned && echo marker; command -v codex >/dev/null && echo codex" 2>/dev/null || true)"
case "$preflight" in
  *codex*) : ;;
  *) die "grok-box-$BOX is not provisioned for agent work (no codex binary). Run scripts/box-setup.sh grok-box-$BOX first, or pick a box that reports .provisioned." ;;
esac

grokfleet ssh --lease "$LEASE_ID" "mkdir -p $REMOTE_SCRATCH" >/dev/null 2>&1

# The certification store travels as one base64 tar, for the same reason the
# payload does: ``grokfleet ssh`` is a single-command transport and a multi-file
# copy through it loses quoting.  It lands OUTSIDE $REMOTE_SCRATCH/<arm>, which
# each arm deletes on entry, so both arms install the same bytes.
if [ -n "$CERT_DIR" ]; then
  cert_b64="$(tar -C "$CERT_DIR" -czf - . | base64 -w0)" ||
    die "could not pack --certification-dir $CERT_DIR"
  grokfleet ssh --lease "$LEASE_ID" "
    rm -rf $REMOTE_SCRATCH-certification
    mkdir -p $REMOTE_SCRATCH-certification
    echo $cert_b64 | base64 -d | tar -C $REMOTE_SCRATCH-certification -xzf -
  " >/dev/null 2>&1 || die "could not ship the certification store to grok-box-$BOX"
  shipped="$(grokfleet ssh --lease "$LEASE_ID" \
    "ls $REMOTE_SCRATCH-certification/positions/$POSITION.md 2>/dev/null" 2>/dev/null || true)"
  case "$shipped" in
    *"$POSITION.md") : ;;
    *) die "the certification store did not arrive on grok-box-$BOX" ;;
  esac
fi

for arm in off on; do
  if [ "$arm" = off ]; then
    payload="$(remote_arm off off '')"
  else
    payload="$(remote_arm on on "$PROVIDERS")"
  fi
  echo "--- arm $arm ---" | tee -a "$OUT/round.log"
  # The payload travels as a FILE, not as an argument.  ``grokfleet ssh`` does
  # not transport a multi-line command with quoting intact — the arm's first
  # attempt failed with no output at all, and the same transport silently ate a
  # ``grep -E "^(FAILED|ERROR) test/"`` earlier in this work.  One base64 line
  # in, decode, run: nothing for a shell to re-interpret on the way.
  printf '%s' "$payload" > "$OUT/$arm/payload.sh"
  payload_b64="$(base64 -w0 "$OUT/$arm/payload.sh")"
  if ! grokfleet ssh --lease "$LEASE_ID" "echo $payload_b64 | base64 -d > $REMOTE_SCRATCH-payload.sh && bash $REMOTE_SCRATCH-payload.sh" >>"$OUT/round.log" 2>&1; then
    die "arm $arm did not complete; see $OUT/round.log"
  fi
  for artefact in db fleet.json fleet-series.jsonl read-path.jsonl server.log cao.log launch.log preconditions.json lane-providers.txt lane-readiness.txt composed-names.txt lane-flat-control.json send.log; do
    grokfleet ssh --lease "$LEASE_ID" "cat $REMOTE_SCRATCH/$arm/$artefact 2>/dev/null | base64 -w0" \
      2>/dev/null | base64 -d > "$OUT/$arm/$artefact" 2>/dev/null || true
  done
done

echo "--- analysing ---" | tee -a "$OUT/round.log"
# On the BOX, where the fork is installed: the comparison maps the pane's
# vocabulary through the projection's own ``legacy_state``, and a second copy of
# that table on the laptop is exactly the drift the plan warns about.
ANALYSER_B64="$(base64 -w0 "$ANALYSER")"
grokfleet ssh --lease "$LEASE_ID" "
  echo '$ANALYSER_B64' | base64 -d > $REMOTE_SCRATCH/analyse.py
  cd $BOXHOME/cli-subagents/cli-agent-orchestrator
  uv run python $REMOTE_SCRATCH/analyse.py \
    --on-db $REMOTE_SCRATCH/on/db \
    --off-db $REMOTE_SCRATCH/off/db \
    --on-fleet $REMOTE_SCRATCH/on/fleet.json \
    --on-fleet-series $REMOTE_SCRATCH/on/fleet-series.jsonl \
    --on-read-path $REMOTE_SCRATCH/on/read-path.jsonl \
    --on-server-log $REMOTE_SCRATCH/on/cao.log \
    --on-preconditions $REMOTE_SCRATCH/on/preconditions.json
" | tee "$OUT/report.txt"
verdict_status=${PIPESTATUS[0]}

echo "report: $OUT/report.txt" | tee -a "$OUT/round.log"
# The analyser prints FLIP-READY-BOX, renamed in 38d335cc when the criteria a
# box cannot reach moved to the laptop acceptance.  This grep still read the OLD
# name, which no longer appears anywhere in its output, so verdict_status was
# forced to 1 on EVERY round including a clean one and the exit status could not
# say YES.  Match the line the analyser actually prints.
grep -q 'FLIP-READY-BOX: YES' "$OUT/report.txt" 2>/dev/null || verdict_status=1
exit "$verdict_status"
