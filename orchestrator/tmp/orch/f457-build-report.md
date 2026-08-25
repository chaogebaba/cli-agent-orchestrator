# F457 Build Report — Unified Socket-Wake Gates + Acked-Row Dedupe

## Summary

Unified the two independent socket-wake paths under one config gate
(`supervisor.wake.native`) and added send-time dedupe against already-acked rows.

## Changes (4 files, +241 −1)

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/doorbell_service.py` | +33: added `_is_row_still_pending()` function + F457 acked-row check before native ring |
| `src/cli_agent_orchestrator/services/inbox_service.py` | +25/−1: added `wake.native` gate + `get_pending_messages_by_ids` dedupe before teammate push |
| `test/services/test_f457_wake_gate_dedupe.py` | +175: new test file with 4 unit tests |
| `test/services/test_fx168_hotfix.py` | +8: updated existing test to mock F457 gates |

## Pre-Fix RED Evidence

Tests run against the unfixed codebase (before the fix commit):

```
FAILED test_f457...::TestAC1WakeNativeFalseSuppressesPush::test_wake_native_false_suppresses_teammate_push
  → AssertionError: Expected 'attempt_teammate_push' to not have been called. Called 1 times.

FAILED test_f457...::TestAC2AckedRowNoWake::test_acked_row_no_teammate_push
  → AssertionError: Expected 'attempt_teammate_push' to not have been called. Called 1 times.

FAILED test_f457...::TestAC2AckedRowNoWake::test_acked_row_no_doorbell_native_ring
  → AttributeError: ...doorbell_service does not have the attribute '_is_row_still_pending'

PASSED test_f457...::TestAC3WakeNativeTruePendingStillFires::test_wake_native_true_pending_row_push_fires
```

3 failed, 1 passed — confirms (a) and (b) are RED pre-fix, (c) is GREEN (no regression).

## Post-Fix Suite Results

### Local (worktree)
- 183 passed (doorbell + inbox + f457 + f136 + f186 + terminal_service) in 8.79s
- 4/4 F457-specific tests GREEN

### Box (box@cursor-3)
- 210 passed in 7.24s
- Test files: test_fx168_doorbell, test_fx168_hotfix, test_fx170_native_doorbell,
  test_f457_wake_gate_dedupe, test_f136_callback_delivery, test_f186_reconciler_doorbell_lock,
  test_terminal_service

## Box-Actions Ledger (box@cursor-3)

| # | Type | Command |
|---|------|---------|
| 1 | box-run.sh | `f457-suite` — git fetch + checkout cao/069ee684 + pytest 7 test files |

- Checkout SHA left on box: `bcc6d32ccf8de0b07a9a4056d95027558be22b2f` (branch: cao/069ee684)
- Dirty state: clean
- Environment mutations: none (uv resolved from existing lockfile)
- Temp files: `/tmp/f457-suite-run.txt` (tee output)
- Deviations: box cleanup (checkout main + branch delete) blocked by worktree hook — branch left on box

## Design Decisions

1. **Unified gate location**: Added `ConfigService.get("supervisor.wake.native")` check
   inline in inbox_service's mailbox-pull branch, BEFORE `_should_teammate_push`. This
   means `wake.native=false` kills the teammate push even if `teammate_push=true`.

2. **Dedupe strategy**: inbox_service re-queries `get_pending_messages_by_ids` (already
   existed in DB layer) before pushing. Doorbell uses a new `_is_row_still_pending` that
   does a lightweight single-row status check. Both fail-open on DB errors.

3. **Not touched** (per spec): durable inbox hold, pane-nudge fallback path, f213 watcher
   contract, message content formats.
