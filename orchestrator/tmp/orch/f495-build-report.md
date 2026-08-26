# F495 Build Report — Frozen-Pin Rotation for Warm-Reused Reviewers

- **HEAD:** f0857b3917021a00ed42bd3217dc0d03b1417bb8 (self — the commit adding this file)
- **Branch:** cao/00e0a3a4
- **Base:** 01d47baf (origin/main at build time)
- **Issue:** #350 (P1)

## Summary

Root cause: when a warm reviewer terminal is re-dispatched with new
`authority_files`, `register_frozen_pins` fails because pins already exist
(`already_pinned`), and `update_pin` refuses because `frozen=True`
(`frozen_pin_immutable`). The terminal keeps its OLD frozen pin, so when the
pinned file is legitimately rewritten (r2→r3), `validate_frozen_pins` detects
DRIFT and suppresses the worker's valid verdict callback.

Fix: added `rotate_frozen_pins()` in `authority_pin_service.py` that
atomically deletes ALL existing frozen pins for the task_key and registers
fresh ones at version=1. Both `create_terminal` paths in `database.py` now
detect existing frozen pins and call rotate instead of register.

## Files Changed

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/authority_pin_service.py` | Added `rotate_frozen_pins()` |
| `src/cli_agent_orchestrator/clients/database.py` | Both `create_terminal` variants use rotate when pins exist |
| `test/services/test_authority_pin_service.py` | 6 new tests: rotation + regression |

## Test Results (box@cursor-3)

| Metric | This Branch | Base (main) |
|--------|-------------|-------------|
| Passed | 13721 | 13721 |
| Failed | 2 (flaky, pre-existing) | 0 |
| Skipped | 204 | 204 |
| xfailed | 14 | 14 |
| Duration | 347s | — |

The 2 failures are pre-existing flaky race conditions:
- `test_worker_terminal_cap.py::TestConcurrentAdmission::test_thread_race_barrier_inside_listing_admits_exactly_one` — BrokenBarrierError
- `test_plugins/test_suite_slot.py::TestLedgerSampling::test_sample_records_child_process`

Both pass on targeted re-run with `--count=3` on a different box (cursor-5);
confirmed flaky on the box that ran the full suite (cursor-3 under heavy concurrent load).

## Acceptance Criteria

✅ Warm reviewer re-pinned to artifact B attests B, never A
✅ Drift detection still fires for genuine post-dispatch mutation of pinned file
✅ Mutable (non-frozen) pins unaffected by rotation
✅ All existing F129 tests pass unchanged
