# F255 Fix Report — delete_terminal 500s on grok deferred cleanup

## Root Cause

`_delete_terminal_under_lease` (terminal_service.py:5598-5601) returns bare `False`
when `provider_manager.cleanup_provider(terminal_id)` defers grok private-home cleanup.
The function's signature is `-> Dict`, and all 4 callers call `.get("rollback_kill_uncertain")`
on the return value — raising `AttributeError: 'bool' object has no attribute 'get'`.

**Call chain:**
```
api/main.py:6526  (delete_terminal endpoint)
  → terminal_service.py:5141  (delete_terminal → _delete_terminal_inner)
    → terminal_service.py:5234  (_delete_terminal_under_lease call)
      → terminal_service.py:5598  (cleanup_provider returns False for grok)
      → terminal_service.py:5601  ← `return False`  ← BUG
    → terminal_service.py:5244  ← `result.get(...)` ← CRASH
```

**Trigger:** Grok terminals in error/wedged state where the private-home updater process
cannot yet be confirmed stopped. `GrokCliProvider.cleanup()` returns `False` to signal
"retry later", `ProviderManager.cleanup_provider()` propagates this, and the delete path
crashed before reaching the "uncertain/retain" handling.

**Impact:** tmux window killed successfully, but record cleanup aborts → undeletable
zombie rows (reported: ac0dfdb0, 1acd606c, 10cb8eaf, 79f896fe). Reproduced 6× on 2026-08-18.

## Fix

**File:** `src/cli_agent_orchestrator/services/terminal_service.py`
**Change:** Replace `return False` at the deferred-cleanup early-exit with a proper dict:

```python
return {
    "terminal_deleted": False,
    "intent_deleted": False,
    "intent_error": None,
    "intent_retain_reason": "cleanup_deferred",
    "rollback_kill_uncertain": True,
    "cleanup_deferred": True,
}
```

This is the minimal sound fix at the true source — it restores the function's `-> Dict`
contract with a semantically correct result. The `rollback_kill_uncertain: True` flag
triggers the existing "uncertain/retain" path in all callers, stopping the cascade
cleanly and keeping metadata for retry.

## Resilience (Idempotent Re-delete)

No additional design change needed. The existing architecture already supports this:
1. `rollback_kill_uncertain: True` tells the cascade caller to stop and report "uncertain"
2. Metadata is retained (DB record stays)
3. A subsequent `DELETE /terminals/{id}` retries `cleanup_provider`
4. Once grok processes exit, `cleanup_provider` succeeds → full deletion completes

The bug was simply preventing the code from reaching this designed retry path.

## Regression Test

**File:** `test/services/test_f255_deferred_cleanup_delete.py`

4 tests:
- `test_deferred_cleanup_returns_dict_not_bool` — exercises exact production path with cleanup deferred
- `test_successful_cleanup_returns_dict` — normal path still returns dict
- `test_result_get_on_deferred_dict_does_not_crash` — verifies .get() on new shape
- `test_old_return_false_would_crash` — proves the old code crashes (pins failure shape)

## Suite Results

```
make test-quick ARGS="-n 2 --timeout=60"
11543 passed, 4 failed (pre-existing, unrelated), 149 skipped
```

Pre-existing failures (not introduced by this change):
- `test/services/test_fifo_reader.py` — race condition in writer EOF handling
- `test/test_f254_quarantine.py` — quarantine entry missing expires field
