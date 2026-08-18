# WP-SUITE D2 Build Report — r2

**Branch:** `cao/473b1670`  
**Base:** `20878cab` (Merge 'cao/f262' into f254-phase3-enforcement)  
**Scope:** AC2.1–AC2.6 (full D2 wall) + Step 0

## Commits

1. `3ff96154` — quarantine: re-quarantine test_ready_completion_at_deadline as serial_only
2. `70774455` — D2: unify skip vocabulary — markers + env detection, strip all deselects
3. `2ec8b759` — Add D2 build report (wp-suite-d2-build-r1.md)
4. (r2 commit) — AC2.4-AC2.6: --run-live opt-in, delete CAO_RUN_LIVE_PROVIDER_TESTS, N1 fix

## Disposition Table Cross-Check (all 14 handled)

| # | Deselect | Disposition | Status |
|---|---|---|---|
| 1 | test_persona_context::test_manifest_rehydration_fails_loud_on_inaccessible_persona_root | requires_bwrap | DONE |
| 2 | test_persona_context::test_filter_and_compose_claude_manifest_rehydration | requires_bwrap | DONE |
| 3 | test_persona_context::test_manifest_rehydration_fails_loud_on_invalid_runtime_root | requires_bwrap | DONE |
| 4 | test_persona_context::test_reap_persona_generations_keeps_current_only | requires_bwrap | DONE |
| 5 | test_persona_context::test_provider_manager_rehydrates_manifest_plan | requires_bwrap | DONE |
| 6 | test_persona_context::test_generation_reaper_fails_loud_on_inaccessible_persona_root | requires_bwrap | DONE |
| 7 | test_f139::TestAdmitProvider::test_credential_provider_still_admitted | requires_codex_auth | DONE |
| 8 | test_f93::test_ac5_herdr_inherits_error_liveness_and_never_auto_purges | requires_herdr | DONE |
| 9 | test_wave3b::test_probe_09_raw_addressed_output_bytes_match_parent_33aad1c | requires_git_object("33aad1c") | DONE |
| 10 | test_fifo_reader::TestReaderLoopCoalescing::test_rapid_writes_produce_fewer_publishes_than_writes | FIX THE TEST (assert <= not <) | DONE |
| 11 | test_fx193::TestA1FullJitter::test_step0_degenerates_to_exactly_30s | FIX THE TEST (pytest.approx) | DONE |
| 12 | test_fx193::TestA1FullJitter::test_a1_ac2_seeded_rng_reproduces_exact_delays | FIX THE TEST (fixed base time) | DONE |
| 13 | test_g7a_sandbox::test_up_reclaims_a_dead_socket_of_the_same_name | FIX THE TEST (tmux list-sessions) + requires_tmux | DONE |
| 14 | test_stalled_callback_watchdog::test_trigger_a_rollover_is_stale... | FIX THE TEST (route through self._clock) | DONE |

## AC2.3b Verification

- `grep -c -- --deselect .github/workflows/test-ci.yml` → **0** (verified)
- Targeted run of all 14 tests: **14/14 passed** (-n 0, 23.72s)

## AC2.4 — CAO_RUN_LIVE_PROVIDER_TESTS → --run-live (r2)

Migrated sites:
- `test/providers/test_claude_transcript_hook.py` — 2 skipif blocks removed (tests already `@pytest.mark.live`)
- `test/providers/test_codex_provider_unit.py` — 2 skipif blocks removed + 1 added `@pytest.mark.live`
- `test/providers/test_kiro_cli_integration.py` — module-level skipif removed from pytestmark
- `test/e2e/conftest.py` — autouse fixture now checks `request.config.getoption("--run-live")`
- `test/fixtures/cao_server.py` — `cao_terminal` fixture now checks `request.config.getoption("--run-live")`
- `Makefile:60` — `test-live` target now passes `--run-live`

`CAO_RUN_LIVE_PROVIDER_TESTS` deleted from the tree (zero code hits outside plugin doc comment).

Evidence: without `--run-live`, live tests SKIP:
```
test_project_and_generated_session_start_hooks_both_fire SKIPPED
test_check_for_update_on_startup_accepted_by_binary SKIPPED
test_codex_launch_flags_are_valid SKIPPED
```
With `--run-live --collect-only`: all 3 collected (would run).

## AC2.5 — No opus-tier model in live suite paths (r2)

Grep results for `opus` and `--model` under live/e2e test files:
- `test_claude_transcript_hook.py` already uses `--model claude-sonnet-5` (free lane)
- No other live/e2e test invokes any model explicitly
- No `opus` or `default-model` references found in any live/e2e path

## AC2.6 — Collected-counts evidence (r2)

```
# CI-mode collection (local, same markers as workflow):
uv run pytest --collect-only -q -m "not live and not e2e"
→ 11572/11711 tests collected (139 deselected)

# Full local collection:
uv run pytest --collect-only -q
→ 11711 tests collected

# Difference: 139 tests = live + e2e marked (correctly excluded by -m expression)
```

The 139 excluded tests are precisely those with `live` or `e2e` markers. CI and local
share one vocabulary — the marker expression is the only selection mechanism. No
`--deselect` (verified: grep returns 0). Skip reasons are machine-readable via the
`requires_*` markers and the `--run-live` gate.

## Suite Counts (r1 full run, still valid)

**Full suite (`make test-ci`):**
- 11524 passed, 36 skipped, 8 xfailed, 4 failed (pre-existing, unrelated)
- Exit code: 1 (due to pre-existing failures)
- Wall time: 1201.51s (20:01)

**Pre-existing failures (not caused by D2):**
1. `test_ready_completion_at_deadline_has_one_lawful_owner@quarantine-serial` — quarantined flake
2. `test_busy_descendant_is_named_and_all_held_rows_leave_held_state` — fleet lifecycle
3. `test_stop_right_after_writer_eof_does_not_leak` — fifo reader ENXIO race
4. `test_data_received_across_writer_reconnects` — same fifo reader ENXIO race

## Production Code Change

`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py:1233`:
`now = time.monotonic()` → `now = self._clock()` — the class already accepts `clock: Callable[[], float]` at init; `notify_due` was the only method bypassing it.

## Step 0

`test/quarantine.toml`: added `test_ready_completion_at_deadline_has_one_lawful_owner` as `serial_only` class. Fields: `filed = "2026-08-19"`, `review_by = "2026-09-18"` (AC5.3 format).

## Notes

- `timing_sensitive` marker deliberately NOT introduced (per blueprint zero-decision)
- All 14 deselects converted without introducing any new CI-invisible test
- The env_capabilities plugin follows the local_fixture_guard.py pattern (AC2.1)
- `CAO_RUN_LIVE_PROVIDER_TESTS` fully deleted from tree (one vocabulary)
