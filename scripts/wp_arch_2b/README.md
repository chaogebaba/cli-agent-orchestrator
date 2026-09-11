# WP-ARCH 2b — the box live round

`live-round.sh` runs one scripted workload twice on a single grok box, once with
the status cutover OFF and once with it ON, and `live-round-analyse.py` decides
`FLIP-READY: YES/NO` from the two arms' coordination databases.

```
scripts/wp_arch_2b/live-round.sh --box 007 --sha <fork-sha> \
  --providers claude_code --lanes "claude_code pi_cli" \
  --out /data/claude-scratch/cli-subagents/2b/live-round-<tag>
```

Exit status is the verdict: 0 for YES, 1 for NO, 2 for a harness failure.

## Three outcomes, and the difference between two of them matters

* **PASS / FAIL** — the criterion was exercised and the build met it, or did not.
* **SKIP** — the workload that should have exercised the criterion did not
  happen. **A SKIP is never a pass**, and it makes the verdict NO. This is the
  rule that stops a round from certifying a criterion it never reached.
* **N/A `[scope]`** — the criterion is one a BOX round cannot reach however well
  it runs. Excluded from the verdict, reported with the scope that carries it
  instead. This is not a softened SKIP: it exists because counting a structurally
  unreachable criterion as a SKIP makes the verdict permanently NO, and a gate
  that always says NO stops being read.

The verdict line is therefore `FLIP-READY-BOX`, not `FLIP-READY`. A YES is
**necessary** for the flip and not **sufficient**: the `N/A [LAPTOP-ONLY]`
criteria are met separately, in the laptop flip acceptance run before
`CAO_WORKER_TRUTH_STATUS` is turned on.

Current scopes:

| Criterion | Scope | Why a box cannot reach it |
|---|---|---|
| `prompt-awaiting` | `LAPTOP-ONLY` | Every box lane spawns `--dangerously-skip-permissions` and the add-terminal endpoint has no parameter to disable it, so no real card renders. A printed card does not substitute: the classifier requires it to be the live bottom region when the sampler fires. Met by a real permission card a human answers. |
| `capped-parity` | `LAPTOP-ONLY` | No provider on a box is both projectable and cappable — `claude_code` has no entry in the cap-pattern table at all. Met by a codex seat that actually hits its cap. |
| `certified-pane-silence` | `PENDING-COHORT` | No certified herdr cohort exists anywhere yet. Arms itself when WP-HERDR certifies one. |

Adding to this table is a **ruling**, not a convenience. A criterion moves out of
the box round only when the box genuinely cannot reach it and something else
genuinely does.

## Six things that are easy to get wrong, each of which cost a round

1. **`message` on `POST /terminals/{id}/input` is a QUERY parameter, not a JSON
   body.** FastAPI binds the bare `str` argument to the query string
   (`api/main.py`). A JSON body answers **422** and sends nothing, so the whole
   workload is a silent no-op — two rounds produced six transitions in twenty
   minutes before this was found. `send()` urlencodes onto the URL and records
   every non-2xx instead of swallowing it with `curl -sf`.
2. **The box workload home is `/workspace/cao/home`, not the login home.**
   box-setup puts the repo, the provider credentials and the `.provisioned`
   sentinel there. Without `HOME` set to it every provider CLI reads as MISSING
   on a fully provisioned box.
3. **`grokfleet ssh` does not transport a multi-line quoted payload.** Each arm
   ships as one base64 line, is decoded to a file on the box, and is run from
   there. The same transport silently ate a `grep -E` over box output earlier in
   this work package.
4. **The console script is `cao-server`, with a hyphen.** `cao server` is not a
   command. The teardown used to `pkill -f "cao server"`, which matched nothing,
   so every database was copied out from under a running sweep — a torn read, in
   both arms. Each arm now records its own server pid, waits for the port to
   close, and checkpoints the write-ahead log before copying.
5. **The announce line is at INFO, and INFO does not reach the captured
   stdout/stderr.** `utils/logging.py` sends WARNING and above to stderr and
   everything else to its own file under `CAO_HOME_DIR/logs`. The
   `announce-count` check greps for `Terminal <id> status changed:`, so it must
   be fed `cao.log`, not `server.log`.
6. **A banner driven for a test must not travel in the message.** Slice 4's
   `note_delivered_text` subtracts what the server sent from the pane read, on
   the ON arm only (#545). A cap banner quoted in the input would therefore be
   stripped in one arm and not the other, and `capped-parity` would report a
   difference the product did not cause. The cap drive writes the banner to a
   file on the box and asks the worker to `cat` it.

## Known limits of the round

The three criteria above are scoped out and carried elsewhere. Beyond them:

* **A codex lane needs the box `config.toml` fixed first.** The shipped
  `auth.json` serves the OAuth provider, but a box's `config.toml` may point
  `model_provider` at `aihub`, whose key exists only in a login shell and whose
  gateway answers 403. `codex login status` still says "Logged in" in that state
  — only `codex exec` settles it, so preflight with `codex exec`, never with
  `login status`.
* **pi needs a wrapper, not the bare install.** The working shape is bun's
  `@earendil-works/pi-coding-agent` `cli.js` launched from a `~/.local/bin/pi`
  wrapper using `exec -a pi`, because herdr keys on `argv[0]` and node 20 rejects
  the node-shebang entry point. Without it the lane answers 500 "startup error
  banner".
