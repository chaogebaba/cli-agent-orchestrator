# F556 — send_message to a status=completed worker never delivers

Branch: `cao/1c59a744` (worktree `.cao/worktrees/1c59a744`)
Scope: root-cause + fix the delivery stall to `status=completed` workers.

## Proven root cause

A stuck **non-claude** `DELIVERING` inbox row is a hard, terminal-wide delivery
exclusion, and the periodic reconciliation heartbeat never clears it — so the
whole pending backlog behind it stalls for the life of the server.

Chain, all confirmed by reading the deployed code and by empirical tests
(`test/services/test_f556_completed_delivery_stall.py`, driven through deployed
entry points on the real-sqlite fixture):

1. **The exclusion.** `begin_delivery_attempt_if_no_other_delivering()`
   (`clients/database.py`) opens with `_delivering_authority_in_db(db, receiver)`;
   if ANY inbox row for that terminal is in `DELIVERING`, it returns
   `"delivering_conflict"` and opens no attempt. `deliver_pending()`
   (`services/inbox_service.py`) also short-circuits earlier at its pre-open
   guard `if not legacy_test_seam and list_delivering_attempts_for_terminal(...)：
   return`. Either way, a fresh `send_message` behind a stuck `DELIVERING` row
   opens **zero delivery attempts** — exactly the incident's
   `cao messages trace 1644` (only `f524.stall_surfaced`, no attempts).

2. **The missing recovery.** The only code that settles a stuck `DELIVERING`
   row for a **non-claude** provider is
   `InboxService.recover_stale_deliveries(recurring=False)` — and that only runs
   **once at process startup** (`api/main.py` startup recovery). The periodic
   reconciliation heartbeat (`inbox_reconciliation_daemon` →
   `reconcile_orphaned_messages` → `recover_stale_deliveries(recurring=True)`)
   took a branch that recovered **only** `provider == "claude_code"` attempts
   (`list_stale_open_claude_attempts`, which filters `provider == "claude_code"`).
   A `kiro_cli` (or any non-claude) stuck `DELIVERING` row was therefore never
   cleared while the server stayed up.

3. **The observed symptom.** The ready-backlog watchdog
   (`stalled_callback_watchdog.tick_ready_backlog`) correctly notices the aged
   pending row on a `COMPLETED` terminal and emits
   "status=completed … no open delivery attempt … Reconciliation remains the
   retry owner" — but the retry owner it defers to (reconciliation) had no path
   to clear a non-claude stuck row, so the row stayed `pending` forever. Kiro
   workers cycling `processing -> completed` again still hit the same
   `delivering_conflict`, so the new ready boundary changed nothing.

### Hypotheses tested

- **H1 (only IDLE is a deliverable boundary): DISPROVEN.** The event consumer
  `InboxService.run()` wakes `deliver_pending` on BOTH `IDLE` and `COMPLETED`
  status events, and the delivery admission gate admits `COMPLETED`
  (`status not in (IDLE, COMPLETED)` is the reject condition). `COMPLETED` is a
  deliverable boundary already — the block is upstream, at attempt-open.
- **H2 (F506 pane-delta fusion maps the Kiro idle prompt to a non-ready status):
  DISPROVEN as the cause.** `get_boundary_observation()` returns the fused
  status; a settled Kiro pane (green arrow + idle prompt) classifies `COMPLETED`
  and passes admission. The fresh-probe gate was not what stranded the row.
- **H3 (f524 stall-surfacing marks rows so reconciliation skips them):
  DISPROVEN.** `f524.stall_surfaced` only dedupes the one-shot sender notice and
  arms the late-delivery banner; it does not remove the row from reconciliation
  selection. The stall persisted because of the `DELIVERING` exclusion + the
  claude-only recurring recovery, not the trace stamp.

## Fix

`services/inbox_service.py` — `recover_stale_deliveries`:

