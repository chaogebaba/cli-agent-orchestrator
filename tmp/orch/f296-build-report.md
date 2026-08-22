# F296 Build Report: fix cleanup_deferred zombie rows

**Branch:** `cao/f296` (off `cao/wp-suite-d6b`)  
**Issue:** #150 (P1)  
**Root cause report:** `orchestrator/tmp/orch/f296-rootcause.md`

## Summary

The F255 fix (commit `6d7556f7`, merged 2026-08-18) corrected a crash (`return
False` → `return dict`) but conflated two distinct failure modes under a single
`rollback_kill_uncertain: True` flag. The `cleanup_deferred` path (Grok provider
home cleanup) has a confirmed-dead window — unlike the true kill-uncertain path
where `window_liveness != "gone"`. Reusing the same flag caused:

1. The cascade caller to stop at cleanup-deferred nodes (unnecessary)
2. Zombie DB rows accumulating with no automatic retry (6 rows today alone)

## Changes

### `src/cli_agent_orchestrator/services/terminal_service.py`

1. **Line ~5607** — `cleanup_deferred` return dict: `rollback_kill_uncertain`
   changed from `True` → `False`. The window kill IS confirmed; only FS
   cleanup is deferred.

2. **Lines ~5248-5252** — Cascade caller (`_delete_terminal_inner`): Added
   `if result.get("cleanup_deferred"):` branch that records the node as
   `"cleanup_deferred"` in `reaped` and continues the cascade (no stop).

3. **Line ~559** — `purge_stale_terminal_records`: Added best-effort
   `provider_manager.cleanup_provider(terminal_id)` call before the DB row
   delete. Retries Grok cleanup (processes likely dead by startup time); if
   still deferred, proceeds with delete anyway (window confirmed gone, row
   must not persist forever).

### `test/services/test_f296_cleanup_deferred_cascade.py` (new)

5 tests covering:
- Path B: `cleanup_deferred` returns `rollback_kill_uncertain=False`
- Path A: true uncertain still returns `rollback_kill_uncertain=True` + quarantines
- Cascade: `cleanup_deferred` node does NOT stop cascade (continues)
- Cascade: true uncertain node DOES stop cascade (unchanged behavior)
- Purge: `purge_stale_terminal_records` calls `cleanup_provider` before DB delete

### `test/services/test_f255_deferred_cleanup_delete.py` (updated)

Updated assertions to match new behavior: `rollback_kill_uncertain` is now
`False` for cleanup-deferred path.

## Test Results

### Targeted (`-k "terminal or cleanup or f296"`)
- **1060 passed**, 5 skipped, 1 xfailed
- 2 pre-existing failures (confirmed on base branch `cao/wp-suite-d6b`)

### Full default suite
- **11586 passed**, 157 skipped, 8 xfailed, 1 xpassed
- 4 pre-existing failures (0 new failures introduced)
- Duration: 4m35s

## Behavioral change

| Scenario | Before (F255) | After (F296) |
|----------|--------------|--------------|
| Cleanup deferred | Cascade stops, returns `uncertain`, zombie row persists forever | Cascade continues, row recorded as `cleanup_deferred` in reaped list, startup sweep retries cleanup |
| True kill-uncertain | Cascade stops, quarantines | **Unchanged** — still quarantines |
| Normal delete | Cascade proceeds | **Unchanged** |

## Deferred work

- A periodic background retry (timer-based) for cleanup-deferred rows was
  considered but NOT implemented. The startup sweep (`purge_stale_terminal_records`)
  handles it on next server restart. If mid-session accumulation becomes a
  problem, a periodic sweep can be added later.
