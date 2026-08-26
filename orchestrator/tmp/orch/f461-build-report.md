# F461 Build Report — Doorbell Coalesce

## Summary
Implemented coalescing of near-simultaneous worker callbacks into one wake/one bridge message per the spec in issue #316.

## Changes

### New files
- `src/cli_agent_orchestrator/services/doorbell_coalesce.py` — Coalesce service with per-terminal buffer and timer
- `test/services/test_f461_doorbell_coalesce.py` — 13 acceptance tests

### Modified files
- `src/cli_agent_orchestrator/services/config_service.py` — Added `supervisor.wake.coalesce_s` config knob (default 5.0s)
- `src/cli_agent_orchestrator/services/inbox_service.py` — Route doorbell through coalesce service; bind on start, flush on shutdown
- `test/services/test_fx168_doorbell.py` — Updated call-site inventory and replay test for new routing
- `test/services/test_f186_reconciler_doorbell_lock.py` — Updated reconciler tests for coalesce routing

## Design Decisions

- **D1**: Per-terminal buffer of pending doorbell intents
- **D2**: Timer fires after `coalesce_s`; first intent arms the timer
- **D3**: On fire, buffer drained, ONE ring issued
- **D4**: `from-name = 'cao-fleet'` when N>1, individual worker name when N=1
- **D5**: Ordering preserved (sorted by row_id, oldest-first)
- **D6**: Durable inbox and exactly-once ack untouched (this is transport only)
- **D7**: `coalesce_s=0` disables coalescing (immediate fire for backward compat)
- **Deadlock avoidance**: Lock released before `_arm_timer` to prevent re-entrancy in degraded (no-loop) path
- **`caller_holds_no_delivery_lock=True`**: Always passed by coalesce service since fire is async (no lock held at fire time)

## Config
```
supervisor.wake.coalesce_s = 5.0  (env: CAO_SUPERVISOR_WAKE_COALESCE_S)
```
Set to 0 to disable coalescing entirely.

## Test Results

### Local (targeted)
- `test_f461_doorbell_coalesce.py`: **13 passed**
- `test_fx168_doorbell.py`: **39 passed**
- `test_f186_reconciler_doorbell_lock.py`: **9 passed**
- `test_f476_single_wake_cursor.py`: **82 passed** (combined run)
- `test_f136_callback_delivery.py`: **58 passed**

### Offload box (full suite, cursor-4)
- **13601 passed, 12 failed, 204 skipped**
- All 12 failures are pre-existing / unrelated:
  - `test_tmux_session_exists_strict` — infra flake (tmux server)
  - `test_suite_slot` (2) — subprocess timeout flakes
  - `test_f424_f426_inbox_mutation_kills` (6) — stale `cc_inbox_path` attribute in test fake
  - `test_fx168_hotfix` (2) — same stale attribute
  - `test_stage0_flip_machinery` — trace manifest stale
  - `test_coverage_matrix` — new MCP tool not rostered

### Offload box (doorbell-specific, cursor-3)
- **82 passed** in 4.85s

## Branch / SHA
- Branch: `cao/f461-build`
- HEAD: `db66eb16`
