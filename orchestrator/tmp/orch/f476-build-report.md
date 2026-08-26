# F476 Build Report — Single Wake Cursor

Branch: `cao/f476-build` (head: 088600d6)
Base: `main` (a70bd41a)

## What shipped per decision

| # | Decision | Shipped |
|---|----------|---------|
| D1 | One wake cursor, one exclusive claimant | `claim_unnotified_wake` acquires `get_mailbox_authority_lock` with 0.5s timeout; `authority_lock_contention` returned on failure |
| D2 | Wake ≠ consume | `commit_wake` checks `superseded_by_ack` (through_id <= consumed_through_id); ack_messages and commit_wake share the same authority lock |
| D3 | Claim-then-commit pair | `claim_unnotified_wake` + `commit_wake` in database.py; HTTP: `POST /messages/wake-claim`, `POST /messages/wake-commit`, `POST /messages/wake-drain-replay`; GET /messages gains `unconsumed_only` param |
| D4 | Lost-wake recovery server-side | 3 new columns: `wake_notified_at`, `wake_streak`, `wake_notified_id`; migration `_migrate_f476_wake_recovery`; lease visibility predicate (300s cooldown, streak cap 3); stamp at claim, clear at commit |
| D5 | Legacy cursors retired | `get/set_terminal_last_notified_inbox_id` and `last_doorbell_row_id` are no-op stubs; functional behavior removed; columns kept |
| D6 | Wake-role hierarchy | Teammate push remains primary (via F136 callback runner); HTTP endpoints ready for f213 fallback hook; SessionStart prime via `GET /messages?unconsumed_only=true` + `POST /messages/wake-drain-replay` |
| D7 | F157 text-scrub untouched | Confirmed |
| D8 | Doorbell = transport of path 2 | Cursor dedup removed from `ring_supervisor_doorbell`; F457 still-pending check retained |
| D9 | Server-unreachable behavior | Client-side (hook) — HTTP endpoints ready; server returns structured errors |
| D10 | Kind-agnostic | `claim_unnotified_wake` query uses `status='pending'` only, no `orchestration_type` filter |

## Test evidence

- **17 F476-specific tests**: All pass (AC2, AC3, AC4, AC5, AC6, AC9, AC10, AC11, AC12, AC14, AC15, D10)
- **Box suite** (230 tests across affected files): 230 passed, 0 failed, 1 xfailed
- Files tested on box: test_f476_single_wake_cursor, test_f175_push_storm_dedup, test_fx170_native_doorbell, test_fx168_doorbell, test_teammate_push_bridge, test_fx158_pull_reconciler, test_f165_fixture_isolation, test_f136_callback_delivery

## Mutation ledger

| Mutant | Killed by |
|--------|-----------|
| claim returns empty instead of rows | AC2, AC9, AC10, D10 tests all assert `len(result.rows) >= 1` |
| commit advances cursor without authority check | AC4 (lock contention → no advance) |
| Streak never resets | AC3 `test_streak_resets_on_new_forward` |
| Lease check disabled | AC14 `test_lease_blocks_second_claimer` (lease_held) |
| superseded_by_ack removed | AC11 asserts `kind == "superseded_by_ack"` |
| path_changed removed | AC15 asserts `kind == "path_changed"` |
| wake_exhausted never returned | AC12 `test_exhaustion_blocks_claim` |

## AC status

| AC | Status | Notes |
|----|--------|-------|
| AC1 | Partial | Server-side claim/commit tested; full integration (CC inbox write + doorbell counting) requires hook wiring |
| AC1b | Partial | `unconsumed_only` + `wake-drain-replay` endpoints ready; needs hook integration |
| AC2 | ✅ | Tested |
| AC3 | ✅ | Tested via sim clock |
| AC4 | ✅ | Tested |
| AC5 | ✅ | Server-side claim available regardless of flag |
| AC6 | ⚠️ | Functionally satisfied (stubs are no-ops); grep count is 7 not 3 due to retained stubs for test compat |
| AC7 | N/A | Hook-side (root repo) |
| AC8 | ✅ | 230 tests pass on box |
| AC9 | ✅ | Tested |
| AC10 | ✅ | Tested (replay below cursor + survive between claim/commit) |
| AC10b | Partial | Endpoints ready; full integration needs hook |
| AC11 | ✅ | Tested |
| AC12 | ✅ | Tested |
| AC13 | N/A | Hook-side (root repo) |
| AC14 | ✅ | Tested |
| AC15 | ✅ | Tested |

## Commits

1. `ffa02c59` — D1-D5/D8: core claim/commit pair + legacy removal
2. `8d0d4085` — Integration tests + conftest fix
3. `91c21d96` — Stubs for legacy test compat
4. `002c2a27` — Fix doorbell tests for D8
5. `088600d6` — Fix remaining broken tests
