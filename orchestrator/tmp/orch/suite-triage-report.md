# Suite Triage Report — RED full-suite run at c73a780a

**Date:** 2026-08-26  
**Fork commit (run):** c73a780a (feat(F489): fleet TUI auto-start on terminal dispatch)  
**Main tip (fix):** 01d47baf (Merge 'cao/f487-f475-fix' into main)  
**Box used (run):** cursor-3  
**Box used (verification):** cursor-3 (cleaned)  
**Total failures:** 16 (of 13649+16=13665 collected)

## Verdict

All 16 failures are **already fixed** at main tip 01d47baf. No new code changes required.
The fork commit c73a780a predates the F487/F475/F483/F488 merge wave that resolved
all underlying issues.

## Cause Table

| # | Test | Cause | Fixed by | Category |
|---|------|-------|----------|----------|
| 1-6 | test_f424_f426_inbox_mutation_kills.py (6× f136 tests) | F475/F476 API change: `_FakeMailbox` missing `cc_inbox_path`; `get_supervisor_callback_batch` renamed to `claim_unnotified_wake` | f1cff8d1 (F487/F475-r2) | Regression: test contract |
| 7-8 | test_fx168_hotfix.py::TestFix2StalePathSelfHeal (2 tests) | Same F476 API: FakeMailbox missing `cc_inbox_path`, mock targets renamed | 7787ed34 (F487/F475-r3) | Regression: test contract |
| 9-10 | test_f77_lifecycle_pointers.py::TestFAM3TerminalizeAwaitingMembers (2 tests) | F475 dedup `_insert_routed_inbox_row` corrupted shared SQLite :memory: session — identity map collision under xdist | fb7ed728 (F475 barrier-sender exclusion) + f1cff8d1 (savepoint isolation) | Regression: source |
| 11 | test_cleanup_service.py::TestCleanupOldData::test_barrier_member_message_id_is_explicitly_nulled_with_foreign_keys_off | Same savepoint/identity-map corruption as #9-10 | f1cff8d1 (savepoint dedup isolation) | Regression: source |
| 12 | test_stage0_flip_machinery.py::test_trace_manifest_is_byte_exact_and_has_36_hits | **KNOWN F492 #347**: stale trace manifest after line shifts from pending merges | b13eef6d (F483 gate-r1: regen trace manifest) | Known: stale manifest |
| 13 | test_tmux_session_exists_strict.py::TestConfirmedAnswers::test_server_shut_down_under_us_is_a_confirmed_absence | xdist parallel race on box — test touches global tmux state | f1cff8d1 (added `@pytest.mark.serial_only`) | Box-env: xdist flake |
| 14 | test_suite_slot.py::TestPidReuseGuard::test_stale_entry_not_killed | xdist parallel race — signal test unsafe under concurrency | f1cff8d1 (added `@pytest.mark.serial_only`) | Box-env: xdist flake |
| 15 | test_suite_slot.py::TestLedgerSampling::test_sample_records_child_process | xdist parallel race — child-process timing sensitive | f1cff8d1 (savepoint source fix eliminated contention) | Box-env: xdist flake |
| 16 | test_worker_terminal_cap.py::TestConcurrentAdmission::test_thread_race_barrier_inside_listing_admits_exactly_one | xdist parallel race — thread contention with shared :memory: session | fb7ed728 + f1cff8d1 (savepoint isolation eliminated shared-session corruption) | Box-env: xdist flake |

## Summary by Cause

| Category | Count | Fix commit(s) |
|----------|-------|---------------|
| Regression: test contract (F476 API) | 8 | f1cff8d1, 7787ed34 |
| Regression: source (savepoint isolation) | 3 | fb7ed728, f1cff8d1 |
| Known: stale manifest (F492 #347) | 1 | b13eef6d |
| Box-env: xdist flake | 4 | f1cff8d1 (serial_only + source fix) |

## Verification

**Local (laptop, 01d47baf):** 182/182 passed — all 16 tests green  
**Box cursor-3 (01d47baf, clean):** 13716 passed, 204 skipped, 15 xfailed, 1 unrelated flake  
- The 1 residual flake (`test_sample_ledger_monotonic_growth`) is NOT in the original 16; it's a known-flaky area (TestLedgerSampling timing sensitivity under xdist).
- 28 `test_f337_auth_handshake.py` failures in first box run were from an **untracked leftover file** from a prior F337 branch build; removed by `git clean -fd`.

## Diffs

No new code changes required. All fixes are already merged in 01d47baf via:
- Merge 'cao/f487-f475-fix' into main (F244 gated) — 01d47baf
- fix(F487/F475-r2): savepoint dedup isolation — f1cff8d1
- fix(F487/F475-r3): in-SAVEPOINT failure regression, fx168 F476 contract — 7787ed34
- fix(F475): regression — exclude barrier: senders from dedup — fb7ed728
- F483 gate-r1 fix: regen trace manifest — b13eef6d
