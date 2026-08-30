# F619 (#475) build report — terminal log rotation/retention + E_DISK_LOW spawn guard

Fork branch `cao/3470a5b1`, off main `718849a4`. Code commit: `e96cc679c5e93ad9391a359f0c1c8e69c0c0b2d2`.

## Issue recap
`~/.aws/cli-agent-orchestrator/logs/terminal/<id>.log` are never rotated or
pruned; dead-terminal logs (single files up to 322M) accumulate until `/` fills,
which ENOSPC-truncates a source file mid-`str_replace` and breaks git. Fix asks:
(1) per-terminal rotation, (2) retention on delete + at startup with total cap,
(3) `assign`/`handoff` refuse to spawn below a free-disk floor (typed
`E_DISK_LOW`). Encode, don't document.

## What was built

### (1) Per-terminal log cap — rotation
- `src/cli_agent_orchestrator/services/log_writer.py`
  - New `_rotate_if_needed(path, max_bytes)`: rolls `<id>.log` → `<id>.log.1`
    (via `os.replace`, atomic same-FS) once the active file reaches the cap,
    keeping **exactly one** backup. Non-positive cap disables; every OS error is
    swallowed with a WARNING so a failed rotation never stops log persistence or
    crashes the writer loop.
  - `LogWriter._write(path, data, max_bytes)`: rotates BEFORE appending.
  - Batch loop resolves `max_bytes = logs.max_file_mb * 1MB` **once per batch**
    (not per event) — hot path.

### (2) Retention — delete-prune, startup age-prune, total cap
- `src/cli_agent_orchestrator/services/cleanup_service.py`
  - `prune_terminal_log(terminal_id)`: removes `<id>.log` + `<id>.log.1`.
    Best-effort/exception-safe; returns count.
  - `prune_terminal_logs_at_startup()`: two passes over `TERMINAL_LOG_DIR`, both
    **skipping any log whose terminal id still has a DB row** (`_live_terminal_ids`):
    - Pass 1 (age): delete dead-terminal logs with `mtime` older than
      `logs.retention_hours`.
    - Pass 2 (total cap): if surviving dead logs still exceed `logs.max_total_mb`,
      delete **oldest-mtime-first** until under the cap. Live logs are never
      counted or deleted.
  - Restore artifacts (`.scrollback` / `.snapshot.json`) are deliberately NOT in
    the managed suffix set — they stay on the existing `RETENTION_DAYS` sweep.
- `src/cli_agent_orchestrator/services/terminal_service.py`
  - `_delete_terminal_under_lease` calls `prune_terminal_log(terminal_id)` right
    AFTER the scrollback/snapshot restore artifacts are captured, so the big
    `.log` is reclaimed on delete. Best-effort — never fails the delete.
- `src/cli_agent_orchestrator/api/main.py`
  - lifespan schedules `prune_terminal_logs_at_startup` off-thread, plus a
    `warn_if_disk_low_at_startup()` WARNING.

### (3) E_DISK_LOW spawn guard
- `src/cli_agent_orchestrator/utils/disk_guard.py` (new)
  - `check_spawn_disk(worktree_root) -> Optional[str]`: `shutil.disk_usage` on the
    FS holding the worktree root AND the logs dir; returns a typed string that
    STARTS WITH `E_DISK_LOW:` naming the path and free GB when either is below
    `disk.min_free_gb`, else `None`. Unmeasurable mounts never block a spawn.
  - `warn_if_disk_low_at_startup()`: same check, logs WARNING only.
- `src/cli_agent_orchestrator/mcp_server/server.py`
  - `_assign_impl`: checks AFTER `working_directory` is resolved and BEFORE
    `_create_terminal` (no orphan window); returns `{success:False, error:<E_DISK_LOW…>}`.
  - `_handoff_impl`: checks after `strict_supervisor_cwd()` and before the
    run-step call; returns a failed `HandoffResult`.

### Config — same loader as the rest of config
- `src/cli_agent_orchestrator/services/settings_service.py`
  - `get_logs_settings()` reads `providers.toml` `[logs]` via the existing
    `get_provider_defaults`; keys `max_file_mb` (50), `retention_hours` (24),
    `max_total_mb` (2048).
  - `get_disk_settings()` reads `[disk]`; key `min_free_gb` (5).
  - `_coerce_positive_number` rejects bool / non-numeric / non-positive per key
    and falls back to the built-in default with a WARNING — a toml typo can never
    disable a cap or set it to zero.
- `providers.toml.default`: added commented `[logs]` / `[disk]` sections.

## Tests
`test/services/test_f619_log_retention.py` (local `settings_file` fixture):
- Config loaders: defaults-when-missing, reads both sections, invalid→default
  (0/-5/string/bool parametrized), partial section keeps other defaults.
- Rotation: rolls at cap, no-rotate below cap, keeps-only-one-backup, zero-cap
  disables, write-rotates-then-appends.
- Delete-prune: removes log+backup, missing is no-op, leaves other terminals.
- Startup age-prune: old dead pruned, recent dead kept,
  **live-terminal-log-never-pruned-even-if-old**.
- Total cap: **oldest-first**, skips live terminal.
- E_DISK_LOW: fires below floor, does-not-fire above, names offending path,
  startup warning logs+returns. `shutil.disk_usage` monkeypatched.

### Required mutation checks (explicit tests)
- Retention filter inverted → RED: `test_mutation_retention_filter_inverted`
  (old must be deleted, fresh must survive; an inverted `mtime < cutoff` flips both).
- E_DISK_LOW threshold removed → RED: `test_mutation_disk_threshold_removed`
  (1GB free vs 5GB floor MUST return a string; dropping `free < floor` returns None).

## Static checks (laptop — allowed, not compute-heavy)
- `black -l100 --check`: clean on all 8 changed files.
- `isort --profile black -l100 --check`: clean (isort reordered `cleanup_service`
  imports; `_utcnow` still imported).
- `python -m py_compile`: OK on all changed modules + test file.
- Local NON-pytest smoke via `python -c` (unit-sized, boxes-rule-compliant):
  disk_guard fires/clears correctly and names both paths; rotation rolls + keeps
  one backup + write-rotates-then-appends; startup age-prune keeps LIVE + fresh
  and deletes old-dead; total-cap deletes oldest-first. ALL PASS.

## NOT yet run — blocked (honest deviation flag)
- `mypy --strict` base-vs-head parity: **NOT run.**
- Full `pytest` (incl. this new test file) on a grok box: **NOT run.**
- Reason: box-ops requires code to reach a box EXCLUSIVELY via `git fetch`/`checkout`
  of a **PUSHED** commit, but the task says **"No push."** These two mandatory
  constraints conflict for any box-side mypy/pytest run. Per the stop-and-ask
  directive I did not silently push or silently skip; raising to the supervisor
  for a decision (push a work branch for box CI, or accept laptop-local
  verification, or another route).

## Paths written under (this run)
- Code + commit: worktree `.cao/worktrees/3470a5b1` ONLY.
- Scratch: `/data/cao-scratch/3470a5b1/` (one throwaway PEP-758 probe file).
- No `/tmp` scratch beyond ephemeral `tempfile.mkdtemp()` inside the smoke `python -c`.
- No `.venv` created, no pytest/mypy/uv sync on the laptop.

## Box used
- NONE yet (see blocked section). No box-run.sh invocations, no ssh to any box.
