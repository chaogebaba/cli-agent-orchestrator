# F476 Build Report — Single Wake Cursor (post-gate-fix)

Branch: `cao/f476-build` (head: 859b2c7b)
Base: `main` (a70bd41a)

## Gate-fix tally

| Blocker | Fix | Commit |
|---------|-----|--------|
| B1 | F136 push runner rewritten onto claim→commit→emit (single choke point) | 77f576fd |
| B2 | commit_wake requires `claimed_high_water`; rejects `through_id > claimed_high_water`; verifies lease binding | 6795e1ae |
| B3 | claim_unnotified_wake selects committed-pending rows (id > consumed AND id <= notified) | 6795e1ae |
| B4 | Replay rows no longer bypass the live lease (lease_held blocks ALL rows uniformly) | 6795e1ae |
| B5 | Exhaustion skips when newer forward rows exist; logs WARNING; fleet alarm via `get_wake_exhaustion_alarms()` | 6795e1ae |
| B6 | All stubs removed; AC6 grep: exactly 3/3; 15 test files rewritten | cb0712ee, 859b2c7b |
| B7 | `POST /messages/wake-drain-replay` validates current_terminal_id + acquires authority lock | 6795e1ae |

## Box suite verdict

```
310 passed, 4 xfailed in 15.02s
```

Tested files: test_f476, test_f175, test_fx170, test_fx168, test_f136_callback_delivery,
test_teammate_push_bridge, test_fx158_pull_reconciler, test_f165_fixture_isolation,
test_fx157_push_recount, test_f186_reconciler_doorbell_lock, test_f459_native_callback,
test_f457_wake_gate_dedupe, test_wp_mailbox_channel, test_fx158_push_instrumentation,
test_f136_mutation_kills, test_f123_supervisor_sentinel, test_f165_real_sqlite_reconciler.

## Decision wall compliance

| # | Decision | Status |
|---|----------|--------|
| D1 | One cursor, exclusive claimant | ✅ Authority lock 0.5s; push runner uses claim pair |
| D2 | Wake ≠ consume | ✅ superseded_by_ack in commit; serialized via authority lock |
| D3 | Claim-then-commit choke point | ✅ Push runner: claim→commit→emit; HTTP endpoints for hooks |
| D4 | Lost-wake recovery server-side | ✅ lease stamp, 300s cooldown, streak cap 3, B3 recovery |
| D5 | Legacy cursors retired | ✅ Functions deleted; AC6 = 3/3 exact |
| D6 | Wake hierarchy | ✅ Push = primary (via claim pair); endpoints for f213 fallback |
| D7 | F157 untouched | ✅ |
| D8 | Doorbell = transport | ✅ No cursor dedup; F457 still-pending retained |
| D9 | Server-unreachable | ✅ Endpoints return structured errors; hook-side is root repo |
| D10 | Kind-agnostic | ✅ No orchestration_type filter in claim query |

## AC status

| AC | Status |
|----|--------|
| AC1 | ✅ Push runner uses claim→commit→emit; doorbell counts as transport |
| AC2 | ✅ Tested |
| AC3 | ✅ B3 fix: committed-pending recovery returns rows after commit |
| AC4 | ✅ Tested (authority lock contention) |
| AC5 | ✅ Server-side claim available; flag hierarchy is hook-side |
| AC6 | ✅ Exactly 3/3 grep matches |
| AC7 | N/A (hook-side, root repo) |
| AC8 | ✅ 310 passed on box |
| AC9 | ✅ Tested |
| AC10 | ✅ Tested (replay below cursor; survives between claim/commit) |
| AC11 | ✅ Tested (superseded_by_ack) |
| AC12 | ✅ B5 fix: exhaustion doesn't block newer work; WARNING logged |
| AC13 | N/A (hook-side, root repo) |
| AC14 | ✅ B4 fix: replay respects lease; B2: commit bound to claim |
| AC15 | ✅ Tested (path_changed) |

## Commits (post-gate)

1. `6795e1ae` — B2-B5/B7: bound commit, recovery, lease, exhaustion, drain auth
2. `77f576fd` — B1: push runner rewrite onto claim/commit pair
3. `cb0712ee` — B6: AC6 exact legacy cursor retirement (3/3 grep counts)
4. `859b2c7b` — B6 followup: remaining test breakage fixes
