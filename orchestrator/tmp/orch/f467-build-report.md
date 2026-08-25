# F467 Build Report — fleet_service since_last_input always 0.0

## Branch & Tip

- **Branch**: `cao/3e3a090d`
- **Tip SHA**: `028a01f7`
- **Parent**: `31983ecf` (main)

## Files Touched

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/fleet_service.py` | Replace `get_localzone()` stamping with `_as_utc()` for naive last_active; remove `tzlocal` import |
| `src/cli_agent_orchestrator/services/cleanup_service.py` | Replace `get_localzone()` stamping of `cutoff_date` with `replace(tzinfo=timezone.utc)`; remove `tzlocal` import |
| `src/cli_agent_orchestrator/services/inbox_service.py` | Replace `get_localzone()` stamping in `_last_active_key` with `replace(tzinfo=timezone.utc)`; remove `tzlocal` import |
| `test/services/test_f72_fleet_lifecycle.py` | Replace old test (validated buggy behavior) + add parametrized 4-TZ test |
| `test/services/test_wpm1_delivery.py` | Remove stale `get_localzone` monkeypatch for cleanup_service |

## Bug-Family Sweep

| Site | File:Line | Source of naive dt | Was buggy? | Fixed? |
|------|-----------|-------------------|-----------|--------|
| `since_last_input` | fleet_service.py:185 | DB `last_active` (naive-UTC-at-rest) | YES — primary bug | YES |
| `now_cutoff` | cleanup_service.py:152 | `_utcnow()` return (naive-UTC) | YES — same pattern (over-retention) | YES |
| `_last_active_key` | inbox_service.py:1363 | DB `last_active` (naive-UTC-at-rest) | YES — same pattern (sort order wrong on offset hosts) | YES |
| `_reply_created_at_utc` | database.py:7675 | Pre-hotfix rows that are genuinely local-at-rest | NO — intentional legacy handling | Not touched |

## Test Counts

| Scope | Passed | Failed | Notes |
|-------|--------|--------|-------|
| F467 focused (5 tests) | 5 | 0 | 4 parametrized TZ + 1 basic |
| test_f72_fleet_lifecycle.py | 102 | 1 | 1 pre-existing failure (fork lifecycle, unrelated) |
| test_wpm1_delivery.py | 1 | 0 | The cleanup retention test |
| **Total** | 103 | 1 (pre-existing) | |

## RED Evidence

```
FAILED f467-red-test.py::test_f467_red_proof
  AssertionError: RED EVIDENCE: expected ~45.0 but got 0.0.
  The bug: get_localzone() misinterprets naive-UTC as local time.
```

Run on `origin/main` (parent `50a64f74`) with monkeypatched `get_localzone → America/New_York (UTC-4)`:
naive-UTC 45s ago stamped as local → when compared to `now(UTC)`, delta = -4h+45s → negative → clamp to 0.0.

## GREEN Evidence

All 5 F467 tests pass on `cao/3e3a090d` (tip `028a01f7`):
- `test_fleet_since_last_input_treats_naive_db_clock_as_utc` — PASSED
- `test_f467_since_last_input_correct_regardless_of_host_tz[America/New_York--4]` — PASSED
- `test_f467_since_last_input_correct_regardless_of_host_tz[Asia/Kolkata-5.5]` — PASSED
- `test_f467_since_last_input_correct_regardless_of_host_tz[Europe/Berlin-2]` — PASSED
- `test_f467_since_last_input_correct_regardless_of_host_tz[Pacific/Auckland-12]` — PASSED

## Box Actions Ledger (box@cursor)

| Invocation | Label | Command |
|-----------|-------|---------|
| box-run.sh | f467-red | `bash ~/f467-red2.sh` (checkout origin/main, overlay test, run RED) |
| box-run.sh | f467-green | `bash ~/f467-green.sh` (checkout origin/cao/3e3a090d, run GREEN suite) |
| raw ssh | — | Read-only: verify pre-existing failure on main (1 command) |
| box-run.sh | f467-cleanup | `bash ~/f467-box-cleanup.sh` (git clean, restore main, rm scripts) |

- Checkout left at: `origin/main` (`50a64f74`), clean state
- Env mutations: none
- Temp files left: none (cleaned up)
- Deviations: none
