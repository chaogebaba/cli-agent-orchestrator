# F620 (#476) build report

**Commit:** `ce4fa12f4bb2457d4296e8bf8d0d5ba3565471c0`
**Fork/branch:** `cao/a778fc81` off main `718849a4`
**Repo:** `cli-agent-orchestrator` (fork worktree `.cao/worktrees/a778fc81`)

Configurable worktree root (defaults off `/`, onto `/data/cao-scratch`) plus a
laptop shim that denies `pytest`/`mypy`/`uv sync|venv|run pytest|run mypy` on the
laptop for WORKER terminals when an offload box is active.

---

## Post-gate commit 9626456e: reason + box evidence

**Protocol note:** commit `9626456e` landed on `cao/a778fc81` AFTER the gate had
reviewed `463e06f0`, which reset the vote (E-TIP-MOVED). It should not have been
committed without supervisor request; recorded here for the re-gate at the new
tip.

**Why it was needed.** The FULL `test/services` box run (grok-box-006, label
`f620-services-final`) — which ran only after the gate saw `463e06f0` — surfaced
failures the earlier two-file run had not exercised:
- `test/services/test_worktree_branch_integrity.py` — **14 ERRORS**. Root cause:
  F620's new `/data/cao-scratch/worktrees/<repo-basename>` default. Every fixture
  repo in that file is named `repo`, so on a box with a writable `/data` they all
  resolved to the SAME `/data/cao-scratch/worktrees/repo/<terminal_id>` and
  collided across tests / xdist workers (`git worktree add ... already exists`).
- `test/services/test_terminal_service_full.py::TestCreateTerminalWorktree::test_use_worktree_rolls_back_the_worktree_on_a_later_failure`
  — **1 FAILURE**. The rollback now forwards the created checkout path, so the
  call is `remove_worktree("/repo", "test1234", <worktree_path>)`; the assertion
  still expected the 2-arg form.

Both are PRE-EXISTING tests broken by intended F620 semantics, not new tests.

**The fix (9626456e).**
- `test_worktree_branch_integrity.py`: autouse fixture pins `CAO_WORKTREE_ROOT`
  to a per-test tmp dir — exactly the knob F620 adds, restoring per-test
  isolation regardless of host `/data` writability.
- `test_terminal_service_full.py`: assertion updated to the 3-arg
  `remove_worktree` call.

**Box evidence for 9626456e** (grok-box-004, label `f620-postgate`, checkout
`9626456e`): `uv run pytest -q -m "not live and not e2e"
test/services/test_worktree_branch_integrity.py
test/services/test_terminal_service_full.py` → **106 passed, 4 skipped,
1 xfailed in 6.32s**. (Earlier at `463e06f0`/`ce4fa12f`-tree the same files gave
14 errors + 1 failure.)

---

## 1. Files written / touched

All code + commits are under the assigned worktree root
`/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/a778fc81/`.

Modified:
- `src/cli_agent_orchestrator/services/worktree_service.py`
  - `_providers_toml_worktree_root()` — reads `[worktrees] root` from
    providers.toml (via `settings_service.PROVIDER_DEFAULTS_FILE`); never raises.
  - `_data_scratch_writable()` — `/data/cao-scratch` exists + writable.
  - `resolve_worktree_root(repo_root) -> (root, in_repo)` — precedence:
    env `CAO_WORKTREE_ROOT` > providers.toml `[worktrees] root` >
    `/data/cao-scratch/worktrees/<repo-basename>` (when writable) >
    `<repo>/.cao/worktrees` (in-repo fallback, `in_repo=True`).
  - `worktree_path_for()` now routes through `resolve_worktree_root`
    (`<root>/<terminal_id>`).
  - `create_worktree()` — makes the root dir up front (off-repo roots have no
    parent yet), and only writes the `.gitignore` for the in-repo case.
  - `remove_worktree(repo_root, terminal_id, worktree_path=None)` — new optional
    explicit path so teardown targets the physical checkout git recorded even if
    the configured root changed between create and teardown.
- `src/cli_agent_orchestrator/services/terminal_service.py`
  - create-terminal worktree rollback passes the created `working_directory`
    to `remove_worktree`.
  - teardown (`dismantle_terminal_runtime`) prefers CAO's stored `worktree_info`
    (`repo_root` + `worktree_path`, authoritative) over parsing the pane cwd —
    required because an off-repo checkout no longer contains the in-repo
    `.cao/worktrees` marker `parse_worktree_path` keys on. `git worktree remove`
    still runs from the real repo root, i.e. from `.git/worktrees` truth.
    Path-parse retained as the in-repo / pre-F620 fallback.
  - WORKER-only shim PATH injection at the existing-session env-composition seam
    (`_create_session_or_window_locked`, the `else`/`create_window` branch):
    `if caller_id:` resolve repo root from `resolved_working_directory`
    (`find_repo_root`, `WorktreeError` → no shim) then `laptop_shim.maybe_shim_env`.
    Supervisor (`caller_id is None`) and operator new-session launches are never
    shimmed. Import added: `laptop_shim`.

