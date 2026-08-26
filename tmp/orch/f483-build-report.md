# F483 Build Report — task_label for assign/handoff

## Summary
Implements #338: `assign` and `handoff` gain an optional `task_label` parameter.
The server writes `<terminal_id>\t<label>` rows to `/data/cao-scratch/fleet-labels.tsv`
at terminal creation and removes them on `delete_terminal` (cascade-aware).

## Changes

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/fleet_labels.py` | **NEW** — atomic TSV upsert/remove logic |
| `src/cli_agent_orchestrator/mcp_server/server.py` | `assign` + `handoff` gain `task_label` param; `delete_terminal` removes rows; `update_task_label` tool added |
| `src/cli_agent_orchestrator/services/agent_step.py` | `run_agent_step` gains `task_label` — write on create, remove in finally |
| `src/cli_agent_orchestrator/api/main.py` | `RunStepRequest` gains `task_label` field; threaded to `run_agent_step` |
| `test/services/test_fleet_labels.py` | **NEW** — 23 unit tests for fleet_labels module |
| `test/mcp_server/test_assign_task_label.py` | **NEW** — 6 integration tests for MCP tools |

## Design Decisions

1. **TSV format hard contract**: exactly `<terminal_id>\t<label>` — no timestamps, no profiles.
   Validated by `TestTsvFormatContract` which simulates fleet-tui.py's `read_labels()`.
2. **Fail-safe**: all TSV operations swallow exceptions — a missing/unwritable file never
   blocks an assign or delete.
3. **Atomic writes**: advisory flock + write-to-temp + `os.replace()`.
4. **Path configurable**: `CAO_FLEET_LABELS_PATH` env, default `/data/cao-scratch/fleet-labels.tsv`.
5. **Cascade-aware delete**: iterates the `reaped` list to remove labels for all reaped terminals.
6. **Minimal `update_task_label` tool**: standalone MCP tool for live updates; does not require
   discovery permissions (unlike `update_metadata`).
7. **Label sanitization**: tabs/newlines stripped, max 40 chars enforced.

## Test Results (box@cursor-3)

```
Full suite: 3813 passed, 148 skipped, 1 xfailed, 2 warnings in 128.54s
Pre-existing failure: test/plugins/test_suite_slot.py::TestPidReuseGuard::test_stale_entry_not_killed
  (unrelated F445 suite-slot watchdog test — race condition in subprocess.TimeoutExpired)

F483-specific tests: 29 passed (23 fleet_labels + 6 MCP integration)
```

### Gate R1 Fixes
- B1: Added `update_task_label` to S11 surface in `test/ux_surfaces.toml`
- B2: Regenerated trace manifest via `cao verify manifest --regen` (36→39 hits)
- Both blocker tests now pass locally.

## Branch
- Branch: `cao/f483-build`
- Base: `main` @ `ceae95da`
- HEAD: see git log
