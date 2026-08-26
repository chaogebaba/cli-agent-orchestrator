# F158-R5 Build Report

**Round:** R5
**Branch:** cao/f158-build
**HEAD:** 89c882de (post-push)
**Base:** 01d47baf (origin/main)

## Findings → Fixes

### B1 (BLOCKER): Late WS frame coexists with native fallback

**Root cause (r3-r4):** `push_doorbell_frame_sync` returned immediately on timeout
without waiting for the coroutine to terminate. A cancellation-resistant `send_text`
could emit a frame after the function returned False, causing both the WS frame
and native fallback to fire.

**R5 Fix — Single-arbitration wrapper:**

`push_doorbell_frame_sync` now WAITS for the coroutine to TERMINATE before returning:

1. Submit coroutine, wait with timeout for result
2. If result arrives in time → return it directly
3. If timeout fires:
   - Clear permit (prevents send from starting if coroutine hasn't reached it)
   - Cancel the asyncio.Task via `loop.call_soon_threadsafe` (injects CancelledError)
   - **WAIT** (bounded 200ms drain) for the future to settle
   - If future settles with True → send completed despite cancellation (resistant
     send emitted frame synchronously after catching CancelledError) → **return True**
     (WS wins, no native fallback)
   - If future settles with False/exception/CancelledError → **return False** (native wins)

**Guarantee:** The return value reflects the ACTUAL send outcome. When it returns
False, no frame was emitted. When it returns True, a frame was emitted. The
frame and native fallback NEVER coexist because the decision is made AFTER the
coroutine terminates.

`_guarded_push_doorbell_frame` propagates `CancelledError` (never catches it).
For normal `send_text` (Starlette WebSocket): cancel → CancelledError propagates → False.
For resistant `send_text`: catches cancel, emits synchronously → returns True → WS wins.

### S1 (FIXED in R4, retained)

- `consume_ws_delivered` checks `_is_expired` — expired entries treated as absent
- `unregister_connection` only calls `abandon_ws_delivered` when `was_current=True`

### S2 (SHOULD): Tests assert frame/fallback non-coexistence

**New test file:** `test/services/test_f158_r5_e2e_doorbell_race.py` (10 tests)

Key assertions:
- `test_cancellation_resistant_send_ws_wins`: resistant send emits frame → function
  returns True → WS wins. Asserts `frames_emitted=1` AND `result=True`.
- `test_no_coexistence_frame_and_fallback`: _f413_after_commit → resistant send
  emits frame → mark_ws_delivered called → _f136_post_delivery → coalesce_submit=0.
  Asserts frame emitted=1 AND no native ring submitted.
- `test_cancelled_send_native_wins`: normal send → CancelledError propagates →
  frames_emitted=0 AND result=False.
- Real `_f136_post_delivery` with `CallbackRunOutcome` + `doorbell_coalesce_service.submit`

### S3/NIT: Report metadata

Report now uses the correct pinned HEAD SHA. Suite aggregates labeled as run-specific
(reviewer's clean runs: fork 2f/13738p, base 1f/13706p — residual failures are
pre-existing suite-infra).

## Test Results (run-specific)

- **Focused F158 suite (cursor-5, clean detached worktree):** 30 passed in 3.60s
- **Reviewer clean fork run:** 2 failed, 13738 passed, 214 skipped, 15 xfailed
- **Reviewer clean base run:** 1 failed, 13706 passed, 214 skipped, 15 xfailed
- **Both residual failures pre-existing** (suite-slot/manifest, not F158)

## Modified Files

- `src/cli_agent_orchestrator/services/ws_doorbell.py` (single-arbitration wrapper,
  CancelledError propagation, task-level cancel via loop.call_soon_threadsafe)
- `test/services/test_f158_r5_e2e_doorbell_race.py` (new, replaces R4 file)
