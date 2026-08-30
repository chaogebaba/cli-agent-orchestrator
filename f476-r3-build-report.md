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

- **Worktree:** `/data/cao-scratch/60f65f33/f476-r4`
- **Branch:** `cao/60f65f33` (pushed to origin as a scratch branch; never main)
- **Base:** `e64684f9` (fork main HEAD at task start; note origin/main is *behind*
  this at `b2814464` — a pre-existing local-ahead-of-origin state)
- **HEAD:** `306178da2eb4f7c9f86e3ba4c76718f0b86d43cc`
- Commits (on top of e64684f9):
  - `0a411258` F476 r3 (#388): route deliver_pending push + WS doorbell through the wake cursor
  - `c21ba8cd` F476 r3 (#388): black --line-length 100 formatting
  - `306178da` F476 r3 (#388): update f158_doorbell_fallback + fx168_hotfix tests for cursor-routed wake
- **Base ref pushed for A/B:** `cao/60f65f33-base` → `e64684f9` (deleted from boxes
  after use; still on origin for audit).

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

## Files changed (vs base e64684f9)

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

Tests (6):
- `test/services/test_f476_r3_bypass_closure.py` (NEW) — 10 tests (see below).
- Updated to the r3 contract (blueprint Test-break list — old-behavior pins):
  `test_f413_orm_listeners.py`, `test_f158_r2_doorbell_regression.py`,
  `test_f158_r5_e2e_doorbell_race.py`, `test_f158_doorbell_fallback.py`,
  `test_f457_r2_gate_fixes.py`, `test_f457_wake_gate_dedupe.py`,
  `test_fx168_hotfix.py`.

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
  - **Net new mypy --strict errors introduced: 0.**
- **test/services/ full suite** at HEAD 306178da (grok-box-004):
  **8 failed, 7165 passed, 19 skipped, 3 xfailed** (191s).
  - All 8 failures are PRE-EXISTING at base e64684f9 (verified same-box A/B: the same
    8 fail at `cao/60f65f33-base`): `test_f516_d2`, `test_f516_fixtures`,
    `test_session_brief_contract`, `test_stage0_flip_machinery`,
    `test_stage0b_receiver_evidence[False/True]`, `test_wp_watchdog_delegation`,
    `test_wpdt_delivery_truth::...doctrine_arming`. Root causes observed:
    hook-count fixture drift, `sqlite3.OperationalError: no such column:
    inbox.expire_after_s` (F578 migration in the test's own DB setup), byte-exact
    trace-manifest drift, missing `doctrine/sections/shared/ws-arming.md`, tmux
    region rendering. My commits touch NONE of these code paths (diff vs e64684f9
    is 4 source + 6 test files; references zero of expire_after_s/_migrate_/
    session_brief/auto_responder/park_warm).
  - All tests affected by MY change pass (f158_doorbell_fallback, fx168_hotfix,
    f158_r2, f158_r5, f457×2, f413, and the 11 new r3 tests).

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
  - `f476r3-suite2` (box-004) — final full test/services run at HEAD 306178da:
    8 fail / 7165 pass (only the pre-existing 8).
- **raw ssh:** none (all via box-run.sh).
- **checkout SHA left on boxes:** box-002 and box-004 each left at
  `cao/60f65f33` (306178da), clean; temp probe branches (`_base_probe`,
  `cao/60f65f33-base`) deleted on the box after use.
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

## Laptop-suite steer compliance

Local `.venv` deleted; local suite run was killed per the user order. All suites,
black/isort, and mypy after the steer ran on grok boxes via scripts/box-run.sh.
