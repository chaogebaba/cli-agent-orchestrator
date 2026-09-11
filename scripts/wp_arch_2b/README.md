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
**A SKIP is never a pass** — a criterion the round did not exercise has not been
met, and the verdict is NO.

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

* **`prompt-awaiting` cannot be driven.** Every lane spawns with
  `--dangerously-skip-permissions` and the add-terminal endpoint has no
  parameter to disable it, so no real card renders. A printed card does not
  substitute: `_is_ink_selection_waiting` requires the card to be the live bottom
  region when the sampler fires, which a `cat` cannot hold. The criterion is
  covered by unit tests, not by the round.
* **`capped-parity` reaches only the unsourced half on a codex-less box.**
  `claude_code` has no entry in the cap-pattern table at all, so the only
  cappable lane is one no allowlist projects. codex is the single provider that
  is both projectable and cappable.
* **`certified-pane-silence` needs a certified herdr cohort.** It skips until
  one exists, which is WP-HERDR's to deliver.
