# F337-r3 Build Report — native supervisor wake (fix round 3)

**Branch:** cao/f337-native-wake
**HEAD:** bbeebfae
**Merge-base:** 8c994302

## Findings addressed

| ID | Severity | Status | Summary |
|----|----------|--------|---------|
| B1 | BLOCKER | FIXED | Ambiguity: `read_peer_token` collects ALL valid candidates, requires exactly one; multiple valid keys → None before `write_to_socket` |
| B2 | BLOCKER | FIXED | 3 branch-only test failures: added `"supervisor.wake.native": True` to config maps in reconciler/pull-mode tests that require native push |
| S1 | SHOULD | FIXED | `base.iterdir()` wrapped in `try/except OSError` — unreadable directory returns None cleanly |
| S2 | SHOULD | FIXED | Gate test exercises production code path via `ConfigService.get` monkeypatch; a mutant `_native_wake_enabled = True` would cause `mock_derive` assertion failure |
| N1 | NIT | FIXED | Report HEAD is the actual pinned commit SHA |

## Changed files

- `src/cli_agent_orchestrator/services/cc_session_registry.py` — collect-all-then-select-one logic, iterdir guard
- `test/services/test_f337_auth_handshake.py` — S2 production gate test, B1 ambiguity + S1 iterdir regressions
- `test/services/test_f424_f426_inbox_mutation_kills.py` — B2: add `supervisor.wake.native: True` to config
- `test/services/test_f165_f166_real_sqlite_daemons.py` — B2: add `supervisor.wake.native: True` to config
- `test/services/test_f165_real_sqlite_reconciler.py` — B2: add `supervisor.wake.native: True` to config

## Regression tests added (r3)

- `TestF337R3Ambiguity` (4 tests): two-valid-keys, identical-token, one-valid-one-mismatch, single-key
- `TestF337R3IterdirError` (2 tests): OSError, PermissionError on iterdir

## Test evidence

### Local targeted (F337 + doorbell + delivery + reconciler + fx168)
```
230 passed, 6 failed (pre-existing _FakeMailbox.cc_inbox_path)
```

### 3 named previously-failing tests
```
test_reconcile_pull_mode_push_selects_only_own_mailbox_rows: PASSED
TestF165MigratedFx158::test_pull_mode_push_delivered_on_shared_fixture: PASSED
TestF165RealSqliteReconciler::test_pull_mode_push_delivered_end_to_end: PASSED
```

### Box (cursor-3, full suite)
```
13667 passed, 20 failed (pre-existing), 204 skipped, 15 xfailed in 330.64s
```

### Box (cursor-3, 3 named + F337 targeted)
```
38 passed in 4.38s
```

All 3 named B2 tests pass. Zero F337-related failures.
