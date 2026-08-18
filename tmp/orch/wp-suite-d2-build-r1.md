# WP-SUITE D2 Build Report — r1

**Branch:** `cao/473b1670`  
**HEAD:** `70774455987a3a61cf3fc668ae53982b4cf09f9b`  
**Base:** `20878cab` (Merge 'cao/f262' into f254-phase3-enforcement)  
**Commits:** 2

## Commits

1. `3ff96154` — quarantine: re-quarantine test_ready_completion_at_deadline as serial_only
2. `70774455` — D2: unify skip vocabulary — markers + env detection, strip all deselects

## Diff stat (20878cab..HEAD)

```
 .github/workflows/test-ci.yml                      | 14 ----
 pyproject.toml                                     |  5 ++
 src/.../stalled_callback_watchdog.py               |  2 +-
 test/conftest.py                                   |  1 +
 test/plugins/env_capabilities.py                   | 94 ++++++++++++++++++++++
 test/quarantine.toml                               | 10 +++
 test/services/test_f93_terminal_identity_resolution.py |  1 +
 test/services/test_fifo_reader.py                  |  9 ++-
 test/services/test_fx193_nudge_discipline.py       |  4 +-
 test/services/test_stalled_callback_watchdog.py    |  6 +-
 test/services/test_wave3b_supervisor_mailbox.py    |  1 +
 test/test_f139_fixture_provider.py                 |  1 +
 test/test_g7a_sandbox.py                           | 13 ++-
 test/utils/test_persona_context.py                 |  6 ++
 14 files changed, 141 insertions(+), 26 deletions(-)
```

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

## Suite Counts

**Full suite (`make test-ci`):**
- 11524 passed, 36 skipped, 8 xfailed, 4 failed (pre-existing, unrelated)
- Exit code: 1 (due to pre-existing failures)
- Wall time: 1201.51s (20:01)

**Pre-existing failures (not caused by D2):**
1. `test_ready_completion_at_deadline_has_one_lawful_owner@quarantine-serial` — the test quarantined in step 0; serial_only class still runs (expected flake)
2. `test_busy_descendant_is_named_and_all_held_rows_leave_held_state` — fleet lifecycle assertion on HELD rows
3. `test_stop_right_after_writer_eof_does_not_leak` — fifo reader ENXIO race (different class from D2's fix)
4. `test_data_received_across_writer_reconnects` — same fifo reader ENXIO race

## Production Code Change

`src/cli_agent_orchestrator/services/stalled_callback_watchdog.py:1233`:
`now = time.monotonic()` → `now = self._clock()` — the class already accepts `clock: Callable[[], float]` at init; `notify_due` was the only method bypassing it.

## Step 0

`test/quarantine.toml`: added `test_ready_completion_at_deadline_has_one_lawful_owner` as `serial_only` class (F262 verdict (d), re-quarantine trigger: any failure under -n 2). Precondition confirmed: `_VALID_CLASSES` at quarantine.py:42 contains `serial_only` post-F262 merge (20878cab).

## Notes

- `timing_sensitive` marker deliberately NOT introduced (per blueprint zero-decision)
- All 14 deselects converted without introducing any new CI-invisible test
- The env_capabilities plugin follows the local_fixture_guard.py pattern (AC2.1)
