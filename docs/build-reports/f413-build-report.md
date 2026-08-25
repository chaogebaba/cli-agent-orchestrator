# F413 Build Report — ORM Listeners: obligation + sentinel + doorbell structurally unbypassable

## Summary

Implemented the F413 blueprint (r4, DESIGN + EMPIRICAL gates passed) exactly as specified.
All obligations, sentinel touches, and doorbell emissions are now structurally attached to
InboxModel inserts via SQLAlchemy mapper/session listeners, making bypass impossible.

## Changed Files

| File | Lines | Change |
|------|-------|--------|
| `src/cli_agent_orchestrator/clients/database.py:566-740` | D1/D2/D3/D6/D7b | Listener functions + registration |
| `src/cli_agent_orchestrator/clients/database.py:1270-1290` | D6 | SessionLocal listener registration |
| `src/cli_agent_orchestrator/clients/database.py:6148-6152` | D4 | Removed hand-placed calls from `_insert_routed_inbox_row` |
| `src/cli_agent_orchestrator/clients/database.py:4485-4505` | D7b | Reap per-row + helper call |
| `src/cli_agent_orchestrator/clients/database.py:4515-4537` | D7b | Reap bulk (owned_barriers) + helper call |
| `src/cli_agent_orchestrator/clients/database.py:6347-6379` | D7b | Cancel-barrier bulk + helper call |
| `src/cli_agent_orchestrator/services/mailbox_service.py:646-647` | D4 | Removed WS doorbell + request_delivery |
| `src/cli_agent_orchestrator/services/delivery_service.py:216-224` | compat | Made create_obligation idempotent |
| `test/services/test_f413_orm_listeners.py` | all | 14 new AC tests |
| `test/services/test_wp_mailbox_channel.py:290-293` | break | Updated settled_count expectation |
| `src/.../kernel/receiver_state/trace_manifest.txt` | regen | Pre-existing stale manifest |

## Test Roster — AC → Test Mapping

| AC | Test | Assertion |
|----|------|-----------|
| AC1 | `TestAC1RawAdd::test_raw_add_yields_obligation_trace_sentinel_doorbell` | Obligation + trace + sentinel + doorbell from raw db.add |
| AC2 | `TestAC2Rollback::test_rollback_clears_obligation_and_doorbell` | No obligation after rollback, no doorbell |
| AC3 | `TestAC3SingleObligation::test_create_inbox_message_single_obligation` | Exactly 1 obligation per row |
| AC3 | `TestAC3SingleObligation::test_no_obligation_call_sites_remain` | grep proves no hand-placed calls |
| AC4 | `TestAC4BarrierHeld::test_held_row_no_obligation` | HELD row → no obligation |
| AC4b | `TestAC4bBarrierCancel::test_cancel_creates_obligations_for_qualifying_rows` | Bulk HELD→PENDING + D7b helper |
| AC4c | `TestAC4cTerminalReap::test_reap_per_row_creates_obligations` | Reap flip + D7b helper |
| AC5 | `TestAC5NonSupervisor::test_non_supervisor_no_obligation` | No obligation/sentinel/doorbell |
| AC6 | Box regression suite (291 passed) | Full inbox/delivery/barrier/F192/FX191/F404/F136/F354 |
| D3 | `TestD3NestedTxGuard::test_nested_commit_does_not_emit_doorbell` | Nested-tx guard |
| D3 | `TestD3NestedTxGuard::test_nested_rollback_preserves_earlier_stash` | Snapshot-restore |
| - | `TestPredicateUnit` (4 tests) | _f413_row_qualifies predicate |

## Box Result

```
box@cursor-3 at SHA 39bd7117
291 passed in 12.13s
```

Ignored pre-existing failures (verified identical on main):
- test_fx158_push_instrumentation.py (mock brittleness)
- test_fx158_pull_reconciler.py (mock brittleness)
- test_terminal_service_inbox_registration.py (unrelated context build)

## Mutation Ledger

Location: `orchestrator/tmp/orch/mutations/`

| ID | Target | Diff | Kill test | Exit | Excerpt |
|----|--------|------|-----------|------|---------|
| M1 | `_f413_row_qualifies` predicate (line 577): `==` → `!=` | m1_predicate.diff | TestAC1, TestPredicateUnit | 1 | 9 failed, 5 passed |
| M2 | Nested-tx guard (line 646): `in_nested_transaction()` → `False` | m2_nested_guard.diff | TestD3NestedTxGuard::nested_commit | 1 | 1 failed, 1 passed |
| M3 | D7b cancel-barrier call (line 6379): disabled | m3_d7b_cancel.diff | TestAC4bBarrierCancel | 1 | 1 failed in 0.66s |

Post-restore hashes verified: m1_post_restore.md5, m2_post_restore.md5, m3_post_restore.md5

## Decision Compliance

All 7 decision rows (D1-D7b) implemented exactly per blueprint r4. No deviations.
D5 (startup reconciliation) untouched per spec.
