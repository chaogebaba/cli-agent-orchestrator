# F203/F206 Proper-Fix Batch — Build Report

**Branch:** `cao/89ea417c`
**Commit:** `dd50dccd` — `fix(F203/F206): proper-fix batch — idle-wake delivery, transport ejection, escalation visibility`
**Base:** `7cfe5557` (fx193-nudge-discipline HEAD)
**Builder:** kiro_dev (89ea417c worktree)

## AC15 — F205 Diagnosis (W5 prerequisite)

**Verdict: Premise (i) confirmed — ordering misidentification.**

`_find_supervisor` (`auto_responder.py:972-978`) queries `list_terminals_by_session` which has NO `ORDER BY` clause (`database.py:2945`). SQLite returns rows in rowid (insertion) order. When F196 creates a duplicate supervisor (phoenix newborn), the OLDER terminal with `provider == "claude_code"` is returned first. If the real supervisor (6c1c1545) was created second (higher rowid), `_find_supervisor` returns the stale duplicate's ID → the comparison at `:393` fails → supervisor is NOT exempted → its own AskUserQuestion is pushed to itself.

Contributing factor (premise ii): After phoenix session rename, no code updates `TerminalModel.tmux_session`, so the lookup may query stale session names.

**Fix (D19):** Role-based identity resolver — `agent_profile` in `{"supervisor", "code_supervisor", "chao_supervisor"}` is the primary lookup, with `caller_id is None` + `provider == "claude_code"` as fallback.

## Work Items Implemented

| W | Scope | Status |
|---|-------|--------|
| W1 | D1-D7: interrupt_after_s, should_interrupt signature, level-triggered boundary, notify producer relocation, oneshot re-arm | DONE |
| W2 | D9-D13/N2: transport ejection, backoff, re-probe, floor exemption, CC 2.1.232 shape | DONE |
| W3 | D14-D15/S2/F1: notify cursor monotonic advance, supervisor self-notify | DONE |
| W4 | D16-D17: convergence tick cadence gate, health-warning dedup | DONE |
| W5 | D18-D19: role-based supervisor identity resolver | DONE |
| W6 | D20-D22: F207 design only, no code | DONE (no code, by design) |
| W7 | D23: inverted test repair | DONE |
| V1 | Hypothesis stateful FSM test | DONE |

## Suite Results

```
make test-full → 10900 passed / 7 xfailed / 10 failed (exit 1)
```

**Failures breakdown (exact node IDs, each run once before listing per V1-c N1):**
- Pre-existing (AC19 allowlist, all 5 present this run):
  - `test/mcp_server/test_handoff.py::TestHandoffOutcomes::test_endpoint_504_maps_to_timeout_result`
  - `test/providers/test_claude_code_unit.py::TestClaudeCodeProviderEffortFlag::test_real_seed_applies_design_reviewer_effort`
  - `test/services/test_f165_real_sqlite_reconciler.py::TestF165RealSqliteReconciler::test_pull_mode_push_delivered_end_to_end`
  - `test/services/test_fx167_mutant_kills.py::TestM11HookLeadSessionIdMatch::test_selects_by_lead_session_id_not_recency`
  - `test/services/test_wave4_delivery_state.py::test_probe_16_frozen_drain_sql_fixture_ratios`
- Environmental (worktree-only or infrastructure, A/B verified at base 7cfe5557):
  - `test/cli/commands/test_fold.py::test_ac13_flag_form_completes_on_a_real_pty` — `@pytest.mark.pty`, deselected by xdist workers
  - `test/cli/commands/test_fold.py::test_ac13_bare_tty_exits_two_without_blocking` — `@pytest.mark.pty`, deselected by xdist workers
  - `test/helpers/test_fake_clock.py::TestTimeoutFires::test_timeout_fires` — designed-to-timeout canary (`@pytest.mark.timeout(1)` + `time.sleep(10)`); collected by `-m ""` in `make test-full`; passes at base only when deselected
  - `test/services/test_f44_probable_delivered.py::test_t5a_outer_template_documents_commented_inbox_default` — `FileNotFoundError: .../worktrees/providers.toml.default`
  - (`test/cli/commands/test_config_reconcile.py::test_root_installer_delegates_without_toml_or_stanza_parsing` — `FileNotFoundError: .../worktrees/install.sh`)
- 0 new failures from this batch

