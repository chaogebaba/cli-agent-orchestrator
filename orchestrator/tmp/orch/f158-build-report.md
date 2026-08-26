# F158-R3 Build Report

**Round:** R3
**Worktree:** /data/cao-scratch/f158-wt
**Branch:** cao/f158-build
**HEAD:** d1f4c68d
**Base:** 4edbf888 (F158-R2)

## Findings → Fixes

### B1 (BLOCKER): Late WS send coexists with native fallback

**Root causes identified:**
1. `push_doorbell_frame_sync` did not cancel/invalidate the submitted future on timeout
2. Direct-terminal POST route ran `create_inbox_message` (triggering F413 sync) on the FastAPI event-loop thread → deterministic same-loop deadlock (0.5s timeout, late send)
3. Redundant second `push_doorbell_frame_sync` call at api/main.py:7795-7805
4. Mailbox authority lock held across entire doorbell stash drain (cumulative 0.5s * N per stash entry)

**Fixes applied:**

| Sub-issue | File | Fix |
|-----------|------|-----|
| Cancel/invalidate | ws_doorbell.py | `future.cancel()` + `_invalidate_ws_send(terminal_id, row_id)` on timeout; `mark_ws_delivered` checks invalidation set before setting mark |
| Same-loop detection | ws_doorbell.py | Detect when `running_loop is target_loop`; return False immediately (no blocking) |
| Off-loop F413 | api/main.py | `create_inbox_message` wrapped in `asyncio.to_thread()` for direct-terminal POST |
| Redundant push | api/main.py | Removed WPDT W1 second push_doorbell_frame_sync call |
| Lock hold bound | mailbox_service.py | Pop doorbell stash BEFORE commit; drain entries AFTER lock.release() |

### S1 (SHOULD): delivered-mark set leaks stale entries

**Fixes applied:**
- `_ws_delivered` changed from `set` to `dict[tuple, float]` (key → monotonic timestamp)
- `_evict_stale()`: TTL-based eviction (30s) replaces clear-all-at-4096
- `abandon_ws_delivered(terminal_id)`: cleans all marks + invalidations on WS disconnect
- `consume_ws_delivered()`: now batch-removes marks for same terminal with row_id ≤ target
- Max entries reduced from 4096 to 2048 (entries have TTL now)

### S2 (SHOULD): Tests don't exercise claimed e2e ring protocol

**New test file:** `test/services/test_f158_r3_e2e_doorbell_race.py` (10 tests)

Tests cover:
- **Late-succeeding-timeout race:** slow WS → timeout → invalidation prevents late mark
- **Same-loop caller:** push_doorbell_frame_sync returns False in <0.1s, no send_text call
- **Normal success path:** WS delivers → mark set → F136 consumes → skip native ring
- **Abandoned path cleanup:** abandon_ws_delivered removes all terminal marks
- **Batch consume:** consume_ws_delivered cleans earlier marks
- **Targeted eviction:** old entries removed, recent entries kept

Tests do NOT patch `request_delivery` — real F136 signaling path exercised.

## Test Results

- **Full suite (box@cursor-3):** 13642 passed, 204 skipped, 39 failed (all pre-existing)
- **Doorbell tests (149):** 149 passed, 0 failed
- **New R3 e2e tests (10):** 10 passed, 0 failed
- **Pre-existing failures confirmed:** same tests fail on base commit 4edbf888

## Modified Files

- `src/cli_agent_orchestrator/services/ws_doorbell.py`
- `src/cli_agent_orchestrator/api/main.py`
- `src/cli_agent_orchestrator/services/mailbox_service.py`
- `test/services/test_f158_r3_e2e_doorbell_race.py` (new)
- `test/services/test_f158_r2_doorbell_regression.py` (adapt to dict-based _ws_delivered)
- `test/services/test_f158_doorbell_fallback.py` (adapt loop setup for R3 same-loop detection)
