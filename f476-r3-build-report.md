# F476 r3 build (blueprint r4) — close the two wake-cursor bypasses (#388)

**Issue:** #388 (F532, "same CAO callback re-delivered many times to the supervisor";
same family as F476 #331). Sample count 37; fresh 07:47Z sample: bridge replayed 7
already-acked ids (2726,2727,2730,2733,2734,2735,2736) in one batch.

**Lineage note (honest):** the F476 single-wake-cursor *mechanism*
(`claim_unnotified_wake` / `commit_wake`, the D4 lease columns, the migration,
AC2–AC15 tests) was already BUILT and MERGED to fork main as `ceae95da` (+ fixup
`7787ed34`). That build gated at **r2** (blueprint revision r4) — there is no "r3
build report" artifact; the supervisor's original pointer to one was withdrawn.
This round is therefore scoped by the supervisor decision, not a prior report:
**close the two surfaces that still BYPASSED the merged cursor** and re-emitted
acked ids. Named "F476 r3 build (blueprint r4)" to keep lineage honest.

## Worktree / branch / commits

- **Worktree:** `/data/cao-scratch/60f65f33/f476-r4` (branch `cao/60f65f33`)
- **Branch:** `cao/60f65f33` (pushed to origin as a scratch branch; never main; no PR)
- **Base:** `718849a4` (fork main after F613 + SEED_OK merged; REBASED onto this from
  the original build base `e64684f9` — rebase was clean, zero conflicts)
