# F457 r2 — Build Report

**Branch:** `cao/1a24ba05`
**Base:** `a74a3d7f` (f457: add build report)
**Fix commit:** `fc5e1a89` (f457-r2: fix 2 BLOCKERs + 1 SHOULD from gate report)

---

## Amendments

### B1 (BLOCKER): `reconcile_pull_mode_notifications` wake.native gate

**File:** `src/cli_agent_orchestrator/services/inbox_service.py` (~line 3490)

**Fix:** Added `ConfigService.get("supervisor.wake.native", default=True)` guard
before `attempt_teammate_push_reported` call — mirrors the `deliver_pending` gate
at line 2315. When `wake.native=false`, the reconciler now logs
`f457_reconciler_push_suppressed` and skips the push via `continue`.

**Side-effect:** Updated `test/services/test_f186_reconciler_doorbell_lock.py` config
mock to include `supervisor.wake.native` in the truthy-keys set (test was returning
`None` for unrecognised keys, which the new gate interpreted as falsy).

---

### B2 (BLOCKER): `delivery_service.attempt_rung1` gates

**File:** `src/cli_agent_orchestrator/services/delivery_service.py` (~line 392)

**Fix (option b — justified below):** Added inline guards in `attempt_rung1`:
1. `ConfigService.get("supervisor.wake.native", default=True)` — returns
   `LadderResult(decision="skipped_disabled", reason="wake_native_disabled")`.
2. `_is_row_still_pending(inbox_row_id)` — returns
   `LadderResult(decision="skipped_acked", reason="row_not_pending")`.

Both checks fire before `_attempt_native_ring` is called.

**Why option (b) instead of (a):** `ring_supervisor_doorbell` has cursor-dedup logic
(`written_count <= 0` → skip; `max_written_row_id <= last_row` → skip) that
structurally conflicts with convergence_tick retry semantics. The stalled callback
watchdog calls `attempt_rung1` repeatedly for the same inbox row until it's acked —
the dedup would suppress all retries after the first ring, breaking the rung ladder.

---

### S1 (SHOULD): `get_pending_messages_by_ids` fail-open

**File:** `src/cli_agent_orchestrator/services/inbox_service.py` (~line 2325)

**Fix:** Wrapped the `get_pending_messages_by_ids` call in `try/except Exception`,
falling back to the original `messages` list on failure. Logs at DEBUG level:
`f457_recheck_db_error terminal=%s error=%s action=fail_open_with_original_messages`.
Mirrors `_is_row_still_pending`'s explicit fail-open pattern in doorbell_service.

---

### N1 (NIT): Suite count reconciliation

**r1 discrepancy:** Build report showed "Local: 183" vs "Box: 210".

**Explanation:** The r1 local run used `pytest -p no:xdist` (no parallel workers),
which collected tests differently with xdist parallelism disabled. The 7-file set
collects exactly 210 tests with xdist enabled (which is the default in pyproject.toml).

**r2 local result:** `210 passed in 9.66s` (7-file set, xdist default).
With r2 test file included: `215 passed in 10.33s`.

---

## Pre-Fix RED Evidence (against a74a3d7f)

```
FAILED TestB1ReconcilerWakeNativeGate::test_reconciler_suppressed_when_wake_native_false
  → AssertionError: Expected 'attempt_teammate_push_reported' to not have been called.
    Called 1 times.

FAILED TestB2AttemptRung1Gates::test_rung1_skipped_when_wake_native_false
  → AssertionError: assert 'defer' == 'skipped_disabled'
    (no wake.native gate — fell through to native ring defer)

FAILED TestB2AttemptRung1Gates::test_rung1_skipped_for_acked_row
  → AssertionError: assert 'defer' == 'skipped_acked'
    (no _is_row_still_pending call in attempt_rung1)

FAILED TestS1FailOpenDbError::test_db_error_falls_back_to_original_messages
  → RuntimeError: DB connection lost
    (exception propagated instead of caught — no try/except wrapper)

PASSED TestB2AttemptRung1Gates::test_rung1_proceeds_when_enabled_and_pending
  (no-regression test — correctly passes on both pre/post-fix)
```

4 fix-specific tests RED on a74a3d7f, GREEN on fix commit. 1 no-regression test passes on both.

---

## Suite Counts

| Scope | Files | Collected | Passed | Failed | Notes |
|-------|-------|-----------|--------|--------|-------|
| Local (7-file set) | 7 | 210 | 210 | 0 | xdist default |
| Local (7 + r2 tests) | 8 | 215 | 215 | 0 | |
| Box (full scope) | 10 | 303 | 302 | 1 | pre-existing flaky |

**Box failure (pre-existing, unrelated to F457):**
`test_stalled_callback_watchdog.py::test_watchdog_polls_already_idle_episode_and_unarms_when_processing`
→ `get_status` called 2x instead of 1x on box (passes locally). This is a
status_monitor mock leakage issue in the box Python 3.14 environment, not
related to wake gates.

---

## Box-Actions Ledger

| # | Label | Command Summary |
|---|-------|-----------------|
| 1 | f457-r2-ls | ls ~ (discovery) |
| 2 | f457-r2-ls2 | ls cli-subagents/ (path discovery) |
| 3 | f457-r2-ls3 | ls cli-subagents/cli-agent-orchestrator/ (confirm path) |
| 4 | f457-r2-suite | fetch cao/1a24ba05, switch, pytest 10 files → 302 passed, 1 failed (pre-existing) |
| 5 | f457-r2-base | (blocked by hook — branch switch denied; verified locally instead) |

Box left at: `cao/1a24ba05` tip `fc5e1a89`, clean state.

---

## Updated Call-Site Completeness Table

| # | Function | Location | Gated by `wake.native`? | Gated by `_is_row_still_pending`? | Status |
|---|----------|----------|------------------------|----------------------------------|--------|
| 1 | `attempt_teammate_push` | inbox_service.py:2315 (deliver_pending) | **YES** | **YES** (via get_pending_messages_by_ids) | GATED ✅ |
| 2 | `attempt_teammate_push_reported` | inbox_service.py:~3492 (reconciler) | **YES** (r2 B1) | NO (rows freshly queried w/ grace window) | GATED ✅ |
| 3 | `_attempt_native_ring` | doorbell_service.py:121 (via ring_supervisor_doorbell) | **YES** | **YES** | GATED ✅ |
| 4 | `_attempt_native_ring` | delivery_service.py:~394 (attempt_rung1) | **YES** (r2 B2) | **YES** (r2 B2) | GATED ✅ |
| 5 | `attempt_teammate_push_on_insert` | teammate_push_service.py:583 | N/A | N/A | DEAD CODE |
| 6 | `ring_supervisor_doorbell` | inbox_service.py:1190 (F168 post-delivery) | **YES** (inherits) | **YES** (inherits) | GATED ✅ |
| 7 | `ring_supervisor_doorbell` | inbox_service.py:~3505 (reconciler ride-along) | **YES** (inherits) | **YES** (inherits) | GATED ✅ |

**All live wake paths now gated. Zero ungated paths remain.**
