# F416 Build Report — Fork (cli-agent-orchestrator)

**Branch:** `cao/48c0e6bd`  
**Tip:** `29bef148b4b9cdbc8a017f6e4573eecc34d72b27`  
**Issue:** #271 — Mechanize reviewer isolation via profile-level worktree default

## Summary

Added `default_use_worktree: Optional[bool] = None` to `AgentProfile`, wired
it through the assign/handoff path using the lifecycle-default precedent: when
the caller omits `use_worktree` (None), terminal_service resolves from the
profile's field. Explicit True/False from the caller always wins.

## File Anchors

| File | Line(s) | Change |
|------|---------|--------|
| `src/cli_agent_orchestrator/models/agent_profile.py` | L170-175 | New `default_use_worktree` field |
| `src/cli_agent_orchestrator/schemas/agent_profile.schema.json` | L200-205 | JSON schema entry |
| `src/cli_agent_orchestrator/services/terminal_service.py` | L1663-1668 | Resolution logic (parallel to lifecycle L1659) |
| `src/cli_agent_orchestrator/services/agent_step.py` | L341, L509 | Type change + `or False` coercion at fingerprint site (S1) |
| `src/cli_agent_orchestrator/api/main.py` | L558, L4028 | RunStepRequest + endpoint param type |
| `src/cli_agent_orchestrator/mcp_server/server.py` | L337,L1181,L1485,L1593,L1992 | MCP tool/impl type changes |

## Test Roster

| Test File | Count | Status |
|-----------|-------|--------|
| `test/services/test_f416_default_use_worktree.py` | 9 | ✅ PASS (NEW) |
| `test/mcp_server/test_assign.py` | 46 | ✅ PASS |
| `test/mcp_server/test_handoff.py` | 27 | ✅ PASS (1 new, 1 updated) |
| `test/api/test_terminals.py` | 121 | ✅ PASS (1 updated) |
| `test/api/test_run_step.py` | 29 | ✅ PASS (1 updated) |
| `test/services/test_terminal_service_coverage.py` | 64 | ✅ PASS |
| `test/services/test_worktree_service.py` | (included) | ✅ PASS |
| `test/services/test_step_fingerprint.py` | 42 | ✅ PASS |
| `test/services/test_terminal_service_full.py` | 10 (worktree) | ✅ PASS |
| `test/services/test_settlement_rewire.py` | 50 | ✅ PASS |
| `test/services/test_worker_terminal_cap.py` | 39 | ✅ PASS |
| `test/api/test_run_step_replay_branch.py` | (included) | ✅ PASS |
| `test/api/test_replay_nfr2_and_c1.py` | (included) | ✅ PASS |
| **Total (box)** | **542 passed, 4 skipped** | ✅ |

## Box Result

```
box-run: acquired box@cursor-3 for 'f416-suite'
======================= 542 passed, 4 skipped in 12.00s ========================
```

## Notes

- `codex_reviewer` profile referenced in task does not exist as a separate file
  (only `codex_empirical_reviewer` and `codex_design_reviewer` exist); skipped.
- Step fingerprint field stays `bool = False` (resolved value, not raw intent) —
  `body.use_worktree or False` coerces None at the fingerprint callsite.