- **HEAD:** `5fe336279bdbe322982cf3d81b7ecdecbae820b1`
- Original build base (pre-rebase): `e64684f9`; original pre-rebase HEAD: `6d5c6c61`.
- Commits (on top of base):
  - `684f705f` F476 r3 (#388): route deliver_pending push + WS doorbell through the wake cursor
  - `1321206f` F476 r3 (#388): black --line-length 100 formatting
  - `623ef641` F476 r3 (#388): update f158_doorbell_fallback + fx168_hotfix tests for cursor-routed wake
  - `97c93f81` F476 r3 (#388): build report
  - `5fe33627` F476 r3 (#388): build report — correct new-test count
  (SHAs above are post-rebase onto 718849a4.)
- **Base ref pushed for A/B:** `cao/60f65f33-base` → `e64684f9` (original build base;
  used for the pre-rebase same-box A/B; still on origin for audit).

## Contract implemented

Per callback id: exactly ONE inbox-drain digest (path 1, untouched) + AT MOST ONE
wake (WS doorbell OR teammate/native, never both, never a replay of an acked id).
Acked ids are never re-emitted on reconnect / replay / aged echo.

## The two bypasses closed

### Bypass 1 — `deliver_pending` pull-mode teammate push
`inbox_service.py` supervisor-mailbox pull-mode gate called
`attempt_teammate_push(terminal_id, still_pending)` **directly**, gated only by
`_should_teammate_push` + `supervisor.wake.native` + an F457 pending recheck —
NEVER through `claim_unnotified_wake`. This is the "Message N ready. Drain"
teammate replay that re-surfaced acked ids (issue #388 samples 3–17).

**Fix:** the gate now calls `request_delivery(terminal_id)`, which arms the F136
runner (`_f136_run_callback_delivery`) — claim → commit → emit through the single
wake cursor. The wake.native / acked-row / `_should_teammate_push` gates are
enforced downstream in the runner and `ring_supervisor_doorbell`, not duplicated
in the gate.

### Bypass 2 — WS advisory frame fired on the insert-commit
The WS frame (`push_doorbell_frame_sync`) was fired from TWO insert-commit sites,
both ungated by the wake cursor (blueprint D8 violation):
- `clients/database.py::_f413_after_commit` (SQLAlchemy `after_commit` hook), and
- `services/mailbox_service.py` deferred-stash drain (the F158-R3 lock-release path).

On a re-insert / replay / aged echo of an already-acked row, either site re-emitted
a WS frame.

**Fix:** both sites now ONLY signal `request_delivery` (one per distinct terminal;
no WS frame on insert). The WS frame is emitted from the cursor-gated F136 runner:
`_f136_run_callback_delivery` (which runs off the delivery loop via
`asyncio.to_thread`, so `push_doorbell_frame_sync` can arbitrate the WS/native
winner) fires the WS frame only for rows it actually wrote this cycle
(`written>0`), sets `outcome._ws_fired`, and `_f136_post_delivery` submits the
native coalesce ring ONLY when `not outcome._ws_fired`. At most one transport per
id; an acked/aged id yields `written==0` → zero WS, zero native.

`api/main.py` already only called `request_delivery` (F158-R3 removed its
redundant WS push); its stale comments were updated.

## Files changed — `git diff --name-status 718849a4..9e7c408b` (13 paths, verbatim)

```
A	f476-r3-build-report.md
M	src/cli_agent_orchestrator/api/main.py
M	src/cli_agent_orchestrator/clients/database.py
M	src/cli_agent_orchestrator/services/inbox_service.py
M	src/cli_agent_orchestrator/services/mailbox_service.py
M	test/services/test_f158_doorbell_fallback.py
M	test/services/test_f158_r2_doorbell_regression.py
M	test/services/test_f158_r5_e2e_doorbell_race.py
M	test/services/test_f413_orm_listeners.py
M	test/services/test_f457_r2_gate_fixes.py
M	test/services/test_f457_wake_gate_dedupe.py
A	test/services/test_f476_r3_bypass_closure.py
M	test/services/test_fx168_hotfix.py
```

That is **4 source (M) + 8 test files (7 M + 1 A) + 1 report (A) = 13 paths.**
(Correction from an earlier draft that said "6 updated tests": the true count is 7
MODIFIED test files + 1 NEW = 8 test files; `test_fx168_hotfix.py` is the 8th.)

Source (4):
- `src/cli_agent_orchestrator/services/inbox_service.py` — deliver_pending gate →
  request_delivery; WS frame fired in the runner; post_delivery gates native on
  `_ws_fired`; new `CallbackRunOutcome._ws_fired` field.
- `src/cli_agent_orchestrator/clients/database.py` — `_f413_after_commit` no longer
  fires WS/mark_ws_delivered; signals request_delivery per terminal (deduped).
- `src/cli_agent_orchestrator/services/mailbox_service.py` — deferred-stash drain no
  longer fires WS; signals request_delivery per terminal (deduped).
- `src/cli_agent_orchestrator/api/main.py` — comment accuracy only (behavior already
  request_delivery-only).

Test files (8):
- `test/services/test_f476_r3_bypass_closure.py` (NEW/A) — 10 tests (see below).
- Updated (M) to the r3 contract (blueprint Test-break list — old-behavior pins):
  `test_f413_orm_listeners.py`, `test_f158_r2_doorbell_regression.py`,
  `test_f158_r5_e2e_doorbell_race.py`, `test_f158_doorbell_fallback.py`,
  `test_f457_r2_gate_fixes.py`, `test_f457_wake_gate_dedupe.py`,
  `test_fx168_hotfix.py` (7 files).

Report (1, A): `f476-r3-build-report.md`.

## Mutation recipe (reproducible — apply, run the node, revert)

Each mutant below is a single content-anchored edit on `HEAD 9e7c408b`
(`src/cli_agent_orchestrator/services/inbox_service.py` unless noted). Apply with
the `python -c` snippet (portable across the line drift a reviewer's checkout may
have), run the named pytest node, confirm it goes **RED**, then revert with
`git checkout -- <file>`. Run from the fork root
(`~/cli-subagents/cli-agent-orchestrator`) on a box, e.g.
`uv run pytest "<node>" -q -p no:cacheprovider`.

**M1 — drop the wake-cursor advance** (the commit_wake `through_id`): the runner
must advance `callback_notified_through_id` to the forward high-water. Force it to
0 so the cursor never moves:
```
python -c "import pathlib,re; f=pathlib.Path('src/cli_agent_orchestrator/services/inbox_service.py'); s=f.read_text(); s=s.replace('through_id = forward_high_water if forward_high_water > 0 else claim.claimed_high_water','through_id = 0  # MUTANT M1',1); f.write_text(s)"
```
- MUST go RED: `test/services/test_f476_r3_bypass_closure.py::TestMutantLedger::test_real_commit_advances_cursor_mutant_baseline`
  (asserts `_cursor(...) == 1`; under the mutant the cursor stays 0).
- Also caught by: `...::TestAckedIdNeverReemitted::test_bridge_replay_of_acked_ids_zero_emits`
  and `...::TestOneWakePerIdNative::test_second_run_same_row_no_reemit` (a cursor
  that never advances re-emits acked/replayed ids).

**M2 — emit on BOTH surfaces** (remove the `not outcome._ws_fired` guard in
`_f136_post_delivery`): the native ring must be suppressed when WS already fired.
Drop the guard so native is submitted even after WS won:
```
python -c "import pathlib; f=pathlib.Path('src/cli_agent_orchestrator/services/inbox_service.py'); s=f.read_text(); s=s.replace('if outcome.written > 0 and outcome.max_written_row_id > 0 and not outcome._ws_fired:','if outcome.written > 0 and outcome.max_written_row_id > 0:  # MUTANT M2',1); f.write_text(s)"
```
- MUST go RED: `test/services/test_f476_r3_bypass_closure.py::TestMutantLedger::test_mutant_emit_on_both_surfaces_is_red`
  (asserts WS∩native == ∅ and exactly one transport; the mutant puts the id on both
  probes).
- Also caught by: `...::TestOneWakePerIdWsWins::test_ws_fires_native_suppressed`
  and `test/services/test_f158_r5_e2e_doorbell_race.py::TestF158R5RealF136PostDelivery::test_ws_delivered_suppresses_coalesce_submit`.

**M3 — restore the deliver_pending bypass** (the pull-mode gate must route through
the cursor via `request_delivery`, not signal nothing / push directly). Neuter the
routing call so the gate no longer wakes through the cursor:
```
python -c "import pathlib; f=pathlib.Path('src/cli_agent_orchestrator/services/inbox_service.py'); s=f.read_text(); s=s.replace('                    request_delivery(terminal_id)\n','                    pass  # MUTANT M3 (dropped request_delivery)\n',1); f.write_text(s)"
```
(The anchor `                    request_delivery(terminal_id)` occurs exactly ONCE
in the file — the deliver_pending pull-mode gate — so `count=1` is unambiguous;
`git diff` will show only that line changed.)
- MUST go RED: `test/services/test_fx168_hotfix.py::TestFix4DeadD9Removed::test_deliver_pending_mailbox_pull_no_doorbell`
  and `test/services/test_f457_r2_gate_fixes.py::TestS1FailOpenDbError::test_pull_gate_routes_through_request_delivery`
  (both assert `request_delivery` is called once from the gate).

Revert any mutant: `git checkout -- src/cli_agent_orchestrator/services/inbox_service.py`.

## New tests (test_f476_r3_bypass_closure.py) — drive the real runner

- One wake per id, native transport: single native wake + cursor advance; second
  poll of the same still-pending row emits nothing.
- One wake per id, WS wins: WS fires once, native suppressed; WS-armed-but-fails →
  exactly one native fallback.
- Acked id never re-emitted: ack→rerun zero emits; reconnect (consumed cursor past
  row) zero emits; **bridge replay of the exact #388 07:47Z acked ids
  (2726,2727,2730,2733,2734,2735,2736) → zero emits across 3 replay polls.**
- Mutant ledger:
  - `test_mutant_emit_on_both_surfaces_is_red` — asserts WS and native are mutually
    exclusive for one id (WS∩native == ∅, exactly one transport). A mutant that
    dropped the `not outcome._ws_fired` guard would put the id on both probes → RED.
  - `test_mutant_drop_cursor_advance_reemits_is_red` + baseline
    `test_real_commit_advances_cursor_mutant_baseline` — the real `commit_wake`
    advances the cursor to the emitted row id (baseline asserts ==1); a mutant that
    no-ops the advance leaves it at 0 → observable divergence, RED.

## Verification (ALL on grok boxes via scripts/box-run.sh — laptop suites forbidden)

### Post-rebase re-verification (base 718849a4, HEAD 5fe33627) — grok-box-002

After rebasing onto fork main `718849a4` (F613 + SEED_OK), re-ran on grok-box-002
via box-run.sh (label `f476r3-rebased`), targeted set = new test file + the 6
updated test files + `test_f158_doorbell_fallback.py` + `test_fx168_hotfix.py` +
`test/services/test_inbox_service.py`:
- **Targeted tests: 115 passed** (0 failed).
- **black --line-length 100 --check: PASS** (12 files unchanged).
- **isort --profile black --line-length 100 --check-only: PASS** (clean).
Rebase was clean (zero conflicts); no regression in the touched area against the
new base.

### AUTHORITATIVE S1 — post-rebase full-suite same-box A/B (grok-box-002)

Both sides run on the SAME box (grok-box-002), `uv run pytest test/services/ -q`,
captured once each:

| side | commit | result |
|------|--------|--------|
| BASE | `718849a4` | **9 failed, 7154 passed**, 19 skipped, 3 xfailed |
| HEAD | `9e7c408b` | **9 failed, 7164 passed**, 19 skipped, 3 xfailed |

Delta: **+10 passes** (exactly this build's 10 new tests in
`test_f476_r3_bypass_closure.py`), **+0 failures**. The failing set is **byte-identical
across base and HEAD** — 9 nodes:

```
test/services/test_f516_d2.py::test_d2_fast_path_waiting_fires_on_first_eval
test/services/test_f516_fixtures.py::test_chooser_fixtures_render_the_resume_cwd_dialog_in_region
test/services/test_session_brief_contract.py::test_absent_field_keeps_generated_settings_literal_bytes
test/services/test_stage0_flip_machinery.py::test_trace_manifest_is_byte_exact_and_has_36_hits
test/services/test_stage0b_receiver_evidence.py::test_d6_auto_responder_publishes_full_frame_then_reclassifies_region[False]
test/services/test_stage0b_receiver_evidence.py::test_d6_auto_responder_publishes_full_frame_then_reclassifies_region[True]
test/services/test_wp2s3_start_status_bootstrap.py::test_codex_seed_and_interactive_share_resolved_model_config
test/services/test_wp_watchdog_delegation.py::test_legacy_inbox_migration_and_null_park_warm_are_false
test/services/test_wpdt_delivery_truth.py::TestAC7DoctrineArmingStep::test_doctrine_arming_section_exists
```

This matches the EMPIRICAL gate's own observation (7164/7154, identical 9-node set,
incl. the `wp2s3` SEED_OK stub introduced by the 718849a4 base). Every failure is
present at the base 718849a4 and is therefore NOT introduced by this build; my diff
(4 src + 8 test files) references none of the failing tests' code paths
(expire_after_s / _migrate_ / session_brief / auto_responder / park_warm / SEED_OK
model resolution). box-run labels: `f476r3-ab-head`, `f476r3-ab-base`.

### Historical full-suite verification (pre-rebase, base e64684f9, HEAD 306178da)

Superseded by the post-rebase A/B above; retained for the record.

Boxes used: **grok-box-002** (fmt/mypy/targeted A/B), **grok-box-004** (final full
suite). box-1 never used; box-3 was unreachable/auto-suspended and skipped.

- **black --line-length 100 --check** (touched files): PASS (10 files unchanged).
- **isort --profile black --line-length 100 --check-only**: PASS (clean).
- **mypy --strict parity** (per-file error counts, base `origin/main` vs HEAD, same box):
  - `clients/database.py`: 34 → 34 (unchanged)
  - `services/mailbox_service.py`: 8 → 8 (unchanged)
  - `services/inbox_service.py`: 57 → 57 (unchanged)
  - My edited line ranges in inbox_service.py produce ZERO mypy errors. Pre-existing
    errors are SQLAlchemy `Column[...]` typing noise in unrelated functions.
  - **Net new mypy --strict errors introduced: 0.** (NOTE: the EMPIRICAL gate flagged
    S2 mypy parity as the reviewer's own incomplete re-run — box contention + command
    quoting — to be re-run by a fresh reviewer; the numbers above are this build's
    pre-rebase measurement and should be re-confirmed at HEAD 9e7c408b.)
- **test/services/ full suite** at HEAD 306178da (grok-box-004):
  **8 failed, 7165 passed, 19 skipped, 3 xfailed** (191s). (Pre-rebase base
  e64684f9 had a different failing set — the F578 `expire_after_s` migration and
  base drift; superseded by the post-rebase 9-node A/B above.)
  - All tests affected by MY change pass (f158_doorbell_fallback, fx168_hotfix,
    f158_r2, f158_r5, f457×2, f413, and the 10 new r3 tests).

## Box-actions ledger

- **box-run.sh invocations** (label — command summary):
  - `f476r3-fmt` (box-002) — fetch+checkout cao/60f65f33; `uv sync --group dev`;
    black --check + isort --check on touched files.
  - `f476r3-checkmypy` (box-002) — black --check; `mypy --strict` on the 3 touched
    src files at HEAD.
  - `f476r3-mypyfiles` (box-002) — per-file mypy error counts at HEAD + grep my
    edited ranges (zero errors).
  - `f476r3-mypybase` (box-002) — `mypy --strict` per-file counts at origin/main
    (base) for parity.
  - `f476r3-suite` (box-002) — first full test/services run (15 fail; pre-fix of
    f158_doorbell_fallback/fx168_hotfix tests).
  - `f476r3-faillist` (box-002) — read-only grep of saved suite output.
  - `f476r3-basefails` (box-002) — 7 suspicious tests at origin/main.
  - `f476r3-headfails` / `f476r3-errdetail` (box-002) — isolated serial runs +
    error detail of the base-drift failures.
  - `f476r3-truebase` (box-002) — the 15 failing tests at TRUE base e64684f9
    (`cao/60f65f33-base`): 8 fail / 11 pass (proves the 8 pre-exist).
  - `f476r3-suite2` (box-004) — final full test/services run at pre-rebase HEAD
    306178da: 8 fail / 7165 pass (only the pre-existing 8).
  - `f476r3-rebased` (box-002) — post-rebase (base 718849a4, HEAD 5fe33627):
    targeted set (new file + 7 updated + test_inbox_service.py) = 115 passed;
    black --check + isort --check PASS.
  - `f476r3-ab-head` (box-002) — post-rebase full test/services at HEAD 9e7c408b:
    9 failed / 7164 passed (AUTHORITATIVE S1 head side).
  - `f476r3-ab-base` (box-002) — post-rebase full test/services at base 718849a4:
    9 failed / 7154 passed (AUTHORITATIVE S1 base side; identical 9-node set → my
    build adds +10 passes, 0 new failures).
- **raw ssh:** none (all via box-run.sh).
- **checkout SHA left on boxes:** box-002 and box-004 left at `cao/60f65f33` HEAD,
  clean; temp probe branches (`_base_probe`, `_ab_base`, `cao/60f65f33-base`)
  deleted on the box after use.
- **env mutations:** `uv sync --group dev` created/updated the box `.venv` from the
  committed lockfile (no lockfile change). No apt/pip/version bumps.
- **temp files on box:** `/tmp/f476r3-suite-run.txt`, `/tmp/f476r3-suite2.txt`,
  `/tmp/f476r3-sync*.txt` (in /tmp; disposable).
- **deviations (honestly stated):**
  1. Applied `black` in WRITE mode on the LAPTOP (local black 26.5.1) to fix
     formatting, because the fx121 pre-commit hook blocks `git add/commit` inside a
     box-run.sh command when the laptop cwd is the root repo (couldn't commit-from-box).
     black is a deterministic formatter, not a test suite; the box then confirmed
     `black --check` PASS. No laptop *suite* was run post-steer.
  2. The supervisor's original "read the r3 report" instruction could not be
     satisfied (no such artifact); scope was re-confirmed with the supervisor
     (decision: option A) before building.

## Laptop-load / work-location compliance

Local `.venv` deleted; verified NO `.venv` remains anywhere under
`/data/cao-scratch/60f65f33`. No pytest/mypy/uv process ran on the laptop after the
steer (the only such processes observed were OTHER lanes' box-run.sh jobs). All
suites, black/isort, and mypy ran on grok boxes via scripts/box-run.sh.

Work-location contract honored:
- Code + commits: the `cao/60f65f33` worktree at
  `/data/cao-scratch/60f65f33/f476-r4` (the supervisor's rebase command targeted
  this exact path).
- Scratch + this report: under `/data/cao-scratch/60f65f33/` only.
- Boxes used: **grok-box-002** (fmt/mypy/targeted A/B + post-rebase re-verify),
  **grok-box-004** (pre-rebase full suite). grok-box-1 never used; grok-box-3 was
  unreachable/auto-suspended and skipped.
- Paths written under (laptop): `/data/cao-scratch/60f65f33/f476-r4/` (worktree
  tree: source edits, test files, `f476-r3-build-report.md`). Nothing written
  outside `/data/cao-scratch/60f65f33/`.
- Paths written on boxes: `/tmp/f476r3-*.txt`, `/tmp/f619|f620` (other lanes),
  box `.venv` via `uv sync` (from committed lockfile).
