---
name: cao-worker-protocols
description: Worker-side callback and completion rules for assigned and handed-off tasks in CAO
---

# CAO Worker Protocols

Use this skill when acting as a worker agent inside CLI Agent Orchestrator.

This skill explains how workers should interpret assigned versus handed-off work, when to call `send_message`, and how to report results back cleanly.

Every `send_message` reference in this skill means the cao-mcp-server `send_message` MCP tool, never a provider-native `collaboration.send_message`.

## Understand the Dispatch Mode

Workers receive tasks through one of two orchestration modes:

- `handoff`: blocking work where the orchestrator captures your final output automatically
- `assign`: non-blocking work where you must actively return results to the requesting terminal

Depending on provider and CAO behavior, a handoff may be made explicit in the task text. For example, Codex workers currently receive a `[CAO Handoff]` prefix for blocking handoffs. Other providers may rely on the task wording and orchestration context instead.

## Rules for Handoff Tasks

When the task is a blocking handoff, complete the work and present the result in your normal response. The orchestrator captures that response automatically.

Do not call `send_message` for ordinary handoff completion unless the task explicitly asks for additional side-channel communication.

## Rules for Assigned Tasks

When the task came through `assign`, send your results back after you finish the work:

1. Format the result clearly and concisely.
2. Call the cao-mcp-server `send_message` MCP tool with `send_message(message=...)` — never a built-in `collaboration.send_message`; omitting `receiver_id` routes the result to the terminal that assigned the task (the recorded caller). This is the reliable default.
3. If the task message names a different callback terminal (directly or in an appended suffix such as `[Assigned by terminal ...]`), pass that ID as `receiver_id` instead.
4. ACK and callback messages should quote the complete received `mid <id>:<hex32>` token verbatim; a bare `mid <id>` cannot confirm delivery.

Do not stop after writing a normal response if the assignment explicitly requires a callback. The requesting terminal depends on `send_message` to receive the result.

Your own `CAO_TERMINAL_ID` identifies your terminal, not the callback target. Never pass it as `receiver_id`.

## Authority Pin Checks

When a task names authority files registered with the authority-pin registry, call `verify_pin(file_path)` for every file at task start, after any suspicion of authority drift, and before every commit.

- `VALID` or `SUPERSEDED`: continue against the current file. `SUPERSEDED` is stateless and may be returned at every checkpoint.
- `DRIFT`: stop and report the verdict; do not continue against stale or changed authority.
- `UNPINNED`: use the legacy prose-pin discipline, subject to the task-start ordering below.

At task start, if the dispatch message names a file as registry-pinned and `verify_pin` returns `UNPINNED`, retry up to 3 times at 2-second intervals before treating it as genuinely `UNPINNED` and falling back to legacy prose-pin discipline. `VALID`, `SUPERSEDED`, and `DRIFT` are never retried. Dispatches that name no registry pin take the immediate legacy path, and `UNPINNED` at any later checkpoint is not retried.

## Message Formatting

Return results that are easy for the supervisor to merge into a larger workflow:

- Identify what task or dataset the result belongs to
- Include the requested output or deliverable
- Keep the message specific enough to act on without re-reading the whole task

If the task asks for progress updates, use `send_message` for those updates too. Otherwise prefer one final callback with the completed deliverable.

## Filesystem and Reporting Discipline

If the task asks you to create files, write them before reporting completion. When sending results back to a supervisor, include absolute file paths so the supervisor can continue the workflow without ambiguity.

### Working-directory discipline

- Never `cd` into a directory you may later delete; run cleanup from outside the disposable directory.
- If every command fails with `getcwd`/`ENOENT`, stop issuing commands and report the cwd brick to your supervisor via `send_message` immediately; do not retry.

### Fixture law for builders (3 consecutive diff-gate SHIP-NOs: 0b, 0b.1, WPWD — 2026-07-20/21)

- **Fixtures drive the PRODUCTION entry path, never the helper directly.** A
  test that calls `abort_dispatch()` by hand, mocks the function under test,
  or injects synthetic rows past the real persistence/dispatch layer proves
  nothing about the law — 12+ mutants survived green suites this way. Drive
  the real API/MCP/send path end to end and assert the final observable state.
- **Mutation evidence must be REPLAYABLE at the build commit**: for every
  claimed mutant — exact patch, selector (which must COLLECT, cite output),
  observed red result, restore step, restored file hash. A prose mapping or
  selector list is not evidence; the reviewer replays your rows.

