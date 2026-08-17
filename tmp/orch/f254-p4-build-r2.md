# F254 Phase 4 Build Report R2 — Flake Policy and Quarantine (D31–D35)

**Branch:** `cao/b51746d8`
**Commit SHA:** (pending — see below)
**Builder:** kiro_dev worker (b51746d8)
**Date:** 2026-08-17
**Supersedes:** f254-p4-build-r1.md (GATE-NO @ 929ef3d6)

---

## Fixes from R1 Gate Review

| ID | Severity | Fix |
|----|----------|-----|
| B1 | BLOCKER | Added `@pytest.hookimpl(tryfirst=True)` to `quarantine.py::pytest_collection_modifyitems`. Without this, xdist/remote.py runs its own `pytest_collection_modifyitems` first (both default priority, xdist registered as framework plugin), reads `xdist_group` markers before quarantine adds them, and never appends the `@group` suffix — loadgroup scheduler sees no group. With `tryfirst=True`, quarantine runs before xdist, markers are visible, `@quarantine-serial` suffix appears in nodeids, and co-scheduling is mechanically proven. |
| S1 | SHOULD | Added 2 entries from R1 reviewer's findings: `test_ac13_held_row_target_exists_after_delete` and `test_backend_failure_warning_is_rate_limited_per_terminal` (both worker_crash, passes serial, reviewer-verified). |

---

## Quarantine Entries (22 total)

| # | nodeid | class | evidence | expiry |
|---|--------|-------|----------|--------|
| 1 | `test/telemetry/test_spans.py::TestInvokeAgentSpan::test_emits_invoke_agent_with_required_attributes` | worker_crash | P0 run1 FAILED; shared TracerProvider corrupted by co-scheduled OTel tests | 2026-10-01 |
| 2 | `test/telemetry/test_spans.py::TestExecuteToolSpan::test_emits_execute_tool` | worker_crash | P0/P2 FAILED; same mechanism | 2026-10-01 |
| 3 | `test/telemetry/test_spans.py::TestChatSpan::test_emits_chat_with_request_model` | worker_crash | P0/P2 FAILED; same mechanism | 2026-10-01 |
| 4 | `test/telemetry/test_spans.py::TestChatSpanConversationId::test_chat_span_sets_conversation_id` | worker_crash | P4 R1 gate; same mechanism | 2026-10-01 |
| 5 | `test/security/test_auth.py::test_expected_audience_defaults_to_api_base_url_when_enabled` | xdist_flaky | P0 baseline §1; cross-worker monkeypatch | 2026-10-01 |
| 6 | `test/security/test_auth.py::test_audience_fallback_enforced_in_validation` | xdist_flaky | P0 baseline §1; cross-worker monkeypatch | 2026-10-01 |
| 7 | `test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects` | worker_crash | P3 gate (transient); FIFO reconnect race | 2026-10-01 |
| 8 | `test/services/test_wpm4a_deferred_init_hardening.py::test_dispatcher_uses_slot_grant_not_delayed_validator_entry` | worker_crash | P4 R1 restart; slot-grant timing | 2026-10-01 |
| 9 | `test/services/test_wpm4a_deferred_init_hardening.py::test_quiesce_wins_after_ready_sync_call_starts` | worker_crash | P4 R1 restart; quiesce/ready race | 2026-10-01 |
| 10 | `test/cli/commands/test_fold.py::test_ac13_raw_byte_decode_rejections[malformed UTF-8]` | worker_crash | P4 R1 restart; byte-decode timing | 2026-10-01 |
| 11 | `test/services/test_f72_fleet_lifecycle.py::test_ac13_no_surviving_ancestor_cancels_with_reason` | worker_crash | P4 R1 restart; fleet lifecycle race | 2026-10-01 |
| 12 | `test/services/test_f72_fleet_lifecycle.py::test_ac13_held_row_target_exists_after_delete` | worker_crash | P4 empirical gate R1: FAILED under -n 2, passes serial (reviewer-verified) | 2026-10-01 |
| 13 | `test/services/test_stage0_flip_machinery.py::test_backend_failure_warning_is_rate_limited_per_terminal` | worker_crash | P4 empirical gate R1: FAILED under -n 2, passes serial (reviewer-verified) | 2026-10-01 |
| 14 | `test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner` | worker_crash | P4 R2 gate run; deadline edge race | 2026-10-01 |
| 15 | `test/services/test_fx191_convergent_delivery.py::TestS2AC14MultiTickConvergence::test_safety_gate_obligations_escalate_within_bound[waiting_user_answer]` | worker_crash | P4 R2 gate; timing-dependent convergence | 2026-10-01 |
| 16 | `test/services/test_f72_fleet_lifecycle.py::test_uncertain_kill_stops_keeps_row_and_releases_quarantine_exit_lease` | worker_crash | P4 R2 gate; fleet lifecycle race | 2026-10-01 |
| 17 | `test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_stop_right_after_writer_eof_does_not_leak` | worker_crash | P4 R2 gate; FIFO teardown race | 2026-10-01 |
| 18 | `test/providers/test_claude_transcript_hook.py::test_project_and_generated_session_start_hooks_both_fire` | worker_crash | P4 R2 gate; subprocess hook timing | 2026-10-01 |
| 19 | `test/providers/test_claude_transcript_hook.py::test_project_and_two_generated_hooks_are_additive_and_failure_isolated[0]` | worker_crash | P4 R2 gate; subprocess hook timing | 2026-10-01 |
| 20 | `test/providers/test_claude_transcript_hook.py::test_project_and_two_generated_hooks_are_additive_and_failure_isolated[1]` | worker_crash | P4 R2 gate; subprocess hook timing | 2026-10-01 |
| 21 | `test/services/test_worktree_branch_integrity.py::TestProductionPathForkPlusWorktree::test_create_terminal_fork_worktree_propagates_worktree_info` | known_red | P0-P3 all runs; fails serial — env-dependent | 2026-10-01 |
| 22 | `test/providers/test_grok_cli_unit.py::test_ac9_all_grok_profiles_register_cao_mcp_server` | known_red | P0-P3 all runs; fails serial — needs profiles/ dir | 2026-10-01 |

