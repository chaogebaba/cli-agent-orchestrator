# F452 Build Report — CAO_WORKTREE env export (#307)

## Summary

When `assign`/`handoff` provisions an isolated git worktree (`use_worktree=True`),
the worker's spawn environment now includes `CAO_WORKTREE=<abs worktree path>`.
Non-worktree workers do not receive the variable (absent, not empty).

## Diff (1 file touched for impl, 1 for tests)

**src/cli_agent_orchestrator/services/terminal_service.py** (+3 lines):
```python
# F452 (#307): Export the provisioned worktree path so workers can
# discover their isolated checkout programmatically instead of
# improvising scratch worktrees under /tmp.
env_vars["CAO_WORKTREE"] = working_directory
```

Added immediately after the `_worktree_info_dict` construction (the point where
`working_directory` holds the provisioned worktree path), inside the existing
`if use_worktree:` block. The `else:` branch (non-worktree) never sets the key.

**test/services/test_terminal_service_full.py** (+112 lines):
- `test_use_worktree_exports_cao_worktree_in_spawn_env` — asserts `extra_env["CAO_WORKTREE"]`
  equals the provisioned worktree path.
- `test_plain_assign_does_not_export_cao_worktree` — asserts `"CAO_WORKTREE" not in extra_env`.

## Pre-fix RED evidence

```
>       assert extra_env["CAO_WORKTREE"] == str(worktree_dir)
               ^^^^^^^^^^^^^^^^^^^^^^^^^
E       KeyError: 'CAO_WORKTREE'

FAILED test_use_worktree_exports_cao_worktree_in_spawn_env
1 failed, 1 passed in 3.80s
```

## Post-fix GREEN (local)

```
2 passed in 3.45s
```

## Full suite on box

```
box-run: acquired box@cursor for 'f452'
box-run: box@cursor fork checkout at d579191c
79 passed, 4 skipped in 4.47s
```

Module: `test/services/test_terminal_service_full.py`

## Box actions ledger

| Action | Box | Command | Outcome |
|--------|-----|---------|---------|
| rsync files | box@cursor | rsync terminal_service.py + test file | OK |
| TestCreateTerminalWorktree (6 tests) | box@cursor | uv run pytest ...::TestCreateTerminalWorktree | 6 passed |
| Full module suite | box@cursor | uv run pytest test/services/test_terminal_service_full.py | 79 passed, 4 skipped |
| Restore box checkout | box@cursor | git checkout -- (both files) | OK |

## Branch / commit

- Branch: `cao/0e48d5ec`
- Tip: see `git log -1` below
