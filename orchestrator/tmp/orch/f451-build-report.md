# F451 (#306) Build Report — Cap-Registry Cardinality Metric

## Diff Summary

**2 files changed, 269 insertions(+), 4 deletions(-)**

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/terminal_service.py` | +63/-4 — cardinality tracking globals, `_check_cap_registry_cardinality()` function, NIT comment fix |
| `test/services/test_cap_registry_cardinality.py` | +210 (new) — 12 tests across 3 classes |

### Production Changes (`terminal_service.py`)

1. **Cardinality metric** (lines ~406-417): Three new module globals:
   - `_CAP_REGISTRY_WARN_CARDINALITY`: threshold from env `CAO_CAP_REGISTRY_WARN_CARDINALITY` (default 512, <=0 disables)
   - `_cap_registry_cardinality`: distinct-session-name counter (process-lifetime)
   - `_cap_registry_warned_at`: highest crossing already warned (prevents duplicate warnings)

2. **Tracking in `_cap_admission_lock`** (lines ~3855-3861): On first entry for a new session name, increments `_cap_registry_cardinality` and calls `_check_cap_registry_cardinality()`.

3. **`_check_cap_registry_cardinality()`** (lines ~3863-3895): Logs ONE `logger.warning` per power-of-two crossing of the threshold (512, 1024, 2048, …). Never refuses admission. Never reclaims. Called under `_cap_admission_locks_guard` so the counter is consistent.

4. **NIT fix** (lines ~436-442): Comment for `_cap_gen` corrected from "bumped on EVERY ledger mutation (reserve, publish transition, release)" to "bumped on publish transitions and releases (NOT on a plain reserve)". Aligns with the actual implementation at lines 4044 and 4102.

### Semantics Preserved
- No refusal path added — `_cap_admission_lock` always returns a lock.
- No reclamation added — `_cap_gen`, `_cap_token_seq`, `_cap_admission_locks` remain permanent per session.
- Admission/release paths completely unchanged (only a counter increment inserted in the lock-creation branch).

## Test Evidence

### New tests (`test/services/test_cap_registry_cardinality.py`)

```
Box: box@cursor | Branch: cao/59428c90 | Commit: d579191c
Exit code: 0 | 12 passed in 1.68s
```

Classes:
- `TestCardinalityWarning` (5 tests): warning fires exactly once at each power-of-two crossing, not between crossings, not on duplicates.
- `TestCardinalityDisabled` (2 tests): threshold=0 and threshold=-1 both suppress all warnings.
- `TestAdmissionReleasePaths` (5 tests): lock identity stable, different sessions get different locks, high cardinality never refuses, gen/token_seq untouched by warning.

### Existing tests (`test/services/test_worker_terminal_cap.py`)

```
Box: box@cursor | Branch: cao/59428c90 | Commit: d579191c
Exit code: 0 | 39 passed in 3.28s
```

All original F439 tests pass unmodified (concurrent admission, seqlock, ledger-authoritative counting, r6 ABA safety, r8 double-cancel).

### Pre-fix-red Proof

Disabled the cardinality increment (`_cap_registry_cardinality += 1` and `_check_cap_registry_cardinality()` commented out on the box), then ran:

```
FAILED test/services/test_cap_registry_cardinality.py::TestCardinalityWarning::test_warning_fires_at_threshold_crossing
    AssertionError: assert 'cap_registry_cardinality_high' in ''
1 failed in 1.71s
```

Confirms the test is sensitive to the production code and cannot pass without it.

## Branch / Commit

- **Branch**: `cao/59428c90`
- **Commit**: `d579191c`
- **Report**: `orchestrator/tmp/orch/f451-build-report.md`