Added:
- `src/cli_agent_orchestrator/services/laptop_shim.py` — the decision seam:
  `should_inject_shim`, `shim_dir_for`, `compose_shim_path`, `maybe_shim_env`,
  `_boxes_tsv_has_active_row`. All conditions: worker + resolvable repo root +
  active box row in `<repo>/scripts/boxes.tsv` + `LAPTOP_OK` unset + shim dir
  exists. Both `scripts/boxes.tsv` and `scripts/laptop-shims` are resolved
  relative to the repo root the terminal runs in.
- `scripts/laptop-shims/pytest` — deny (exit 97) unless `LAPTOP_OK`; then execs
  the real pytest found by stripping its own dir from PATH.
- `scripts/laptop-shims/mypy` — same shape as pytest.
- `scripts/laptop-shims/uv` — passthrough to real uv EXCEPT `sync`, `venv`,
  `run pytest`, `run mypy` (deny, exit 97). Detects the subcommand past leading
  global flags; finds the real uv by stripping its own dir from PATH.
- `test/services/test_laptop_shim.py` — shim subprocess behaviour + the Python
  decision seam.

Tests extended:
- `test/services/test_worktree_service.py` — `TestResolveWorktreeRoot` (4
  precedence cases + empty-env), `TestOffRepoWorktreeLifecycle` (real-git
  off-repo create/list/teardown, in-repo still gitignores), plus an autouse
  fixture pinning root resolution to in-repo so the pre-existing real-git tests
  stay deterministic regardless of the host's `/data`/env/toml.

Scratch/report paths written under: `/data/cao-scratch/a778fc81/` (created; used
for scratch only). This report itself is at the worktree root per the brief.

## 2. Denial message + exit code (verbatim)

`LAPTOP-DENIED: run on a grok box via scripts/box-run.sh (set LAPTOP_OK=1 to override)`
exit code **97**.

## 3. Tests

Root resolution precedence (4 cases): env wins; toml wins when env unset;
`/data` default when writable + nothing configured; in-repo fallback when
`/data` absent. Plus empty-env-ignored and off-repo lifecycle.

Shim deny/allow/passthrough (subprocess, unit-tier safe — no venv, no real
suite run): pytest/mypy deny→97 and `LAPTOP_OK` passthrough to a fake real
binary; uv deny for sync/venv/run-pytest/run-mypy (incl. behind a global flag)
and passthrough for pip/tree/run-echo/lock; `LAPTOP_OK` passes heavy through.

Env composition worker vs supervisor, boxes present/absent: `should_inject_shim`
(worker+active→inject; supervisor→never; boxes absent→no; all-frozen→no;
`LAPTOP_OK`→no; no repo root→no; missing shim dir→no) and `maybe_shim_env`
(worker gets shim-prefixed PATH; supervisor gets no PATH key; boxes absent
unchanged; idempotent no double-prefix).

### Box verification (grok-box-004 / -006, branch cao/a778fc81)
Latest head under test: `9626456e1c3a22c0c5ea3d65b747ea99c07ed059` (adds the two
test-isolation fixes below; code behaviour of commit `ce4fa12f` unchanged).

- `uv run pytest -q -m "not live and not e2e" test/services/test_worktree_service.py
  test/services/test_laptop_shim.py` (grok-box-004 @ ce4fa12f) → **60 passed**.
- Re-verify of the previously-broken worktree tests (grok-box-004 @ 9626456e):
  `test_worktree_branch_integrity.py test_worktree_service.py test_laptop_shim.py
  test_terminal_service_full.py::TestCreateTerminalWorktree` → **93 passed, 1 xfailed**.
- Full `test/services` (grok-box-006 @ 9626456e) → **7192 passed, 9 failed, 19
  skipped, 3 xfailed** in 179s. The 9 failures are ALL pre-existing and
  unrelated to F620 (see analysis below).
- `uv run mypy --strict` base-vs-head on touched src (grok-box-004):
  - HEAD (ce4fa12f): 2 errors, both pre-existing `CompletedProcess [type-arg]`
    in `worktree_service.py` (`_run_git` / `_run_git_bounded`, untouched), now
    at lines 72 / 423.
  - BASE (718849a4): the SAME 2 errors in `worktree_service.py` at pre-F620
    lines 51 / 299.
  - **Parity: 0 new mypy --strict errors.** New file `laptop_shim.py` is clean.

