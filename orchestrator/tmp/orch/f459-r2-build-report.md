# F459 r2 Build Report (#314)

- Branch: `cao/d80befd2` (worker worktree d80befd2), tip `2f8dd2c0`
- Base: merge of `origin/cao/a54c7e3c` (tip `ebb4b93c`) into `cao/d80befd2` (`9eb27cd0`)
- Task: r1 added `message_body` / `sender_display_name` kwargs to `ring_supervisor_doorbell`
  (doorbell_service.py:145) and `_attempt_native_ring` (doorbell_service.py:272); test mocks with
  narrower keyword-only signatures raised TypeError inside try/except-pass, failing 4 tests.
  Fix: add `**kwargs` to each mock signature. No product code (`src/`) changed.

## Mock sweep table (all functions mocking ring_supervisor_doorbell / _attempt_native_ring)

| File | Mock | Installed via | Action |
|---|---|---|---|
| test/services/test_fx168_doorbell.py:265 | `tracking_ring(tid, max_row_id, *, written_count=0)` | `patch(...ring_supervisor_doorbell, tracking_ring)` ×2 (L291/297) | **edited**: added `**kwargs` |
| test/services/test_fx168_doorbell.py:697 | `mock_ring(tid, max_id, *, written_count=0, caller_holds_no_delivery_lock=False)` | `monkeypatch.setattr` (L701) | **edited**: added `**kwargs` |
| test/services/test_fx168_doorbell.py:813 | `mock_ring(...)` same signature | `monkeypatch.setattr` (L817) | **edited**: added `**kwargs` |
| test/services/test_f186_reconciler_doorbell_lock.py:262 | `_capture_doorbell(...)` | `side_effect` (L332) | **edited**: added `**kwargs` |
| test/services/test_f186_reconciler_doorbell_lock.py:370 | `_capture_ring(...)` | `side_effect` (L434) | **edited**: added `**kwargs` |
| test/services/test_fx170_native_doorbell.py (many sites) | MagicMock `patch(..._attempt_native_ring, return_value=...)` | unittest.mock | already-fine (MagicMock accepts any kwargs) |
| test/services/test_f457_r2_gate_fixes.py (L175/201/227) | MagicMock `patch(..._attempt_native_ring, ...)` | unittest.mock | already-fine |
| test/services/test_f459_native_callback.py (L283+) | MagicMock `patch(..._attempt_native_ring, ...)` | unittest.mock | already-fine |
| test/services/test_delivery_service.py (L211/226/238) | MagicMock `patch(..._attempt_native_ring, return_value=...)` | unittest.mock | already-fine (out of verify scope, noted in sweep) |
| test/services/test_f216_null_socket_path.py (L208/251/293+) | real `_attempt_native_ring` called under MagicMock patches of deps | — | already-fine (no signature mocking) |

Grep method: `grep -rn 'ring_supervisor_doorbell\|_attempt_native_ring' test/` + `grep -rn 'def .*ring' test/ | grep -v 'def test_'` —
no other mock function defs found.

## Verification (offload box `box@cursor` via scripts/box-run.sh, label `f459-r2-suite`)

`uv run pytest -q` on the 7 files matching fx168/fx170/f186/f457/f459 at `2f8dd2c0`:

| File | Tests | Result |
|---|---|---|
| test/services/test_fx168_doorbell.py | 46 | pass |
| test/services/test_fx168_hotfix.py | 9 | pass |
| test/services/test_fx170_native_doorbell.py | 60 | pass |
| test/services/test_f186_reconciler_doorbell_lock.py | 6 | pass |
| test/services/test_f457_r2_gate_fixes.py | 5 | pass |
| test/services/test_f457_wake_gate_dedupe.py | 4 | pass |
| test/services/test_f459_native_callback.py | 17 | pass |
| **Total** | **147** | **147 passed in 6.81s** |

Raw output: box `~/box-scratch/f459-r2-run.txt`.

## Notes

- `orchestrator/tmp/orch/f459-gate-report.md` referenced by the task brief was NOT present in the
  fork main checkout (or anywhere in the tree); the inline spec in the task message was complete
  and was followed.
- Diff: 2 files, 5 signature lines (+5/−5). No src/ changes.