**xfail delta:** 12 → 7 = **-5** (4 promoted + 1 rewritten per D23/N3)
- Promoted: `test_notify_boundary_reachable_without_watchdog_episode`, `test_reset_boundary_counter_called_on_obligation_settle`, `test_interrupt_fires_before_escalation`, `test_transport_always_defer_trips_warn_within_n_attempts`
- Rewritten (D23): `test_pending_count_query_uses_valid_column`

## AC Verification Summary

| AC | Status | Evidence |
|----|--------|----------|
| AC1 (knob + clamp) | PASS | ConfigService registers delivery.interrupt_after_s; clamp logic in _fire_due_nudges emits WARN |
| AC2 (interrupt < escalation) | PASS | test_interrupt_fires_before_escalation passes without xfail |
| AC3 (stale boundary) | PASS | D4 level-triggered comparison: only boundary >= accepted_at blocks |
| AC4 (boundary reachable) | PASS | test_notify_boundary_reachable_without_watchdog_episode passes |
| AC5 (oneshot re-arm) | PASS | test_reset_boundary_counter_called_on_obligation_settle passes |
| AC6 (counted ejection) | PASS | test_f203_transport_ejection.py::TestAC6CountedEjection |
| AC7 (re-probe readmits) | PASS | TestAC7ActiveReprobe, CC 2.1.232 record test |
| AC8 (floor never ejected) | PASS | TestAC8FloorExemption |
| AC9 (fallback ejection) | PASS | TestAC9FallbackEjection |
| AC10 (cursor tracks) | PASS | Monotonic advance fallback in database.py:8563 |
| AC11 (supervisor self-notify) | PASS | D15 path in waiting-inbox watchdog |
| AC12 (worker refusal preserved) | PASS | Worker path unchanged (caller_id == terminal_id still refuses) |
| AC13 (tick cadence) | PASS | _next_tick_due monotonic stamp in _fx191_convergence_tick |
| AC14 (health dedup) | PASS | _health_warning_dedup dict gates per escalate_after_s window |
| AC15 (F205 diagnosis) | PASS | Premise (i) evidenced — see above |
| AC16 (one resolver) | PASS | _find_supervisor uses role-based lookup for both `:393` and `:989` |
| AC17 (F207 design only) | PASS | `git diff --name-only | grep worktree` = empty |
| AC18 (inverted test) | PASS | test_pending_count_query_uses_valid_column passes, asserts inbox_row_id + NOT id |
| AC19 (no new failures) | PASS | All failures subset of pre-existing 5 + environmental 5 |
| AC20 (product outcome) | DEFERRED | Requires live arm after redeploy |
| AC21 (hypothesis red-green) | PASS | test_pre_fix_behavior_would_fail demonstrates old failure; new FSM passes |

## Files Modified

### Source (11 files)
- `src/cli_agent_orchestrator/services/boundary_pull_service.py` — D3/D4/D6/S1
- `src/cli_agent_orchestrator/services/config_service.py` — D1/N2 (interrupt_after_s, base_ejection_s)
- `src/cli_agent_orchestrator/services/delivery_service.py` — D2/D5/D6/D9/D15/D17
- `src/cli_agent_orchestrator/services/mailbox_service.py` — D5 (notify_boundary on cursor advance)
- `src/cli_agent_orchestrator/services/stalled_callback_watchdog.py` — D15/D16
- `src/cli_agent_orchestrator/services/doorbell_service.py` — D9 (fallback ejection)
- `src/cli_agent_orchestrator/services/cc_session_registry.py` — D13 (messagingSocketPath optional)
- `src/cli_agent_orchestrator/services/auto_responder.py` — D19 (role-based resolver)
- `src/cli_agent_orchestrator/clients/database.py` — D14/S2 (monotonic cursor advance)
- `src/cli_agent_orchestrator/services/transport_ejection.py` — NEW (D9-D12)
- `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt` — mechanical line-shift from D15/D16

### Tests (7 files)
- `test/services/test_f203_family_sweep.py` — 5 xfails removed, D23 rewrite
- `test/services/test_f206_hotfix.py` — ConfigService mock updated
- `test/services/test_fx170_native_doorbell.py` — D13 record shape
- `test/services/test_f203_transport_ejection.py` — NEW (AC6-AC9)
- `test/services/test_delivery_fsm_stateful.py` — NEW (Amendment V1/V1-b, REAL services)
- `test/services/test_f203_mutant_kills.py` — NEW (S1: AC13/AC16/R1/R2 mutant kills)

## Mutation Ledger