### Pre-existing failure analysis (NOT caused by F620)
Confirmed identical failures at BASE `718849a4` on the SAME box (grok-box-004):
`test_stage0b_receiver_evidence` (x2), `test_wp2s3_start_status_bootstrap`,
`test_wp_watchdog_delegation`, `test_wpdt_delivery_truth` — root cause a
shared-DB schema drift (`sqlite3.OperationalError: no such column:
inbox.expire_after_s`), entirely unrelated to worktrees/shims. The full-suite
run on grok-box-006 also showed `test_session_brief_contract` /
`test_stage0_flip_machinery` (byte-exact manifest tests that vary by box
environment) — likewise untouched by F620.

### Two follow-up test-isolation fixes (commit 9626456e) — required by F620 semantics
- `test/services/test_worktree_branch_integrity.py`: added an autouse fixture
  pinning `CAO_WORKTREE_ROOT` to a per-test tmp dir. WITHOUT F620 these tests
  provisioned in-repo; WITH the new `/data/cao-scratch` default they all shared
  `/data/cao-scratch/worktrees/repo/<terminal_id>` (every fixture repo is named
  `repo`) and collided across tests/xdist workers → 14 ERRORS on a box with a
  writable `/data`. The fixture restores per-test isolation regardless of host.
  This is exactly the config knob F620 adds, used as intended.
- `test/services/test_terminal_service_full.py`:
  `test_use_worktree_rolls_back_the_worktree_on_a_later_failure` updated to
  expect the new 3-arg `remove_worktree("/repo", "test1234", <worktree_path>)`
  — the rollback now forwards the created checkout path (intended F620 change).

### Local verification done on the laptop (allowed)
- `python3 -m py_compile` on all touched `.py` files — OK.
- `isort --profile black -l100` + `black -l100` on all touched `.py` — clean.
- Shim shell scripts smoke-tested directly (not pytest): deny→97, `LAPTOP_OK`
  passthrough, uv selective passthrough, real-binary discovery by PATH-strip,
  global-flag-before-subcommand detection — all as designed.

## 4. Mutant ledger (box-confirmed test design)
- Drop the precedence env case in `resolve_worktree_root` → `TestResolveWorktreeRoot::test_env_var_wins_over_everything` goes RED (would resolve to the toml root instead).
- Shim exits 0 instead of 97 → `TestPytestMypyShims::test_denies_by_default_exit_97` / `TestUvShim::test_denies_heavy_subcommands_exit_97` go RED.

## 5. Boxes used
**grok-box-004** and **grok-box-006** (fleet grok-box-2..8; never grok-box-1).
box-actions ledger:
- `scripts/box-run.sh f620-pytest -- fetch cao/a778fc81 + checkout ce4fa12f + pytest <2 files>` → grok-box-004.
- `CAO_BOXES=grok-box-004 f620-mypy-head / f620-mypy-base` → mypy --strict head + base (base re-checked-out to head after).
- `f620-services` / `f620-services-final` → full test/services (grok-box-004 then grok-box-006).
- `CAO_BOXES=grok-box-004 f620-diag / f620-diag2 / f620-base-unrelated` → failure triage (incl. base-vs-head of the unrelated failures).
- `CAO_BOXES=grok-box-004 f620-reverify` → 93 passed post-fix at 9626456e.
- `f620-cleanup / f620-cleanup4 / f620-cleanup6` → restore each box repo to `main`.
- Raw ssh (read-only): `git remote -v` / load / branch peeks on -002/-004/-006.
- Checkouts left at: grok-box-004 and grok-box-006 repos on `main` (e64684f9), clean trees.
- Env mutations: none (no installs, no lockfile changes).
- Temp files left on boxes: none (used no box /tmp; laptop scratch only).
- Deviations: full `test/services` A/B split across boxes (-004 base-unrelated
  vs -006 final) — the 5 core pre-existing failures were still confirmed at
  BASE on the SAME box (-004); the 2 byte-exact manifest failures are known
  cross-box environmental. Reported honestly, not hidden.

## 6. Push note
Branch `cao/a778fc81` pushed to origin (`chaogebaba/cli-agent-orchestrator`) as a
SCRATCH branch for box fetch-by-SHA delivery only — never main, no PR — per the
supervisor's authorization. Scratch on the laptop stayed under
`/data/cao-scratch/a778fc81/`; nothing written to `/tmp`.

Commits on the branch:
- `ce4fa12f` — implementation (code of record).
- `c9e38de2` / `463e06f0` — build report (superseded by this update).
- `9626456e` — test-isolation fixes surfaced by the full-suite box run.
