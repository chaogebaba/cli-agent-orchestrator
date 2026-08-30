# F582 D14 / F521 D15 build report

## Authority and result

- Frozen authority: `/home/chao/VScode_projects/cli-subagents/orchestrator/blueprints/wp-status-truth.md`.
- Required SHA-256 verified before implementation: `2c6eaf8659554fa28b2bdbed3bc858b617819248b07f336a84a463d54cc4597d`.
- Base: `main@6f6ae605`.
- Branch: `cao/f582-d14d15`.
- Pre-report implementation tip: `80cde2ab`.
- Result: D14 provider-local abort truth, retry routing, and D15 drop/periodic pane-tail resync are implemented. Every requested AC arm is covered; all 16 recorded mutants were killed.

## Commits

- `7262264c` — WIP F582 D14/D15: codex partial build (preserved at codex usage cap 2026-08-30T03:37Z)
- `99c06072` — F582/F521 complete D14 abort truth and D15 resync
- `32937f1f` — Fix F582 over-cap test import
- `80cde2ab` — Strengthen dispatcher-crash mutant arm
- The final report commit is the branch tip recorded by the completion callback.

The supervisor-created WIP preservation commit was audited before completion. A second-lane synthetic reconstruction test was removed; only blueprint-authorized implementation and acceptance coverage remain.

## Files touched

Production:

- `src/cli_agent_orchestrator/providers/cline_cli.py`
- `src/cli_agent_orchestrator/services/config_service.py`
- `src/cli_agent_orchestrator/services/event_bus.py`
- `src/cli_agent_orchestrator/services/pane_liveness.py`
- `src/cli_agent_orchestrator/services/stalled_callback_watchdog.py`
- `src/cli_agent_orchestrator/services/status_monitor.py`

Tests and fixtures:

- `test/providers/fixtures/status_truth/cline_cli/abort-1.json`
- `test/providers/fixtures/status_truth/cline_cli/abort-1.txt`
- `test/providers/fixtures/status_truth/cline_cli/abort-2.json`
- `test/providers/fixtures/status_truth/cline_cli/abort-2.txt`
- `test/providers/test_cline_f343_busy_gate.py`
- `test/providers/test_cline_f582_abort_truth.py`
- `test/services/test_f516_d4.py`
- `test/services/test_f521_status_resync.py`
- `test/services/test_pane_liveness.py`
- `test/services/test_status_monitor_f582_abort_retry.py`

Implementation diff against `6f6ae605`: **16 files changed, 1003 insertions, 30 deletions**. Final branch diff including this 153-line report: **17 files changed, 1156 insertions, 30 deletions**.

## Behavioral implementation

- D14 counts stripped `ABORT_LINE` occurrences only while the dispatcher is idle and a run is dispatched. It rejects pane tails at or below the 45-line authority floor, reports only a new occurrence edge, holds `ERROR` for a strict two-second window, explicitly rearms a two-second retry on every held evaluation, and closes to `IDLE`, never `COMPLETED`.
- `notify_status_buffer_reset()` resets only the three provider-local abort epoch fields.
- Retry routing chooses the screen callback only for pyte-capable providers; raw providers use `_on_raw_quiescent(terminal_id, chunk_seq)`.
- D15 records a monotonic per-terminal drop sequence independently of the bounded drop-level map, retains the last filtered pane tail in pane liveness, and forces a provider pass on that retained tail after a drop or on the processing periodic backstop. Recovered publishes carry `fusion_reason=resync_after_drop`.
- Watchdog resync uses `pane_liveness.peek()` after the existing observation and performs no second pane capture.

## Targeted test evidence

All Python dependency setup, pytest, Black, isort, and mypy work ran on `grok-box-002`; none ran on the laptop.

Authorized pytest scope:

```text
uv run pytest -q test/providers/test_cline*.py test/services/test_status_monitor*.py test/services/test_f516*.py test/services/test_f521_status_resync.py test/services/test_pane_liveness.py
```

- Base `6f6ae605`: **187 passed, 2 failed** in 6.76s.
- Head `80cde2ab`: **217 passed, 2 failed** in 10.18s.
- Delta: **+30 passed, no new failures**.
- The same two pre-existing F516 failures occur at base and head:
  - `test/services/test_f516_d2.py::test_d2_fast_path_waiting_fires_on_first_eval`
  - `test/services/test_f516_fixtures.py::test_chooser_fixtures_render_the_resume_cwd_dialog_in_region`
- Black `--check --line-length 100`: pass, 12 touched Python files unchanged.
- isort `--check-only --line-length 100`: pass.

## Mypy strict counts

Command at base and head:

```text
uv run mypy --strict src/cli_agent_orchestrator/providers/cline_cli.py src/cli_agent_orchestrator/services/config_service.py src/cli_agent_orchestrator/services/event_bus.py src/cli_agent_orchestrator/services/pane_liveness.py src/cli_agent_orchestrator/services/stalled_callback_watchdog.py src/cli_agent_orchestrator/services/status_monitor.py
```

- Base `6f6ae605`: **102 errors in 4 files**.
- Head `80cde2ab`: **102 errors in 4 files**.
- Delta: **0 errors, 0 files**.

## Mutation ledger

Every row used a detached `80cde2ab` box worktree. The command column is the discriminating selector run after the exact listed edit. Every selector collected, exited nonzero, and the source was restored with `git restore -- <file>` followed by a clean-diff assertion before the next row.

| Mutant | Exact applied edit | Command after `uv run pytest -q` | Failing excerpt |
|---|---|---|---|
| `presence_not_edge` | `occurrences > self._abort_reported_occ` → `occurrences > 0` | `test/providers/test_cline_f582_abort_truth.py::test_abort_2_replay_reports_once_then_closes_idle_never_completed` | expected closed `IDLE`; got `ERROR` |
| `closed_returns_completed` | post-hold `return TerminalStatus.IDLE` → `return TerminalStatus.COMPLETED` | same replay selector | expected `IDLE`; got `COMPLETED` |
| `report_clears_dispatch_flag` | inserted `self._task_dispatched_flag = False` on the abort report edge | `test/providers/test_cline_f582_abort_truth.py::test_level_consumer_sees_held_error_after_first_detection_is_discarded` | expected held error outcome; got completion path |
| `reset_hook_noop` | replaced the three `notify_status_buffer_reset()` field resets with a no-op return | `test/providers/test_cline_f582_abort_truth.py::test_new_epoch_reports_a_fresh_abort_again` | expected fresh `ERROR`; got `COMPLETED` |
| `drop_line_floor` | `non_authoritative = len(lines) <= PANE_LIVENESS_TAIL_LINES` → `non_authoritative = False` | `test/providers/test_cline_f582_abort_truth.py::test_abort_1_fixture_is_the_accepted_sub_floor_residual` | expected accepted residual `COMPLETED`; got `ERROR` |
| `retry_raw_screen_args` | added `provider` to raw `_arm_quiesce_timer` callback args | `test/services/test_f516_d4.py::test_retry_routes_raw_provider_to_raw_callback` | expected `('term1', 0)`; got provider-bearing tuple |
| `retry_raw_uses_screen` | screen-route condition → `if True` | same F516 selector | expected `_on_raw_quiescent`; got `_on_screen_quiescent` |
| `retry_uses_backoff` | removed explicit `delay_s=_ABORT_REPORT_HOLD_S` | `test/providers/test_cline_f582_abort_truth.py::test_abort_report_is_an_occurrence_edge_then_hold_then_idle` | mock expected `delay_s=2.0`; kwargs were empty |
| `retry_once_per_run_gate` | gated scheduling with `not self._abort_retry_armed` | `test/services/test_status_monitor_f582_abort_retry.py::test_chunk_slot_theft_still_rearms_until_idle_and_delivery` | `TimeoutError`; stolen slot was not rearmed |
| `retry_inside_provider_lock` | acquired `_flush_lock` immediately before scheduling | `test/providers/test_cline_f582_abort_truth.py::test_retry_is_a_leaf_call_with_provider_flush_lock_unheld` | expected lock unheld `[False]`; observed held `[True]` |
| `abort_rule_every_command` | `if current_cmd == DISPATCHER_IDLE_CMD` → `if True` | `test/providers/test_cline_f582_abort_truth.py::test_dispatcher_crash_remains_error_even_with_visible_abort` | expected dispatcher-crash `ERROR`; got abort close behavior |
| `no_drop_seq_increment` | drop update `old + 1` → `old` | `test/services/test_f521_status_resync.py::test_full_output_queue_drop_recovers_the_lost_falling_edge` | expected drop sequence `1`; got `0` |
| `peek_drops_tail` | `filtered_tail=state.filtered_tail` → `filtered_tail=""` | `test/services/test_pane_liveness.py::test_peek_returns_the_retained_filtered_tail_without_capturing` | expected `retained pane tail`; got empty string |
| `no_periodic_backstop` | `periodic = (` → `periodic = False and (` | `test/services/test_f521_status_resync.py::test_processing_backstop_runs_once_per_interval_without_a_drop` | expected `True`; got `False` |
| `no_tick_resync` | watchdog `resync_from_pane_tail(...)` → `get_raw_status(...)` | `test/services/test_f521_status_resync.py::test_no_usable_pane_sample_means_no_forced_detection` | `ValueError: substring not found` in source-route assertion |
| `resync_uses_rolling_buffer` | `provider.get_status(filtered_tail)` → `provider.get_status(self.get_buffer(terminal_id))` | `test/services/test_f521_status_resync.py::test_drop_forces_pane_tail_redetect_and_publishes_ready_with_audit_reason` | expected provider call with `pane says done`; call not found |