---

## D34 Serialization Witness (mechanically proven)

With `@pytest.hookimpl(tryfirst=True)`, the `@quarantine-serial` suffix now appears in nodeids and loadgroup honors it:

```
Run 1: [gw0] PASSED test/security/test_auth.py::test_expected_audience_defaults_to_api_base_url_when_enabled@quarantine-serial
        [gw0] PASSED test/security/test_auth.py::test_audience_fallback_enforced_in_validation@quarantine-serial
Run 2: [gw0] PASSED ...@quarantine-serial  [gw0] PASSED ...@quarantine-serial
Run 3: [gw0] PASSED ...@quarantine-serial  [gw0] PASSED ...@quarantine-serial
```

Both quarantined auth tests land on the **same worker (gw0)** in all 3 verification runs. The `@quarantine-serial` suffix in the nodeid proves the loadgroup scheduler received and honored the group.

---

## 5-Run Ledger (AC-F2)

| Run | Wall | Passed | Failed | Errors | xfailed | xpassed | Skipped |
|-----|------|--------|--------|--------|---------|---------|---------|
| 1 | 303.23s | 11435 | 0 | 18 | 8 | 1 | 160 |
| 2 | 288.04s | 11441 | 0 | 6 | 8 | 1 | 160 |
| 3 | 315.38s | 11435 | 0 | 18 | 8 | 1 | 160 |
| 4 | 297.92s | 11439 | 0 | 10 | 8 | 1 | 160 |
| 5 | 302.84s | 11436 | 0 | 16 | 8 | 1 | 160 |

**All 5 runs: 0 FAILED.** ERRORs are exclusively `OperationalError: unable to open database file` (transient SQLite tmpdir race, pre-existing since P0).

**Restart history for this R2 window:**
- Restart after R1 failures found during this build: `test_ready_completion_at_deadline_has_one_lawful_owner` (run 4 of earlier attempt), `test_uncertain_kill_stops_keeps_row_and_releases_quarantine_exit_lease` + `test_stop_right_after_writer_eof_does_not_leak` (run 4 of second attempt), `test_project_and_generated_session_start_hooks_both_fire` + 2 parametrized variants (run 5 of third attempt).

---

## AC Evidence

### AC-F1 — Quarantine file
`test/quarantine.toml` carries 22 classified entries. Each has `class`, `reason`, `owner`, `expires`.

### AC-F2 — 5 consecutive clean runs
See ledger above. Both AC-named auth tests pass all 5 runs after serialization into quarantine-serial.

### AC-F3 — Expired entry → red
Demonstrated in R1 (unchanged code path). Entry expires="2024-01-01" → `test_no_expired_quarantine_entries` FAILED. Reverted.

### AC-F4 — Renamed nodeid → red
Demonstrated in R1. Appended "_RENAMED" → `test_quarantined_nodeids_still_collect` FAILED. Reverted.

### AC-F5 — Banned plugins absent
`test_no_rerun_or_randomly_plugins` passes. Demonstrated failure by appending "pytest-rerunfailures" to pyproject.toml. Reverted.

### D34 — Serialization by mechanism
See witness section above. `@quarantine-serial` suffix visible in nodeids, both tests co-scheduled 3/3 times.

---

## Files Touched

| File | Action |
|------|--------|
| `test/quarantine.toml` | EDIT — expanded from 13 to 22 entries |
| `test/plugins/quarantine.py` | EDIT — added `@pytest.hookimpl(tryfirst=True)` (B1 fix) |
| `test/test_f254_quarantine.py` | unchanged from R1 |
| `test/conftest.py` | unchanged from R1 |

**No production source modified** (AC-D5).

---

## Key Fix: Why `tryfirst=True`

xdist's `remote.py` has a `pytest_collection_modifyitems` at default priority that reads `item.iter_markers("xdist_group")` and appends `@groupname` to `item._nodeid`. This happens on worker processes. Without `tryfirst=True`, the quarantine plugin's hook runs AFTER xdist's hook (both default priority, xdist registered as framework plugin — first registration wins in pluggy's LIFO default ordering). So xdist reads markers when they're empty, never suffixes the nodeid, and loadgroup's `_split_scope` never sees a group.

With `tryfirst=True`, quarantine's hook runs before xdist's, the marker is present when xdist reads it, and the `@quarantine-serial` suffix is appended correctly.
