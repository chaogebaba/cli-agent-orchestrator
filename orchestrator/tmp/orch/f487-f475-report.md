# F487 + F475 Fix Report (r3)

**Branch:** `cao/f487-f475-noise`

## F487 (#342): park_warm watchdog suppression — UNCHANGED from r1

**Fix:** `merge_terminal_system_metadata(terminal_id, {"park_warm": True})` at
create_terminal; `record_inbound_task` checks `metadata.cao.park_warm is True`.

**Tests:** `test/services/test_f487_park_warm_watchdog.py` — 7 cases.

## F475 (#330): callback dedup — r3 rewrite (rolling window, normalized hash, choke-point)

**Mechanism:**

Enforcement lives inside `_insert_routed_inbox_row` — the single function
ALL inbox inserts pass through (direct-terminal, mailbox/`create_logical_inbox_message`,
barrier combined messages, watchdog auto-resume, digest notices). This covers
the `mb_` production path that previously bypassed the endpoint-level check.

**Dedup logic (one atomic query, no check-then-insert):**

After barrier association is resolved (so `barrier_id` is known):
1. If the row has `barrier_id != None` or `park_warm=True` or `dispatch_barrier != None`
   → assign `callback_dedup_key = NULL`, skip dedup (these rows never suppress anything).
2. If `_f475_should_dedup(sender, receiver, orch_type, ...)` → compute normalized hash:
   - Strip `[FROZEN-PIN-ATTESTATION ...]` blocks from message before hashing
   - `content_hash = sha256(normalized_message)`
3. Single atomic `SELECT` within the SAME transaction (SQLite serializes writers):
   `WHERE sender_id=? AND receiver_id=? AND callback_dedup_key=? AND created_at >= (now - 60s) AND park_warm IS NOT TRUE AND barrier_id IS NULL`
4. If existing row found → return it (suppressed). Otherwise → INSERT with
   `callback_dedup_key = content_hash`.

**Why this is race-free:** SQLite serializes all writers through a single write
lock. The `SELECT` and `INSERT` happen inside the same ORM session/transaction —
no window between check and insert for another writer to slip through.

**Rolling window:** The `created_at >= (now - 60s)` clause is evaluated at query
time, not bucketed. No boundary-straddling gap is possible.

**Normalized content:** The `_f475_normalize_message` function strips
`[FROZEN-PIN-ATTESTATION valid_at=... pins=N]` blocks before hashing, so
identical logical callbacks with differing attestation timestamps dedup correctly.

**Barrier isolation (B2 fix):** Key assignment happens AFTER
`_barrier_member_for_callback` resolves. If the row got `barrier_id != None`,
it gets `callback_dedup_key = NULL` and can never match a later ordinary
callback's dedup query (which filters `barrier_id IS NULL`).

**Tests:** `test/services/test_f475_callback_dedup.py` — 15 cases:
- `TestF475DistinctContent`: distinct content persists; identical deduped
- `TestF475BoundaryStraddling`: 0.2s apart across minute edge — deduped (rolling)
- `TestF475AttestationNormalization`: different attestation timestamps — deduped
- `TestF475MailboxPath`: mb_ addressed duplicate — deduped through choke point
- `TestF475BarrierParkWarmIsolation`: park_warm then ordinary (delivers);
  barrier-associated then identical ordinary (delivers)
- `TestF475Helpers`: normalize, content hash, should_dedup eligibility

## Box suite failures (infra-only, pre-existing on main)

- `test/clients/test_tmux_session_exists_strict.py::TestConfirmedAnswers::test_server_shut_down_under_us_is_a_confirmed_absence`
- `test/plugins/test_suite_slot.py::TestPidReuseGuard::test_stale_entry_not_killed`
- `test/plugins/test_suite_slot.py::TestLedgerSampling::test_sample_ledger_monotonic_growth`
- `test/services/test_worker_terminal_cap.py::TestConcurrentAdmission::test_thread_race_barrier_inside_listing_admits_exactly_one`

## Files changed

- `src/cli_agent_orchestrator/services/terminal_service.py` — persist park_warm
- `src/cli_agent_orchestrator/services/stalled_callback_watchdog.py` — F487 guard
- `src/cli_agent_orchestrator/clients/database.py` — F475 dedup in `_insert_routed_inbox_row`
- `src/cli_agent_orchestrator/api/main.py` — removed old endpoint-level dedup
- `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt` — regen
- `test/services/test_f487_park_warm_watchdog.py` — 7 tests
- `test/services/test_f475_callback_dedup.py` — 15 tests
