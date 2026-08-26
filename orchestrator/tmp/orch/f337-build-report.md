# F337-r2 Build Report — native supervisor wake (fix round)

**Branch:** cao/f337-native-wake
**HEAD:** cd3be940
**Merge-base:** 8c994302

## Findings addressed

| ID | Severity | Status | Summary |
|----|----------|--------|---------|
| B1 | BLOCKER | FIXED | Default-dark: `WAKE_NATIVE_DEFAULT=False` canonical constant in `cc_session_registry`, config registry default `False`, all 5 call sites import and use the constant |
| B2 | BLOCKER | FIXED | procStart binding: `read_peer_token` gains `expected_proc_start` param, verifies key file `procStart` matches live process incarnation, strict 64-hex filename pattern |
| S1 | SHOULD | FIXED | Non-object JSON: `isinstance(data, dict)` check before `.get()` — array/string/number/null return `None` cleanly |
| S2 | SHOULD | FIXED | Gate test: replaced structural assertion with behavioral test (derive not called when native=False, called when True) |

## Changed files (8)

- `src/cli_agent_orchestrator/services/cc_session_registry.py` — add `WAKE_NATIVE_DEFAULT`, rewrite `read_peer_token`
- `src/cli_agent_orchestrator/services/config_service.py` — registry default → False
- `src/cli_agent_orchestrator/services/delivery_service.py` — import + use `WAKE_NATIVE_DEFAULT`
- `src/cli_agent_orchestrator/services/doorbell_service.py` — import + use `WAKE_NATIVE_DEFAULT`, pass `expected_proc_start`
- `src/cli_agent_orchestrator/services/inbox_service.py` — import + use `WAKE_NATIVE_DEFAULT` (both call sites)
- `src/cli_agent_orchestrator/services/terminal_service.py` — import + use `WAKE_NATIVE_DEFAULT`
- `test/services/test_f337_auth_handshake.py` — behavioral gate test (S2), regression tests (B1, B2, S1)
- `test/services/test_delivery_service.py` — adapt `attempt_rung1` tests to explicitly enable native wake

## Regression tests added

- `TestF337R2DefaultDark` (4 tests): canonical constant, config registry, doorbell gate, delivery gate
- `TestF337R2ProcStartBinding` (5 tests): mismatch, match, no-check, unparseable, strict-filename
- `TestF337R2MalformedKeyJSON` (5 tests): array, string, number, null, empty

## Test evidence

### Local (laptop)
```
uv run pytest test/services/test_f337_auth_handshake.py \
  test/services/test_fx170_native_doorbell.py \
  test/services/test_f216_null_socket_path.py \
  test/services/test_f457_wake_gate_dedupe.py \
  test/services/test_f461_doorbell_coalesce.py \
  test/services/test_f476_single_wake_cursor.py \
  test/services/test_fx168_doorbell.py

180 passed in 16.79s
```

### Box (cursor-4, full suite)
```
13658 passed, 23 failed, 204 skipped, 15 xfailed in 345.52s
```

All 23 failures are pre-existing (test_f77, test_tmux_session_exists_strict,
test_suite_slot, test_cleanup_service, test_f424_f426, test_fx168_hotfix,
test_stage0_flip_machinery, test_f186_reconciler, test_fx168_doorbell,
test_f165 — none F337-related).

### Box (cursor-4, targeted F337 + delivery + wake tests)
```
109 passed in 6.92s
```

Zero F337-related failures on the box.