- Extracted the per-message provider-agnostic recovery body (verbatim) into
  `_recover_stale_delivering_message(message, seen_attempts, *, reason_tag,
  skip_claude)`. The startup branch calls it unchanged (`skip_claude=False`,
  `reason_tag="startup_sweep"`).
- The recurring heartbeat now, AFTER the existing claude WPM2 sweep, also runs
  the SAME recovery over aged non-claude stuck `DELIVERING` rows
  (`skip_claude=True`, `reason_tag="reconcile_sweep"`). claude rows are left to
  the WPM2 sweep it already ran (no double-processing).

`clients/database.py` — `list_stale_delivering_messages(min_age_seconds=0)`:

- Added an optional age gate keyed off the NEWEST attempt's `started_at`.
  Startup passes `0` (recover everything — the owning process is gone). The
  recurring heartbeat passes `WPM2_STALE_OPEN_AGE_SECONDS` (60s) so it only
  adopts rows that have genuinely stalled and never races a healthy in-flight
  `deliver_pending` mid-confirmation. A row with no attempt row is always
  included (age unknowable → treat as stale), matching the startup branch's own
  "no attempts → DELIVERY_FAILED" handling.

Clearing the stuck row (to `DELIVERED` on a transcript hit, or back to `PENDING`
on `interrupted`/`proven_absent`) lifts the `delivering_conflict` exclusion; the
next reconcile/ready boundary delivers the backlog. This makes reconciliation a
real retry owner for `COMPLETED` receivers (fix contract #2) and delivers to a
`COMPLETED`-at-idle receiver within one heartbeat once the row is cleared
(fix contract #1).

## Tests (targeted; `test/services/test_f556_completed_delivery_stall.py`)

3 tests, all pass. Each carries a mutation note; all mutants were run and killed.

- `test_stuck_delivering_blocks_new_attempt` — PART A (the block): a stuck
  `DELIVERING` row forces `begin_delivery_attempt_if_no_other_delivering` to
  return `delivering_conflict`; fresh row stays `PENDING` (zero attempts).
  Mutant: ignore the open DELIVERING authority → opener returns `opened` → fails.
- `test_recurring_reconcile_recovers_non_claude_stuck_delivering` — PART B
  (the fix): the recurring heartbeat now settles an aged stuck `kiro_cli`
  `DELIVERING` attempt and the message returns to `PENDING`. Also asserts the
  claude-only selector never saw it (the gap the fix closes).
  Mutant: revert recurring branch to claude-only (drop the
  `list_stale_delivering_messages` sweep) → attempt stays unsettled → fails.
  (Verified: reverting produced `assert None is not None`.)
- `test_recurring_reconcile_leaves_fresh_delivering_untouched` — the age gate:
  a `DELIVERING` row whose newest attempt is younger than
  `WPM2_STALE_OPEN_AGE_SECONDS` is left untouched (still `DELIVERING`).
  Mutant: drop the `min_age_seconds` gate → fresh attempt settled → fails.
  (Verified by running the mutant.)

### Regression runs (no failures)

- `test_f556_completed_delivery_stall.py test_message_trace_inbox_matrix.py
  test_wp_watchdog_delegation.py test_wp_watchdog_production_paths.py` → 40 passed.
- `test_wpm2_delivery_soundness.py test_delivery_fixbatch_f12_f13_f14.py
  test_f524_direct_delivery_stall.py` → 126 passed.

## Quality gates

- `black --check` and `isort --check-only` on all three touched files: clean.
- `mypy` on the two touched src files: no NEW error class introduced. The one
  arg-type false-positive in the extracted helper
  (`transcript_lookup(path, …)`) is the SAME pre-existing SQLAlchemy/Union
  false-positive the original inline loop carried (moved, not added);
  `clients/database.py` has zero mypy errors in the edited region.

## Files touched

- `src/cli_agent_orchestrator/services/inbox_service.py`
- `src/cli_agent_orchestrator/clients/database.py`
- `test/services/test_f556_completed_delivery_stall.py` (new)
