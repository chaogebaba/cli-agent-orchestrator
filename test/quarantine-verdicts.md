# Quarantine Verdicts Ledger (F262)

Append-only. One `##` section per entry that leaves the registry.
Format: nodeid heading, bucket tag, root cause, arm counts, fix commit or retirement date.

---

## test_emits_invoke_agent_with_required_attributes

- **Bucket:** (b) test-fixture bug
- **Root cause:** Process-global OTel `TracerProvider` + shared `InMemorySpanExporter` at `test/telemetry/conftest.py:31-39`. Under `-n 2`, non-quarantined OTel tests on the same worker add spans to the exporter, breaking `len(finished) == 1` assertions.
- **Arm counts:** A 3/3 green (serial passes), B 20/20 green (file-only), C identified co-schedulers in `test/telemetry/`
- **Fix:** Serialize via `serial_only` (process-global OTel provider is un-isolatable per `conftest.py:3-5`)
- **Date:** 2026-08-18

## test_emits_execute_tool

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same as `test_emits_invoke_agent_with_required_attributes` — shared `InMemorySpanExporter`.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_emits_full_exec_span_on_successful_result

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1).
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_real_span_export_to_memory_exporter

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1).
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_emits_chat_with_request_model

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1) — process-global TracerProvider.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_chat_span_sets_conversation_id

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1) — process-global TracerProvider.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_expected_audience_defaults_to_api_base_url_when_enabled

- **Bucket:** (b) test-fixture bug
- **Root cause:** Cross-worker `monkeypatch` of module-level auth constants (`test/security/test_auth.py`). Under xdist, two workers mutate `auth._audience` concurrently.
- **Arm counts:** A 3/3 green, B 20/20 green (file-only serialization resolves)
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_audience_fallback_enforced_in_validation

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same as `test_expected_audience_defaults_to_api_base_url_when_enabled` — cross-worker auth constant mutation.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18



## test_data_received_across_writer_reconnects

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized FIFO timing under xdist load; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20 (test/services/), D 5/5
- **Commands:** `CAO_TEST_QUARANTINE=off TCACHE=off uv run pytest test/services/test_fifo_reader.py -n 2`
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure of this nodeid under `-n 2` in CI or gate runs.

## test_stop_right_after_writer_eof_does_not_leak

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized FIFO timing; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_dispatcher_uses_slot_grant_not_delayed_validator_entry

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized timing (0.01s deadline vs 0.03s sleep); not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_quiesce_wins_after_ready_sync_call_starts

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized timing (0.02s timeout); not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_ac13_no_surviving_ancestor_cancels_with_reason

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized shared lease state; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_ac13_held_row_target_exists_after_delete

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized shared lease state; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_uncertain_kill_stops_keeps_row_and_releases_quarantine_exit_lease

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized kill-rollback race; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_project_and_generated_session_start_hooks_both_fire

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized fixed-filename collision (terminal_id="hookterm"); not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20 (test/providers/), D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_project_and_two_generated_hooks_are_additive_and_failure_isolated

- **Bucket:** (d) irreproducible
- **Root cause:** Same transcript hook cluster; not reproduced for param [0] or [1].
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_ac13_raw_byte_decode_rejections

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized subprocess timeout under load; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20 (test/cli/), D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_backend_failure_warning_is_rate_limited_per_terminal

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized module-level state; not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_ready_completion_at_deadline_has_one_lawful_owner

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized timing (0.010s timeout vs threading.Timer); not reproduced.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_safety_gate_obligations_escalate_within_bound

- **Bucket:** (d) irreproducible
- **Root cause:** Hypothesized convergence ordering; not reproduced for [waiting_user_answer] param.
- **Arm counts:** A 3/3, B 20/20, C 20/20, D 5/5
- **Retirement date:** 2026-08-18
- **Re-quarantine trigger:** Any failure under `-n 2`.


## test_server_shut_down_under_us_is_a_confirmed_absence

- **Bucket:** (b) xdist resource contention
- **Root cause:** Real tmux kill-server is async; under xdist CPU contention the 15s poll may miss the server-down transition.
- **Arm counts:** isolated 5/5 pass, full -n 2 flake observed 2026-08-26
- **Retirement date:** 2026-08-26
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_stale_entry_not_killed

- **Bucket:** (c) global state corruption
- **Root cause:** Test manipulates global suite_slot._ledger and _armed_pgid; concurrent xdist workers sharing the process can corrupt these.
- **Arm counts:** isolated 5/5 pass, full -n 2 flake observed 2026-08-26
- **Retirement date:** 2026-08-26
- **Re-quarantine trigger:** Any failure under `-n 2`.

## test_data_received_across_writer_reconnects

- **Bucket:** (b) xdist resource contention
- **Root cause:** FIFO O_WRONLY open with 3s deadline fails under heavy xdist CPU contention (reader thread scheduling latency).
- **Arm counts:** isolated 5/5 pass, full -n 2 flake observed 2026-08-26
- **Retirement date:** 2026-08-26
- **Re-quarantine trigger:** Any failure under `-n 2`.
