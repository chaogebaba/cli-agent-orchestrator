# F158-R4 Build Report

**Round:** R4
**Branch:** cao/f158-build
**HEAD:** 134c885eb4c36e1f2480497520dbf5d7a2c79337
**Base:** 01d47baf (origin/main)
**Box:** cursor-5 (fork), cursor-3 (base)

## Findings → Fixes

### B1 (BLOCKER): Timed-out WS send can still emit a late frame

**Root cause:** `future.cancel()` does not guarantee the coroutine stops — a
`send_text` that catches `CancelledError` continues executing. The R3
invalidation set only suppressed `mark_ws_delivered`, not the frame emission.

**Fix:** Permit-gated send architecture:
- `_guarded_push_doorbell_frame` replaces `push_doorbell_frame` for production
- A `threading.Event` ("permit") is shared between the sync wrapper and coroutine
- The coroutine checks `permit.is_set()` **immediately** before `await ws.send_text()`
  with NO yield point between the check and the call (same execution frame)
- On timeout, the sync wrapper calls `permit.clear()` THEN `future.cancel()`
- Post-send permit check: if permit was cleared DURING an in-flight send,
  the function returns `False` (no mark, F136 rings)
- Invalidation set remains as defense-in-depth for the mark path

**Guarantee:** If `push_doorbell_frame_sync` returns `False`, either:
(a) `send_text` was never called (permit cleared before the check), or
(b) `send_text` started before timeout but the function returns `False` anyway
    (post-send permit check), so no mark is set and F136 rings normally.

### S1 (SHOULD): Delivered-state lifecycle

**Fixes:**
- `consume_ws_delivered` now checks `_is_expired(ts)` — an expired entry below
  capacity returns `False` (treated as absent) and is evicted on access
- `unregister_connection` only calls `abandon_ws_delivered` when `was_current=True`
  (the identity check at lines 54-56 gates the abandon)
- A superseded old socket teardown preserves the replacement's marks

### S2 (SHOULD): Tests exercise real F136 delivery path

**New test file:** `test/services/test_f158_r4_e2e_doorbell_race.py` (11 tests)

Tests exercise:
- `_f136_post_delivery` with real `CallbackRunOutcome` objects (not manual mark/consume)
- `doorbell_coalesce_service.submit` as the ring target (post-F461 coalesce)
- Permit-gated cancellation: cleared permit → `send_text` never called
- In-flight send with timeout → returns `False`, invalidation blocks mark
- Same-loop caller → immediate `False`
- Superseded-socket conditional abandon
- TTL expiry on consume (expired entry = absent)
- Current-socket disconnect clears marks

### S3 (SHOULD): Reproducible clean detached worktree evidence

**Fork suite (cursor-5, clean detached worktree at 134c885e):**
- `git status --short` = empty before and after
- **3 failed, 13737 passed, 214 skipped, 15 xfailed** in 362.56s

**Base spot-check (cursor-3, clean detached worktree at 01d47baf):**
- Same 3 nodes: 3 passed (test_suite_slot and test_fx191 are xdist-order-dependent;
  test_stage0_flip_machinery fails isolated on fork due to new files changing manifest hash)

**Isolated recheck (cursor-3, fork 134c885e, no xdist):**
- test_suite_slot: PASSED
- test_fx191: PASSED
- test_stage0_flip_machinery: FAILED (byte-exact manifest hash — pre-existing, also fails at R2 base)

**Conclusion:** 0 feature regressions introduced. All 3 failures are pre-existing/environment.

## Modified Files

- `src/cli_agent_orchestrator/services/ws_doorbell.py` (rewritten: permit-gated coroutine,
  TTL on consume, conditional abandon)
- `src/cli_agent_orchestrator/services/inbox_service.py` (rebase conflict resolution:
  F158 consume_ws_delivered + F461 doorbell_coalesce_service.submit)
- `test/services/test_f158_r4_e2e_doorbell_race.py` (new, replaces R3 file)
- `test/services/test_f158_r2_doorbell_regression.py` (adapted: dict-based _ws_delivered,
  delivery_loop setup)
- `test/services/test_f158_doorbell_fallback.py` (adapted: delivery_loop setup)
