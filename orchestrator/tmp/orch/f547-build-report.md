# F547 (#403) build report — duplicate supervisor doorbell pushes

- **Artifact-Path:** `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/93cb6ea9`
- **Git-SHA-fork:** `3dae9ed8` (branch `cao/93cb6ea9`, base `b7a1f9d5`; fix commit `adb3f9ab` + live-evidence test `3dae9ed8`)

## Root cause (from #403, not re-derived)
`delivery_service.convergence_tick` (tick_s=5s) → `_drive_one_obligation` → `attempt_rung1`
→ `doorbell_service._attempt_native_ring`. The obligation re-armed `next_attempt_at = now+tick_s`
on EVERY attempt including after a delivered rung1, each ring built a fresh `uuid4` `msg_id`
(so no consumer could dedupe), rung1 success never recorded `socket_delivered` nor consulted
`WAITING_USER_ANSWER`, and the Claude client's previous-only "identical to previous from this
sender" drop was defeated by one interleaved message.

## Contract implementation
| Pt | Change | File |
|----|--------|------|
| 1 | `build_wake_msg_id(receiver,row,incarnation)` sha256, uuid-shaped; `build_wake_payload` takes `incarnation`; doorbell threads `incarnation=record.proc_start` | `cc_session_registry.py`, `doorbell_service.py` |
| 2 | Escalating backoff after delivered rung1 (60s→300s→1800s cap) via `_rung1_backoff_seconds`; re-ring body `_rung1_repush_body` carries re-push N + unacked Xm + pending ids; legacy text only on first ring (delivered_count 0). Config `delivery.rung1_backoff_s` default `[60,300,1800]` | `delivery_service.py` |
| 3 | `_receiver_hold_reason` — `status_monitor.get_status`==WAITING_USER_ANSWER, else pane-tail markers `Compacting`/`Retrying` (last 2000 chars of `get_buffer`); `_drive_one_obligation` skips ring + keeps obligation while held; consolidated ring lists all pending row ids for the mailbox on unblock. Config `delivery.hold_pane_markers` | `delivery_service.py` |
| 4 | `attempt_rung1` records `f459.socket_delivered` on `rang` (reuses `doorbell_service._mark_socket_delivered`) | `delivery_service.py` |
| 5 | CAO-side per-sender content-hash window (deque, default 20) in `write_to_socket`; dup within window → `None` (idempotent no-op, no re-emit, no fx168 fallback). Config `supervisor.wake.dedupe_window`. **Deviation:** the literal "Dropped a peer message…" component is inside the Claude Code CLI client (not in this repo or root scripts/hooks); implemented the equivalent at CAO's sole bridge write sink. | `cc_session_registry.py` |

Config keys registered in `config_service.py` (`CAO_SUPERVISOR_WAKE_DEDUPE_WINDOW` env + `_ALL_PATHS`).

## Tests (each test carries a mutation note in its docstring)

### `test/services/test_f547_wake_dedupe.py` (13)
- `test_msgid_same_row_same_incarnation_is_stable` — mut: restore `str(uuid.uuid4())` msg_id.
- `test_msgid_different_incarnation_differs` — mut: drop `incarnation` from seed.
- `test_msgid_different_row_differs` — mut: drop `inbox_row_id` from seed.
- `test_msgid_is_uuid_shaped` — mut: return raw 64-char digest (no 8-4-4-4-12 reshape).
- `test_payload_uses_deterministic_msgid` — mut: restore uuid4 msg_id in build_wake_payload.
- `test_dedupe_drops_byte_identical_within_window` — mut: make `_is_duplicate_in_window` always False.
- `test_dedupe_survives_interleaved_other_sender` — mut: previous-only (single last-hash) dedupe.
- `test_dedupe_window_evicts_beyond_20` — mut: unbounded deque (no maxlen).
- `test_dedupe_is_per_sender` — mut: key window on content only.
- `test_dedupe_failopen_on_unparseable_payload` — mut: raise instead of (None,None) on bad JSON.
- `test_write_to_socket_dupe_returns_none_no_connect` — mut: return error string from dedupe branch.
- `test_native_ring_threads_incarnation_from_record` — mut: drop `incarnation=` in doorbell build_wake_payload call.
- `test_live_evidence_one_write_per_sender_row_within_60s` — mut: delete the `_is_duplicate_in_window` guard (id 1616 x3 all write) / reuse legacy re-ring text. Reproduces the #403 screenshot (1616 x3, 1617 x2 interleaved → one write each).

### `test/services/test_f547_rung1_repush_discipline.py` (11)
- `test_backoff_ladder_steps_and_caps` — mut: `_rung1_backoff_seconds` → `return tick_s`.
- `test_repush_body_carries_attempt_and_age_and_ids` — mut: revert body to legacy first-ring text.
- `test_delivered_rung1_reschedules_on_backoff_not_tick` — mut: restore `next_attempt_at=now+tick_s`.
- `test_first_ring_uses_legacy_text_repush_uses_count` — mut: drop `if delivered_count > 0:` guard.
- `test_hold_waiting_user_answer_skips_ring_keeps_obligation` — mut: delete hold early-return block.
- `test_receiver_hold_reason_waiting` — mut: compare status to IDLE instead of WAITING_USER_ANSWER.
- `test_receiver_hold_reason_pane_marker` — mut: remove pane-tail marker loop.
- `test_receiver_hold_reason_none_when_idle` — mut: invert WAITING comparison to `!=`.
- `test_consolidated_pending_ids_on_repush` — mut: `_pending_row_ids_for_mailbox` returns only obl row.
- `test_rung1_rang_records_socket_delivered` — mut: delete `_mark_socket_delivered` in rang branch.
- `test_rung1_non_rang_records_no_socket_delivered` — mut: move mark outside the `== "rang"` branch.

### Existing-test touch-ups (call-shape only)
- `test_delivery_service.py::test_attempt_rung1_rang_is_delivered` and
  `test_f457_r2_gate_fixes.py` — updated to `_attempt_native_ring(..., message_body=None)`.
- `test_fx170_native_doorbell.py` — added autouse `_reset_wake_dedupe_window` fixture (module-global window hygiene).

## Verification (targeted — no full suite on laptop, box suite runs later)
- `test_f547_wake_dedupe.py` (13) + `test_f547_rung1_repush_discipline.py` (11): **24 passed**.
- delivery/doorbell-adjacent (`test_delivery_service`, `test_fx170_native_doorbell`,
  `test_fx191_convergent_delivery`, `test_f457_r2_gate_fixes`, `test_f206_hotfix`,
  `test_f203_family_sweep`, `test_wpdt_delivery_truth`): **184 passed, 5 skipped**.
- config: **68 passed**. f178/teammate/f136: **98 passed, 1 xfailed**.
- No LSP/ruff available in env; syntax verified via `ast.parse`, behaviour via pytest.
