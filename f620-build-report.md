# F620 (#476) build report

**Commit:** `ce4fa12f4bb2457d4296e8bf8d0d5ba3565471c0`
**Fork/branch:** `cao/a778fc81` off main `718849a4`
**Repo:** `cli-agent-orchestrator` (fork worktree `.cao/worktrees/a778fc81`)

Configurable worktree root (defaults off `/`, onto `/data/cao-scratch`) plus a
laptop shim that denies `pytest`/`mypy`/`uv sync|venv|run pytest|run mypy` on the
laptop for WORKER terminals when an offload box is active.

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

### Local verification done on the laptop (allowed)
- `python3 -m py_compile` on all 5 touched `.py` files — OK.
- `isort --profile black -l100` + `black -l100` on all touched `.py` — clean.
- Shim shell scripts smoke-tested directly (not pytest): deny→97, `LAPTOP_OK`
  passthrough, uv selective passthrough, real-binary discovery by PATH-strip,
  global-flag-before-subcommand detection — all as designed.

## 4. Mutant ledger (design intent; box-run confirmation pending — see §6)
- Drop the precedence env case in `resolve_worktree_root` → `TestResolveWorktreeRoot::test_env_var_wins_over_everything` goes RED (would resolve to the toml root instead).
- Shim exits 0 instead of 97 → `TestPytestMypyShims::test_denies_by_default_exit_97` / `TestUvShim::test_denies_heavy_subcommands_exit_97` go RED.

## 5. Box used
NONE YET — see §6. All laptop steps above avoided pytest/mypy/uv-sync per the
work-location rule.

## 6. BLOCKER surfaced to supervisor (not silently worked around)
The brief requires every pytest/mypy/uv-sync run to go to a grok box via
`scripts/box-run.sh`, AND says **No push**. Per the `box-ops` law, code reaches
a box EXCLUSIVELY by `git fetch origin <branch> && git checkout <sha>` of a
**pushed** commit (scp/format-patch/`git am` are forbidden). These two
constraints conflict: I cannot run the box suite / `mypy --strict` base-vs-head
without pushing branch `cao/a778fc81`. I committed `ce4fa12f`, ran every
laptop-safe check, and paused for the supervisor's decision rather than either
pushing without authorization or running the suite on the laptop.
