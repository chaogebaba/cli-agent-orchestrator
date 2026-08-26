# F158-R2 Build Report — Doorbell Delivery Observability & Single Ring Owner

**Branch:** cao/f158-build
**Base:** f5acc7411cc555b4f8e6b3a3cfeaaad7b1de495c
**Round:** R2 (re-fix after GATE-NO with 2 BLOCKERs)

## Blocker Resolutions

### B1 — Transport-drop after armed check now observable

**Root cause:** `push_doorbell_frame_sync` submitted the coroutine via
`asyncio.run_coroutine_threadsafe` and returned `True` immediately (enqueue ack).
The actual `ws.send_text` result was never observed by `_f413_after_commit`.

**Fix:** `push_doorbell_frame_sync` now calls `future.result(timeout=0.5)` on the
submitted coroutine, returning the *actual* send outcome. When `send_text` raises
(transport drop) or the coroutine doesn't complete within 0.5s, the function
returns `False`. The fallback decision in `_f413_after_commit` now correctly sees
delivery failure.

**Blocking trade-off:** The 0.5s bounded wait is acceptable because:
- The after-commit hook runs on the ORM session thread (not the async event loop)
- WS `send_text` on localhost is sub-millisecond in the success case
- The 0.5s cap is a safety net for dead-connection detection, not a performance path
- The wait is per-stash-entry, not cumulative across entries

### B2 — Single post-write ring owner, no pre-write wake

**Root cause:** `_f413_after_commit` called `ring_supervisor_doorbell` directly when
WS was unarmed. This duplicated the F136 post-write doorbell AND violated the
write-before-wake contract (supervisor wakes before callback file exists).

**Fix:** Removed `ring_supervisor_doorbell` from `_f413_after_commit` entirely.
The after-commit hook now only does:
1. Attempt WS push (advisory frame)
2. If WS succeeded → `mark_ws_delivered(terminal_id, row_id)`
3. Always call `request_delivery(terminal_id)` → F136 runner writes callback → rings

In `_f136_post_delivery`, added `consume_ws_delivered` check before
`ring_supervisor_doorbell`. When WS already delivered (mark present), the native
ring is suppressed (no duplicate wake).

**Result per scenario:**
- WS armed + delivered: 1 WS frame, 0 native rings ✓
- WS armed + send failed: 0 WS frames, 1 native ring (F136 post-write) ✓
- WS unarmed: 0 WS frames, 1 native ring (F136 post-write) ✓

## F461 Compatibility

F461 has no code-level implementation in this repo — it was referenced in the
prior build report's compatibility claim. The restructuring preserves:
- Same `push_doorbell_frame_sync` signature and semantics (True = delivered)
- Same `request_delivery` unconditional call from after-commit
- Same F136 delivery runner write path (unchanged)
- Same inbox processing and obligation creation

## Files Changed

- `src/cli_agent_orchestrator/services/ws_doorbell.py` — bounded wait in
  push_doorbell_frame_sync; added mark_ws_delivered/consume_ws_delivered dedup state
- `src/cli_agent_orchestrator/clients/database.py` — _f413_after_commit restructured:
  removed direct ring, added mark_ws_delivered on success
- `src/cli_agent_orchestrator/services/inbox_service.py` — _f136_post_delivery:
  consume_ws_delivered check before native ring
- `test/services/test_f158_doorbell_fallback.py` — updated to match new semantics
- `test/services/test_f158_r2_doorbell_regression.py` — new regression tests

## Test Evidence

- **Local targeted (doorbell/delivery modules):** 195 passed in 62s
- **Box full suite (cursor-4):** 13626 passed, 204 skipped, 15 xfailed, 321s
  - 16 failures all pre-existing (confirmed identical on base commit):
    `_FakeMailbox.cc_inbox_path`, quarantine-serial, trace manifest byte-exact
- **Zero new failures introduced by F158-R2**

## S1/S2 Notes (SHOULD from gate report)

- S1 (caller_holds_no_delivery_lock flag): Moot — the flag is no longer used from
  `_f413_after_commit` since that hook no longer calls `ring_supervisor_doorbell`.
- S2 (idempotent regression test): The idempotent-hit path still stashes 4-tuples
  correctly; the R2 changes don't affect the stash format or the ORM listener logic.