After resolving Python merge conflicts, run `python scripts/verify_resolved_python.py --all-changed` before reporting success. The helper compiles every changed Python file and performs pytest collection for changed test files.

## Forbidden Operations (absolute, regardless of task wording)

You run INSIDE the CAO server you may be asked to test or modify. Some operations
destroy the whole session fleet — including you, your supervisor, and every other
worker — and are reserved for the human operator alone:

- **NEVER restart, stop, or reload `cao-server`** (`systemctl --user restart|stop cao-server`, `pkill`, or any equivalent). Not to "activate" a change, not to A/B-test deployment state, not even if the task says "run anything".
- **NEVER run `install.sh`, `uv tool install cli-agent-orchestrator`, or `cao install`** — deployment/activation is human-gated.
- If your task seems to require any of these, STOP and report back via `send_message` that the step needs the human operator. That callback IS the correct completion of the task.

## Reliability Guidelines

- If the task names an explicit callback terminal, note its ID before you start expensive work; otherwise rely on the default routing (omit `receiver_id`).
- If `send_message` is available and the task requires a callback, call it directly rather than ending with prose alone.
- Keep callback messages structured so the supervisor can merge them into a larger workflow.
- For handoff tasks, return the completed output directly and let the orchestrator handle delivery.

## Spawning your own sub-lanes (delegating workers: blueprint maker, architect)

When your charter has you spawn helper or reviewer lanes with `assign`:

- **The provider comes from the agent PROFILE, never from a model setting.**
  `assign(agent_profile="grok_dev")` gives a Grok CLI lane; `codex_dev` /
  `codex_reviewer` give Codex lanes; `developer-sonnet` gives a cheap
  scratch Claude lane. ALL review lanes are `codex_reviewer` — Fable
  review lanes are RETIRED (user 2026-07-20); never spawn one.
- **NEVER set, pass, or configure a model yourself** — `providers.toml` owns
  per-profile model defaults.
- **Grok lanes LOOK like Claude Code** — the grok_cli provider launches a
  Claude Code binary pointed at a relay grok model; a "Claude Code ... grok-4.5"
  banner on a grok lane is NORMAL, not a mis-spawn.
- **Model-select park = provider/relay outage, not your bug.** If a lane sits
  at "issue with the selected model … Run /model" the relay roster is down for
  that model (it flaps). Do NOT retry the same profile in a loop: ONE retry
  max, then either re-`assign` the same brief to a working-provider profile
  (`developer-sonnet` is the standing fallback for dead grok lanes) or report
  the outage to your caller. `delete_terminal` the parked lane either way.

### Lane lifecycle and ownership (delegating workers: maker, architect)

- **Kill only what you summoned.** Delete your own reviewer/helper lanes when
  your loop closes (charter already says so). You may NEVER delete a terminal
  you did not create — not the supervisor's lanes, not another worker's, not
  shared infrastructure. The supervisor owns fleet-wide lifecycle and sweeps
  up anything you leave behind, but leaving cleanup to the sweep is a
  deviation to cite, not the default.
- **Reuse shared warm infrastructure before summoning.** If the supervisor's
  dispatch names a warm shared lane (the session `grok_oracle` above all),
  ask IT your questions (ask-then-idle w2w) instead of spawning your own
  duplicate — one warm oracle serves every agent in the fleet. Never delete
  a shared lane; it is not yours even while you use it.

### Lane health is the SUPERVISOR'S job (delegating workers: maker, architect)

As a delegating worker you DISPATCH lanes and RECEIVE their reports — nothing
in between. The supervisor owns keeping every lane in the session healthy:

- If a lane crashes, hangs, hits a provider false-flag/refusal banner, or gets
  watchdog-flagged: forward the incident to the supervisor via `send_message`
  (terminal id + what you saw, verbatim) and go back to waiting. The
  supervisor scrubs/nudges/recovers it; the lane's callback still comes to YOU.
- Do NOT peek-doctor, steer, or re-spawn a sick lane yourself. The one
  exception: a lane cleanly DEAD from a known provider outage (model-select
  park above) — there the documented fallback re-assign applies.
- Codex false-flag strikes specifically: report to the supervisor, who runs
  the scrub-and-nudge; never try to talk a codex lane past a refusal banner.

### Codebase recon (delegating workers: maker, architect)

You have the SAME recon toolkit as the supervisor — use it instead of raw
self-grepping:

