# F487+F475 Regression Fix Report

**Branch:** `cao/f487-f475-fix`  
**Base:** `8c994302` (main after F487/F475 + F461 merges)

## Root cause

The F475 dedup query inside `_insert_routed_inbox_row` used the CALLER's `db`
session for the duplicate-detection SELECT. When barrier-fire code called
`_insert_routed_inbox_row` with sender `"barrier:N"`:
1. The outer sender-prefix guard didn't exclude `"barrier:"` senders (only
   `"watchdog:"` and `"cao-"` were listed)
2. Even after the guard excluded internal senders, the dedup SELECT on the
   caller's session could corrupt the transaction state on error (datetime
   comparison issues in test fixtures with naive vs aware timestamps)

This caused `_stamp_enqueue_generation` to fail with
`pending_receiver_generation_unavailable` when it tried to query the same
session that was in a broken state.

## Fix (2 changes in `clients/database.py`)

1. **Added `"barrier:"` to the sender-prefix exclusion list** in both the outer
   F475 guard in `_insert_routed_inbox_row` and in `_f475_should_dedup`. Internal
   senders (barrier-fire combined messages, watchdog auto-resume, digest notices)
   are NEVER real worker callbacks and must never enter the dedup path.

2. **Moved the dedup SELECT to a SEPARATE session** (`SessionLocal()`) so any
   query failure cannot corrupt the caller's transaction. The existing row's `id`
   is captured, then re-fetched from the caller's `db` session for return.

## Tests verified

- `test/clients/test_f77_lifecycle_pointers.py::TestFAM3TerminalizeAwaitingMembers` (4 tests) — PASS
- `test/services/test_cleanup_service.py::TestCleanupOldData::test_barrier_member_message_id_is_explicitly_nulled_with_foreign_keys_off` — PASS
- `test/plugins/test_rss_guard.py::test_rss_guard_trips_on_spike` — PASS (timing flake, not a regression)
- `test/services/test_f475_callback_dedup.py` (15 tests) — PASS
- `test/services/test_f487_park_warm_watchdog.py` (7 tests) — PASS

## Dedup guarantees preserved

The dedup still works correctly:
- Distinct content from same sender both persist
- Identical byte-content within 60s rolling window is suppressed
- Boundary-straddling duplicates are caught (rolling window, no bucket)
- Different attestation blocks with same logical content are deduped
- Barrier-associated and park_warm rows never suppress ordinary callbacks
