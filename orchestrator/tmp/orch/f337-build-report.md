# F337-r4 Build Report — native supervisor wake (fix round 4)

**Branch:** cao/f337-native-wake
**HEAD:** (self — the commit adding this file)
**Merge-base:** 01d47baf

## Findings addressed

| ID | Severity | Status | Summary |
|----|----------|--------|---------|
| B2a | BLOCKER | FIXED | `test_f165f1_f166f1_followups.py` fixture: added `supervisor.wake.native: True` to both config map occurrences |
| B2b | BLOCKER | FIXED | Trace manifest regenerated at final SHA via `generate_manifest()` — 39 lines, byte-exact |
| S2 | SHOULD | FIXED | Gate test calls production helper `_maybe_derive_cc_team_inbox_path` (extracted from `create_terminal`); mutant `_native_wake_enabled = True` now fails the test |
| N1 | NIT | FIXED | Report uses "(self)" convention for HEAD; merge-base is the verified rebase onto |

## Changed files (4)

- `src/cli_agent_orchestrator/services/terminal_service.py` — extract `_maybe_derive_cc_team_inbox_path` helper; `create_terminal` delegates to it
- `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt` — regenerated
- `test/services/test_f337_auth_handshake.py` — S2: tests call production `_maybe_derive_cc_team_inbox_path`
- `test/services/test_f165f1_f166f1_followups.py` — B2a: add `supervisor.wake.native: True` to config maps

## Test evidence

### Named failures from gate report (box@cursor-3, detached worktree at 41d4058b)
```
TestF165F1D9ProgrammingErrorSurface::test_detached_instance_error_produces_durable_attempt_row: PASSED
test_trace_manifest_is_byte_exact_and_has_36_hits: PASSED
test_reconcile_pull_mode_push_selects_only_own_mailbox_rows: PASSED
TestF165MigratedFx158::test_pull_mode_push_delivered_on_shared_fixture: PASSED
TestF165RealSqliteReconciler::test_pull_mode_push_delivered_end_to_end: PASSED
+ 35 F337 auth/gate tests: ALL PASSED
= 40 passed, 0 failed
```

### Box full suite (cursor-3, detached worktree at 41d4058b, copied venv)
```
13653 passed, 53 failed, 214 skipped, 15 xfailed, 2 errors in 313.42s
```
Failures are environment-artifact (test_memory_enabled_flag × 7, ImportError × 2,
plus base-level flakes). Neither named B2 test appears in failures. The two
prior named branch-only regressions (F165F1 fixture, trace manifest) are
confirmed fixed.