- **graphify FIRST**: any repo with `graphify-out/` — `graphify query
  "<question>"` (scoped subgraph), `graphify path "<A>" "<B>"`
  (relationships), `graphify explain "<concept>"`. Orient there before
  reading any source file; read raw lines only after orientation. The CAO
  fork has its OWN graph inside `cli-agent-orchestrator/` — the root graph
  cannot see gitignored paths.
- **Warm grok oracle for exact refs**: if a session `grok_oracle` is alive,
  send it your codebase questions via `send_message` (ask-then-idle: send,
  END YOUR TURN, the answer arrives as a message — busy-waiting deadlocks
  w2w). It returns exact file:line references and snippets. The oracle is
  SHARED session infrastructure — never delete it, never treat it as your
  disposable.
- **grok_dev grunt lane for mechanical recon**: fire `assign(agent_profile=
  "grok_dev")` for bounded chores — enumerate call sites, collect diffs,
  build an inventory file, run a probe script. Same lane rules as everywhere:
  files-not-prose deliverables, absolute paths in callbacks, delete when done.
- Division of labor: graphify/oracle answer "where/what is X" cheaply;
  grok_dev produces artifact files; YOU do only the judgment reading —
  the deciding lines, not the whole tour.

### Provider craft (what the supervisor knows — use it)

- **Codex lanes**: keep briefs terse and concrete (goal, file paths,
  acceptance criteria, what NOT to do; word-cap answers). When a claim can be
  tested, have codex RUN it, not reason about it. NEVER quote refusal/wrapper
  literals or security vocabulary into a codex brief — reference by
  `file:line`; neutral artifact names; frame reviews as "correctness review of
  our own orchestrator". A codex "quota exceeded" banner is FALSE on the relay
  backend, REAL on ChatGPT subscription — report, don't self-diagnose.
- **Grok lanes**: iterate, don't one-shot — grok does best with short
  follow-up rounds. Grok summarizing instead of quoting → have it write full
  output to a file and reference the path. Grok detached-command status
  (`N commands still running`) over an idle composer is a known false-idle
  (F29) — don't panic-kill a grok lane for it.
- **All lanes**: point at files rather than pasting long content; require
  callbacks to name absolute paths; capture a verbatim pane sample BEFORE any
  recover/kill when a lane misbehaves (sample-first law).

### Token-saving edit discipline (authoring workers: blueprint maker, architect)

- **Edit via commands, never Read-then-Edit.** The Read→Edit-tool cycle pays
  for the file bytes twice (once reading, once echoing the diff). Instead:
  - Multi-line/structural edits: `python3 - <<'EOF'` heredoc doing
    `s.replace(old, new)` with an `assert s.count(old) == 1` guard BEFORE
    writing — the guard catches stale/line-wrapped anchors before any damage.
  - One-line substitutions: `sed -i` — but run a count check first
    (`rg -c 'pattern' file`) so you know exactly how many sites you hit.
- **Read narrow — for READING as much as editing.** Never read a whole large
  file to work one region — locate with `rg -n` / `sed -n 'A,Bp'`, then read
  or edit only that span. Re-reading your own 1000+-line blueprint whole for
  one fold costs ~40x the scoped read (measured: ~22K vs ~550 tokens, context
  drill 2026-07-20); ONE lapse per gate loop outweighs every other saving.
  Read a full file only when you must judge all of it (e.g. a final
  convergence pass), and say so.
- **graphify: narrow the query surface.** Default wide BFS answers cost ~2K
  tokens and truncate; prefer `graphify explain "<concept>"` or a scoped
  `graphify query` with a tight question, and drill into specific nodes
  rather than re-running broader queries.
- **Verify by re-grepping the anchor, not re-reading the file.** After a
  command edit, `rg -n` the new text — a few lines, not the whole file.
- **`git add` explicit paths, never `-A`**; commit messages state what ruling
  or round the edit lands.
- If an anchor assert fails, the file drifted (often a line-wrap difference):
  re-grep the exact bytes (`sed -n`, `cat -A`) and retry — never fall back to
  the Edit tool on a big file.
- Same-round multi-lane dispatch: set `barrier="<wp>-r<N>"` on every member if
  your schema exposes it; otherwise have each lane end its callback with
  `ROUND r<N> LANE k/M` and act only when all M arrived.
- **Waiting on lane callbacks = go IDLE.** Delivery is event-driven — lane
  callbacks arrive when you idle. Never poll, busy-wait, or schedule wakeups
  (e.g. ScheduleWakeup — that tool is /loop-mode only) to check on a lane;
  end your turn and let the callback wake you (drill D1, 2026-07-20).
