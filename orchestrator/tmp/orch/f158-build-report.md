# F158 Build Report — Doorbell Fallback for Idle Supervisor Push

## Branch / SHA

- Branch: `cao/f158-build`
- HEAD: `59c7d468` (will update after final commit)
- Base: `main @ 1932fa78`

## Root Cause

When the WebSocket doorbell connection drops (keep-alive timeout, network hiccup,
or supervisor CLI disconnect/reconnect), `push_doorbell_frame_sync` silently returns
without waking the supervisor. **No fallback mechanism existed** — messages sat
PENDING until the periodic reconciler (`reconcile_pull_mode_notifications`) ran,
which has a `INBOX_RECONCILE_GRACE_SECONDS` delay before it picks up orphaned rows.

The structural gap in the push chain:
1. `_f413_after_commit` (ORM after-commit hook) calls `push_doorbell_frame_sync` → WS dead → silent no-op
2. Same hook calls `request_delivery` → `deliver_pending` → pull-mode gate → writes CC native inbox file but does NOT ring `ring_supervisor_doorbell` (removed in fx168 FIX-4 due to delivery_lock deadlock)
3. No immediate wake path remains → messages are stranded

**Secondary defect**: The idempotent-hit path in the ORM listener stashed a
3-tuple `(logical_receiver_id, row_id, preview)` while the after-commit handler
expected 4-tuple destructuring `(terminal_id, row_id, sender_short, preview)`.
This caused a ValueError that was swallowed by bare `except Exception: pass`,
silently breaking the doorbell for ALL subsequent entries in the same commit batch.

## Fix (3 changes)

### 1. `push_doorbell_frame_sync` returns bool (ws_doorbell.py)
- Returns `True` when frame was posted to a live connection
- Returns `False` when WS disabled, unarmed, or loop unavailable
- Backward-compatible: callers that ignore the return value still work

### 2. `_f413_after_commit` — doorbell fallback (database.py)
- Iterates stash entries safely (handles 3-tuple AND 4-tuple)
- When `push_doorbell_frame_sync` returns False, calls `ring_supervisor_doorbell`
  with `caller_holds_no_delivery_lock=True` as immediate fallback
- Uses the same proven native socket/pane-nudge path the reconciler uses

### 3. Idempotent-hit stash fixed to 4-tuple (database.py)
- Changed `stash.append((logical_receiver_id, row_id, preview[:120]))` →
  `stash.append((target.receiver_id, row_id, (target.sender_id or "")[:8], preview[:120]))`
- Uses `target.receiver_id` (terminal_id) instead of `logical_receiver_id` (mailbox_id)

## F461 Compatibility

The fix calls `ring_supervisor_doorbell` — the same entry point F461's doorbell
coalescer wraps. If F461 adds coalescing at the `ring_supervisor_doorbell` entry
point, the fallback naturally gets coalesced too. No overlap or conflict.

## Test Results

### New tests: `test/services/test_f158_doorbell_fallback.py` — 9 tests
- AC1: WS unarmed → fallback ring fires ✓
- AC2: WS armed → no fallback ✓
- AC3: 3-tuple legacy entry handled gracefully ✓
- AC4: push_doorbell_frame_sync returns bool ✓
- AC5: Multiple entries all processed ✓
- AC6: ORM listener stash is 4-tuple ✓

### Targeted suite (box@cursor-3): 156 passed in 6.49s
Covers: test_f413_orm_listeners, test_fx168_doorbell, test_fx170_native_doorbell,
test_f186_reconciler_doorbell_lock, test_f457_wake_gate_dedupe, test_f459_native_callback

### Full suite (box@cursor-3): 13617 passed, 13 failed (pre-existing), 204 skipped
Pre-existing failures on base commit `1932fa78`:
- `test_f424_f426_inbox_mutation_kills` (6): `_FakeMailbox` missing `cc_inbox_path` — fixture issue
- `test_fx168_hotfix::TestFix2StalePathSelfHeal` (2): same fixture gap
- `test_stage0_flip_machinery` (1): trace manifest byte count
- `test_suite_slot::TestPidReuseGuard` (1): timing-sensitive test
- Other 3: transient/fixture issues

Zero new failures introduced by F158.

## Files Modified

- `src/cli_agent_orchestrator/services/ws_doorbell.py` — return type change
- `src/cli_agent_orchestrator/clients/database.py` — after-commit fallback + stash fix
- `test/services/test_f158_doorbell_fallback.py` — new test file (9 tests)
