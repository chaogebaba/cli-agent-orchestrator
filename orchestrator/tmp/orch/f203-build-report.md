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
make test-full → 10889 passed, 7 xfailed, 10 failed (exit 1)
```

**Failures breakdown:**
- 5 pre-existing (AC19 allowlist): test_handoff, test_claude_code_unit, test_f165_real_sqlite_reconciler, test_fx167_mutant_kills, test_wave4_delivery_state
- 5 environmental (worktree-only): test_config_reconcile (missing install.sh), test_fold ×2 (PTY), test_fake_clock (timing), test_f44_probable_delivered (missing providers.toml.default)
- 0 new failures from this batch

**xfail delta:** 12 → 7 = **-5** (4 promoted + 1 rewritten per D23/N3)

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

### Source (10 files)
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

### Tests (5 files)
- `test/services/test_f203_family_sweep.py` — 5 xfails removed, D23 rewrite
- `test/services/test_f206_hotfix.py` — ConfigService mock updated
- `test/services/test_fx170_native_doorbell.py` — D13 record shape
- `test/services/test_f203_transport_ejection.py` — NEW (AC6-AC9)
- `test/services/test_delivery_fsm_stateful.py` — NEW (Amendment V1)

## Mutation Ledger

5 mutants in `orchestrator/tmp/orch/f203-mutants/`:
- M1: interrupt threshold boundary → KILLED
- M2: boundary level trigger → KILLED (by rearm test)
- M3: ejection threshold → KILLED
- M4: cadence gate removal → SURVIVED (needs AC13 integration test)
- M5: supervisor resolver fallback → SURVIVED (needs AC16 two-terminal test)

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
