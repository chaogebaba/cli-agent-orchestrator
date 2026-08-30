# F636 (#491) build report — laptop-shim guard made live

**Lane:** `cao/102d5fe3`  ·  **Base:** fork main `1a82b5cd`  ·  **Code tip:** `d03e1f2f`
**Worktree:** `.cao/worktrees/102d5fe3`

## Defect (confirmed empirically)

`should_inject_shim` (`services/laptop_shim.py`) required BOTH
`scripts/boxes.tsv` (active row) AND `scripts/laptop-shims/` under ONE
`repo_root`. `repo_root` = `git rev-parse --show-toplevel` from the worker's
cwd (`worktree_service.find_repo_root`), which stops at the fork's own `.git`.
But the two files live in DIFFERENT repos — verified on disk at `1a82b5cd`:

| repo | `scripts/boxes.tsv` | `scripts/laptop-shims/` |
|------|--------------------|-------------------------|
| root `cli-subagents` | present, 7 active rows | **absent** |
| fork `cli-agent-orchestrator` (nested) | **absent** | present (mypy/pytest/uv) |

`git rev-parse --show-toplevel` from the fork returns the fork; from the fork's
parent returns the root — the fork is a direct child. So the single-root
predicate was `False` for every worker everywhere and the F620 guard never
fired. F620's r2 EMPIRICAL gate (B=0 S=0) missed it because every arm built a
fixture repo with BOTH files under one root and exercised the predicate
directly — never through a production worker spawn (the blind spot closed here).

## Fix (issue's preferred shape)

Kept the shim dir **fork-relative** (that is where the wrappers physically
are) and taught only the `boxes.tsv` lookup to walk up **one level max**:

- New `_active_boxes_tsv_for(repo_root)`: probes `<repo_root>/scripts/boxes.tsv`
  first, then `<dirname(repo_root)>/scripts/boxes.tsv`. Explicit single-level
  ascent; idempotent at the filesystem root (`dirname("/") == "/"`, deduped),
  so no infinite walk and a grandparent repo can never satisfy the guard.
- `should_inject_shim` now calls `_active_boxes_tsv_for(...) is None` in place
  of the single-root `_boxes_tsv_has_active_row(<repo_root>/…)` check.
- The non-nested layout (both files under one root) is unchanged: the
  `repo_root` candidate is checked first and matches.

Why this shape over the alternatives in the issue: seeding `boxes.tsv` into the
fork checkout couples install/redeploy to a mutable runtime roster (drifts, and
duplicates the source of truth); moving the shim dir into the root repo scatters
fork-owned assets out of the fork. Consulting the parent for the roster only is
the smallest change that matches the real repo topology and stays hermetically
testable. Argued and chosen per the issue's stated preference.

### Diff (production)
`services/laptop_shim.py`: +1 helper (`_active_boxes_tsv_for`), predicate
rewired to it, module docstring updated. No signature/behaviour change to
`maybe_shim_env`, `compose_shim_path`, `_boxes_tsv_has_active_row`.

## Tests

**Spawn-path AC (mandatory — closes the blind spot), `test/services/test_f636_shim_spawn_path.py`:**
Builds the REAL nested layout on disk (root git repo with active `boxes.tsv`;
nested fork git repo with `scripts/laptop-shims`) and drives the REAL
`create_terminal` worker branch with only resource deps stubbed —
`worktree_service.find_repo_root` and the whole `laptop_shim` compose path run
for real. Asserts on the `extra_env["PATH"]` the backend's `create_window`
actually receives:

1. `test_worker_spawn_on_nested_fork_gets_shim_prepended` — **mutant sentinel**:
   a worker (existing session + `caller_id`) with cwd inside the fork gets the
   fork's `scripts/laptop-shims` at the head of PATH.
2. `test_worker_spawn_with_all_frozen_fleet_is_not_shimmed` — same
   `create_window` path, all-frozen roster → no shim (negative arm on the real
   path, not just the predicate).
3. `test_operator_new_session_launch_is_not_shimmed` — operator launch goes
   through `create_session`, not the shimmed `create_window` seam
   (`create_window.call_count == 0`).

**Direct predicate coverage added to `test/services/test_laptop_shim.py`**
(`TestNestedBoxesTsvResolution`, 7 cases): resolver finds parent roster,
prefers own root when present, `None` when neither active, **walks up only one
level** (grandparent roster rejected), `should_inject_shim` True for a
nested-fork worker, False when parent all-frozen.

### Results (all on grok-box-002 via `scripts/box-run.sh`)
- Shim + spawn suites (`test_laptop_shim.py` + `test_f636_shim_spawn_path.py`):
  **39 passed** (sha `d03e1f2f`).
