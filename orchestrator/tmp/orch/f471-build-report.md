# F471 (#326) — Build Report: tmux backend test-mock assertion fix

- **Branch:** `cao/f471-mock-sweep` (from `main` @ 92fe6295)
- **Claim:** Mechanical test-only fix. `test/backends/test_tmux_backend.py` mock
  `assert_called_once_with` assertions were missing the `allowed_blocked_values`
  kwarg that `TmuxBackend.create_session` / `TmuxBackend.create_window` now pass
  through to `TmuxClient` (src untouched).

## Diff summary (1 file, test-only)

`test/backends/test_tmux_backend.py`:

1. `test_create_session_delegates` — assertion updated to include
   `allowed_blocked_values=None`, matching the real call at
   `src/.../backends/tmux_backend.py:50-52`; reformatted to one-arg-per-line
   (file's existing multi-line style).
2. `test_create_window_delegates` — same: added `allowed_blocked_values=None`,
   matching `src/.../backends/tmux_backend.py:82-91`.

Style: exact-kwarg assertion (file's existing convention, same as F459 r2 class
fix). No `**kwargs`-tolerant loosening — kept exact to preserve drift detection.

## Sweep for the same latent mismatch class

Checked every `assert_called_once_with` in the file against the corresponding
`TmuxBackend` delegation call site in `src/.../backends/tmux_backend.py`:
`send_keys` (enter_count/force_bracketed_paste/submit_delay), `get_history`
(tail_lines/strip_escapes/full_history), `session_exists`, `kill_window`,
`send_special_key`, `capture_viewport`, `pipe_pane`, `stop_pipe_pane` — all
match exactly. **No other latent mismatches found; no further edits.**

## Test evidence

- Before (branch point 92fe6295):
  `uv run pytest test/backends/test_tmux_backend.py -q -o "addopts="` →
  **2 failed, 24 passed**
  (`TestTmuxBackendDelegation::test_create_session_delegates`,
  `TestTmuxBackendDelegation::test_create_window_delegates`)
- After fix:
  `uv run pytest test/backends/test_tmux_backend.py -q -o "addopts="` →
  **26 passed** (0 failed)
- Whole `test/backends/` dir after fix:
  `uv run pytest test/backends/ -q -o "addopts="` →
  **190 passed, 0 failed** (nothing new failing; one benign pre-existing
  basetemp prune warning from local pytest tmp config)

## Scope discipline

- src/ untouched; only `test/backends/test_tmux_backend.py` + this report.
- No design decisions: kwarg value `None` mirrors the actual default pass-through.
