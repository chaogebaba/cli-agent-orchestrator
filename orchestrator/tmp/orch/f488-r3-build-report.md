# F488 R3 Build Report — memory default OFF

**Code-SHA:** `e59377a1` (branch `cao/f488-build`)
**Base-SHA:** `37378b32` (r2 artifact commit)
**Report-SHA:** (this commit, child of e59377a1)

## Findings addressed

### BLOCKER 1 — malformed persisted memory.enabled fails open → FIXED

**Problem:** `get_memory_settings()` merged raw persisted `memory.enabled` without
type validation; `is_memory_enabled()` used `bool(value)` — so `"false"`, `"0"`,
`1`, `{}`, `[]` all enabled memory.

**Fix (two layers):**

1. `get_memory_settings()` (settings_service.py ~398-409): after `result.update(saved)`,
   validates `result["enabled"]` — only literal `True`/`False` (Python bool, i.e.
   JSON boolean) passes. Any other type logs a warning with the repr and sets
   `result["enabled"] = False` (fail-closed). The env-var overlay below can still
   override.

2. `is_memory_enabled()` (settings_service.py ~503): changed from `return bool(value)`
   to `return value is True` — identity check as defense-in-depth ensures fail-closed
   even if someone constructs settings dict bypassing `get_memory_settings()`.

**Regression test (AC8):** table-driven parametrize in
`test/services/test_memory_enabled_flag.py::TestMalformedPersistedEnabledFailsClosed`
covering all cases demanded by the r2 gate report:

| Persisted value | Expected | ID |
|---|---|---|
| `True` | `True` | bool-true |
| `False` | `False` | bool-false |
| `"false"` | `False` | string-false |
| `"0"` | `False` | string-zero |
| `0` | `False` | int-zero |
| `1` | `False` | int-one |
| `"true"` | `False` | string-true |
| `null` | `False` | null |
| `{}` | `False` | empty-dict |
| `[]` | `False` | empty-list |
| (key absent) | `False` | missing-key |

Plus `test_invalid_types_log_warning` parametrize verifying the warning message
fires for non-bool values.

### NIT 1 — tier-census stale names → FIXED

- `test_defaults_to_true_when_absent` → `test_defaults_to_false_when_absent`
- `test_enabled_default_preserves_round_trip` → `test_enabled_explicitly_preserves_round_trip`
- Added all AC6 (3 nodes), AC7 (3 nodes), AC8 (16 nodes) entries.

### NIT 2 — report HEAD labeling → FIXED

This report labels code-SHA (`e59377a1`) distinctly from the report commit (child
of code-SHA, created when this report is committed).

## Test results

Focused suite on box@cursor-4 at `e59377a1`:

```text
uv run pytest -q test/services/test_memory_enabled_flag.py \
  test/services/test_config_service.py \
  test/services/test_learning_enabled_flag.py
86 passed in 5.55s
```

`test_memory_enabled_flag.py` alone: 39 passed (was 23 pre-r3; +16 from AC8).

## Pre-existing failures

Not re-run in this round — the 3 pre-existing flakes identified in r2
(`test_sample_ledger_monotonic_growth`, `test_server_shut_down_under_us_is_a_confirmed_absence`,
`test_ready_completion_at_deadline_has_one_lawful_owner`) are unrelated to
settings_service and confirmed pre-existing on main in the r2 report.

## Files changed (df543db2..this commit)

- `src/cli_agent_orchestrator/services/settings_service.py` — validation + identity check
- `test/services/test_memory_enabled_flag.py` — AC8 regression class
- `test/tier-census.json` — name corrections + AC6/AC7/AC8 nodes