- Seam regression (shim + spawn + `test_worker_terminal_cap.py` +
  `test_terminal_service.py` + `test_terminal_service_full.py`):
  **184 passed, 4 skipped** — the shared `create_terminal` seam is unaffected.

### Mutation evidence (replayable at `d03e1f2f`)
- **Mutant:** revert the predicate to the single-root lookup —
  `if _active_boxes_tsv_for(repo_root) is None:` → `boxes_tsv =
  os.path.join(repo_root, _BOXES_TSV_SUBDIR); if not
  _boxes_tsv_has_active_row(boxes_tsv):` (applied by heredoc with an
  `assert count == 1` anchor guard).
- **Selectors run under mutant:**
  `test_f636_shim_spawn_path.py::test_worker_spawn_on_nested_fork_gets_shim_prepended`
  and `test_laptop_shim.py::TestNestedBoxesTsvResolution`.
- **Observed:** `2 failed, 5 passed`. The spawn sentinel failed with
  `PATH=''` (shim never fired) and the direct nested-predicate test failed;
  the 5 non-nested predicate cases stayed green — the mutant is caught by BOTH
  the spawn path (F620's exact blind spot) and the direct predicate.
- **Restore:** `cp` of the pre-mutant copy then `git checkout --` the file;
  post-restore the working tree was clean.

## mypy --strict A/B parity
Same box (grok-box-002), `src/cli_agent_orchestrator/services/laptop_shim.py`:
- base `1a82b5cd`: `Success: no issues found in 1 source file`
- head `d03e1f2f`: `Success: no issues found in 1 source file`
- error-line diff: empty → **parity, no new strict errors**.

## Interaction note (issue §7 / F634 AC21)
F634 D16 (shim host-awareness for box-hosted lanes) carries "gap closed" as an
explicit precondition. This build closes that gap (the guard now fires for a
real worker on the fork). F634's build lane can treat AC21's precondition as
satisfied at `d03e1f2f`.

## Box-actions ledger (grok-box-002; grok-box-1 FROZEN, untouched)
`box-run.sh` invocations (label — command summary):
- `f636-tests` — fetch cao/102d5fe3, checkout 1efa3167, pytest shim+spawn (1 failed: operator arm, session-exists mock — fixed in d03e1f2f).
- `f636-tests2` — checkout d03e1f2f, pytest shim+spawn → 39 passed.
- `f636-mutant` — checkout d03e1f2f, cp orig, apply single-root revert, pytest sentinel+nested → 2 failed/5 passed, restore file, `git checkout --` → clean.
- `f636-boxclean` — restored a pre-existing `D orchestrator/tmp/orch/upstream-remerge-report.md` (NOT mine — the box working tree had it deleted before I arrived), `git checkout main`.
- `f636-mypy` — `CAO_BOXES=box@grok-box-002` pinned; mypy --strict laptop_shim at head then base, back to main → parity.
- `f636-capsuite` — checkout d03e1f2f, worker-cap suite → 39 passed, back to main.
- `f636-svcsuite` — checkout d03e1f2f, full `test/services/` run; **released at my 120s tool timeout so box-run KILLED the process group mid-run** (~70%, partial `F` marks are killed-mid-flight, not real failures). Superseded by the tighter `f636-seam` run (184 passed).
- `f636-svcpeek` / `svcpeek2` / `svcpeek3` — read-only `tail` of the run log (svcpeek3 released at timeout).
- `f636-seam` — checkout d03e1f2f, shim+spawn+cap+terminal_service suites → 184 passed/4 skipped, back to main.
- `f636-finalclean` — read-only status check (HEAD e64684f9, main, clean).
- `f636-rmtmp` — `rm -f /tmp/f636-*.txt /tmp/f636_laptop_shim.orig`.

Raw ssh (read-only): one `ssh box@grok-box-002 'tail -8 /tmp/f636-svcsuite-run.txt'` — read-only, no state change.

Checkout left at: **main `e64684f9`, clean.** Env mutations: none (no installs/lockfile changes). Temp files left on box: none (f636 logs removed).

Deviations honestly stated:
- The full `test/services/` run did not complete — killed by the slot release
  at my client-side timeout, not a hang in my code. Covered instead by the
  targeted 184-test seam run over exactly the modules my change touches.
- Restored an unrelated pre-existing deletion in the box's fork checkout
  (`orchestrator/tmp/orch/upstream-remerge-report.md`) to leave the box clean;
  it was another lane's residue, not produced by this task.