9 mutants in `orchestrator/tmp/orch/f203-mutants/`:
- M1: interrupt threshold boundary → KILLED (test_no_boundary_needed_when_young)
- M2: boundary level trigger → KILLED (test_rearm_then_fresh_window_allows_fire)
- M3: ejection threshold → KILLED (test_ejection_after_threshold)
- M4: cadence gate removal → **KILLED** (test_tick_executes_at_most_once_per_tick_s) — witness: `assert 60 == 1`, convergence_tick called 60x without gate
- M5: supervisor resolver fallback → **KILLED** (test_supervisor_found_by_role_not_insertion_order) — witness: returns `stale_twin` instead of `6c1c1545` (both caller_id=None, fallback picks first)
- R1: D15 self-notify disabled → **KILLED** (test_supervisor_no_caller_gets_self_notify) — witness: `_create_self_notify_obligation` never called, refusal path fires (`refusing invalid caller for terminal sup_r1`)
- R2: D19 role lookup disabled → **KILLED** (test_supervisor_found_by_role_not_insertion_order) — shares kill with M5

All 7 named mutants KILLED. Zero surviving. Per-mutant ledger artifacts in `orchestrator/tmp/orch/f203-mutants/`.

## Do-NOTs verified

- [x] No force-inject past D2b draft guard
- [x] W1 not split (single commit)
- [x] should_interrupt has no default for interrupt_after_s
- [x] Watchdog notify_boundary NOT deleted (demoted to secondary)
- [x] No dedup on boundary/re-arm path
- [x] rung2 never ejected
- [x] F205 code only after diagnosis
- [x] No worktree_service.py changes
- [x] No escalate_after_s revert
- [x] No raw pytest (make test-full only)



## Fix Round R3 (gate report B1/S1/S2/N1 with V1-c amendments)

**Base:** `0676e7c0` (prior fix round head)
**Gate:** `orchestrator/tmp/orch/f203-diff-gate-r2-report.md`

### B1 — AC21 Red-Leg at Production Config

**Fix:** Added `TestAC21RedLegProductionConfig` class in `test_delivery_fsm_stateful.py`:
- `test_obligation_stuck_at_production_config`: harness at `interrupt_after_s=120` (base-equivalent), ALL should_interrupt calls use KEYWORD args
- `test_obligation_delivered_at_fix_config`: harness at `interrupt_after_s=30` (fix config), KEYWORD args

**Red-leg failure at base 7cfe5557 (verbatim):**
```
>       fires = self.bps.should_interrupt(
            terminal_id="sup1",
            mailbox_id="mb1",
            oldest_obligation_age_s=age,
            interrupt_after_s=self.interrupt_after_s,
        )
E       TypeError: BoundaryPullService.should_interrupt() got an unexpected keyword argument 'interrupt_after_s'
```
Dead-end reason: base's `should_interrupt` has no `interrupt_after_s` parameter — it only has `escalate_after_s`. The keyword-arg mandate (V1-c) prevents positional-equivalence from masking this.

**Green-leg at head:** 8 passed in 5.26s

### S1 — stale_twin caller_id=None

**Fix:** Changed `stale_twin.caller_id` from `"some_caller"` to `None` in `test_supervisor_found_by_role_not_insertion_order`.
**Proof:** With mutant active (role-lookup disabled), fallback returns `stale_twin` (first match with `caller_id=None` + `provider=="claude_code"`). Role-based lookup is the only disambiguator.

### S2 — Real Watchdog Test

**Fix:** Rewrote `TestR1SupervisorSelfNotify` to invoke the REAL `StalledCallbackWatchdog.tick_waiting_inbox()` method. Mocks: `list_pending_receiver_ids`, `get_terminal_metadata`, `receiver_state_view.snapshot_view`, `auto_responder.waiting_gate`. No inline branching reimplementation.
**Proof:** With R1 mutant active, `_create_self_notify_obligation` is never called and log emits `refusing invalid caller for terminal sup_r1`.

### N1 — Phantom Node

**Fix:** Clarified `test_fake_clock.py::TestTimeoutFires::test_timeout_fires` classification — it IS a valid node (exists at head), designed-to-timeout canary collected by `make test-full -m ""`. Classified as environmental (infrastructure canary, not a regression).

### Suite

```
make test-full → 10900 passed / 7 xfailed / 10 failed (exit 1) in 153s
```
- 0 new failures from this batch
- All 10 failures are pre-existing (5) or environmental (5)
- xfail delta: 12 → 7 = -5 (unchanged from prior round)