Restored source SHA-256 values at `80cde2ab`:

- `cline_cli.py`: `ebf673eb52221100d1bf80a91189ab52d47d68ff43406111e8f5f80e3477c200`
- `status_monitor.py`: `bd8e7dca9494696550ae1e6bd6ca3a01bacdbc00da4d0b35443846010de72a04`
- `event_bus.py`: `f1531c1748edb46c0441e96aad27e9cde851ccac145cc10c39ed7a02bb2f06d2`
- `pane_liveness.py`: `ece715a95171b75385a6345872eeaa5ec7e22fe07ce798bcc1806f0ff1f74f43`
- `stalled_callback_watchdog.py`: `debbf8c1f724d3895a94378152a761bc885e57e9c4861276bf9fdf53700f3dd7`

## Fixture provenance and deviations

AC1 replay fixture abort-1 is supervisor-authored (real 165daae4 pane bytes + hand-inserted ABORT_LINE); the AC's 'byte-exact capture of adcaa8a8' premise was unrealized — live repro attempts listed in abort-1.json.

- `abort-2` is likewise supervisor-authored as stated in its JSON `source`: real pane bytes with a hand-inserted `ABORT_LINE`; it supplies the authoritative greater-than-45-line replay arm.
- Both fixture pairs were copied byte-for-byte from `/data/cao-scratch/9064394e/fixtures/` and were not edited.
- Fixture hashes:
  - `abort-1.txt`: `2892ff8a9bb2af44436178542ca10b1797cd2c6c0c025768e49a5a3e60217984`
  - `abort-1.json`: `d9e9adbb40679e36451b3ba1fa3008e9cf24407c520c13eed20fc9e28231191e`
  - `abort-2.txt`: `f616d80b8899c1d404ba81fef417d0d762d421b63b970348c3ede448a7bd3daf`
  - `abort-2.json`: `2b377bc8ff30a1c8ab36996927ed7233d700948c82521af63b18df89a2eccd74`
- The frozen blueprint's cited D14/D15 source seams matched the `6f6ae605` tree materially; no design-around was needed.
- During the read-only `f582-mutant-summary` ledger collection, a final status probe targeted the already-cleaned `/data/cao-scratch/f582-mutation` worktree and printed `fatal: cannot change to ...`; all mutation logs had already been collected, and the subsequent cleanup verified no F582 worktree remained.

## Box-actions ledger

All invocations used `CAO_BOXES=box@grok-box-002 bash scripts/box-run.sh <label> -- '<command>'` from `/home/chao/VScode_projects/cli-subagents`. No raw SSH was used.

| Label | Box command/action | Result |
|---|---|---|
| `f582-head-1` | fetch pushed branch; detached head worktree; `uv sync --frozen`; authorized targeted pytest | 216 passed, 3 failed; identified missing test import plus the two base failures |
| `f582-head-2` | fetch `32937f1f`; detached head worktree; sync; same targeted pytest | 217 passed, 2 base failures |
| `f582-base-1` | detached `6f6ae605` worktree; sync; corresponding base targeted pytest | 187 passed, same 2 failures |
| `f582-head-type` | detached head; sync; Black/isort checks; strict mypy on six touched production files | format pass; 102 mypy errors in 4 files |
| `f582-base-type` | detached base; sync; same strict mypy file set | 102 mypy errors in 4 files |
| `f582-mutations` | fetch `80cde2ab`; detached worktree; sync; baseline 14-selector run; sequential exact mutant edits, selectors, restores, clean assertions; remove worktree | baseline 14 passed; all 16 mutants killed; `MUTATION_LEDGER_COMPLETE` |
| `f582-mutant-summary` | read-only summaries of captured mutant logs and a status probe | all 16 red excerpts collected; final probe found the mutation worktree already removed |
| `f582-final-head` | fetch `80cde2ab`; detached worktree; sync; final authorized pytest; Black/isort; remove worktree | 217 passed, same 2 base failures; formatting pass |
| `f582-box-cleanup` | remove any remaining named F582 worktrees; prune; delete only `/data/cao-scratch/f582-*` logs; verify | no F582 worktrees or temp files remain |

Box final state:

- Persistent fork checkout: `2fd3d25c05b17bf1b3d3067fe39e895f7689ec57`, clean.
- Environment mutations: only disposable per-worktree `.venv` installations from `uv sync --frozen`; all removed with their worktrees. No apt/pip/global installs or lockfile changes.
- Temporary files left: none with the F582 prefix.
- Box-rule deviations: none; all state changes and compute ran under the slot lock.
