# F435 (#290, P1) — round 2 (patch round, gate REVISE → addressed)

**Branch:** `cao/81b6cd7e`
**Head SHA:** `723c02be48e767c3a5fb616e4e1eaa6be51b4219` (rebased onto gate artifact `455552f3`)
**Base:** `2f7f1536`, clean lane worktree `.cao/worktrees/81b6cd7e`
**Prior round:** `4e5c1b0e` (REVISE, 3 BLOCKER / 1 SHOULD)

## Per-blocker fix map

### B1 (safety) — composer-scoped chip detection
- **Was:** `_pane_shows_pasted_chip` grepped `CODEX_PASTE_CHIP_PATTERN` over the whole
  200-row capture. A historical chip in scrollback + an empty/Working current composer
  returned stuck → blind extra Enter → double-submit.
- **Now:** detection is scoped to the ACTIVE composer. It locates the TUI footer
  (`_find_tui_footer_index`) and the active composer row via the same strict
  footer-adjacency the status/extraction paths use (`_find_composer_anchor_index` —
  reachable from the footer through only blank / "? for shortcuts" rows), then matches
  the chip ONLY in that region. A `• Working` spinner or any content between footer and a
  prompt row rejects the anchor, so a historical chip is never read as the current composer.
  Deliberately does NOT inherit `read_composer_draft`'s "assistant-above ⇒ defer(None)"
  ownership rule (that guards human-draft stash; the `[Pasted Content N chars]` chip is
  unambiguous CAO chrome, and the real stuck pane always has the SEED_OK bullet above it).
- **Committed tests:** `test_historical_chip_with_empty_composer_is_not_stuck`,
  `test_historical_chip_with_working_composer_is_not_stuck`,
  `test_historical_chip_negatives_send_no_enter_through_the_hook`,
  plus `test_active_chip_matches_in_anchored_composer` (real char counts, anchored).
- **File:** `src/cli_agent_orchestrator/providers/codex.py`.

### B2 (recoverability) — coherent verification-failure transition
- **Was:** both seams called `commit_dispatch` BEFORE the verify hook; a
  `CodexSubmitStuckError` escaped after commit (commit=1, abort=0, prepared path never
  called `mark_injection_completed`) — a half-committed send, neither failed nor deferred.
- **Now:** both seams run `verify_submission_after_send` INSIDE the dispatch transaction,
  BEFORE `commit_dispatch`. A `CodexSubmitStuckError` drives `abort_dispatch` (coherent
  rollback — provider dispatch arm restored, mutex released) and is re-raised as
  `DeliveryDeferredError`, the established retry-safe "delivery unsafe, redeliver later"
  contract the inbox layer already handles (no crash, no pretend-success). On
  `send_prepared_input`, commit / `mark_injection_completed` / `on_submitted` fire ONLY on
  the success path, so a stuck send publishes no submission boundary.
- `CodexSubmitStuckError` stays a plain `Exception` in codex.py: subclassing
  `DeliveryDeferredError` there creates a
  codex→draft_guard→status_monitor→manager→codex import cycle. The seam
  (terminal_service, which already imports `DeliveryDeferredError`) owns the translation.
- **File:** `src/cli_agent_orchestrator/services/terminal_service.py` (both seams + import).

### B3 (coverage) — send-seam wiring tests
- **Was:** the 10 tests hit the provider hook directly; no shipped test asserted the hook
  is invoked from the seams, so the prepared-input disconnect mutant had no shipped guard.
- **Now:** `test/services/test_f435_send_seam_verify.py` — per seam: a wiring test asserts
  `verify_submission_after_send` is called exactly once and the ordering is
  `send_keys → verify → commit` (a verify-after-commit mutant fails); a failure-transition
  test asserts a stuck verdict aborts (not commits), raises `DeliveryDeferredError`, and —
  on the prepared seam — publishes no `mark_injection_completed` / `on_submitted`.

### S1 — pinned-sample char count
- Confirmed: the pinned `/data/cao-scratch/f435-recurrence-61ef8f3d-pane.txt` and the
  committed fixture both render `[Pasted Content 3048 chars]` (not 4,600). The committed
  fixture and my regex/positive tests use 3048. The synthetic historical-chip fixtures use
  4600 ONLY to make the scrollback chip textually distinct from the active composer — they
  are not claims about the live sample.

## Mutation kills (all RED against SHIPPED tests)
1. Disconnect `send_input` verify → `test_send_input_*` RED.
2. Disconnect `send_prepared_input` verify → `test_send_prepared_input_*` RED (the B3 mutant).
3. Verify AFTER commit (the B2 defect) → `send_input` seam tests RED (stuck raises
   `CodexSubmitStuckError` + commits instead of aborting/deferring).
4. Whole-pane grep (the B1 defect) → the 3 scrollback-negative tests RED.
Each mutation applied one at a time, then restored (compile re-verified).

## Verification
- **F433 provenance:** `cli_agent_orchestrator.__file__` resolves inside this worktree
  before every counted run.
- **In-worktree hermetic** (`env -u CAO_TERMINAL_TOKEN`): targeted F435 (provider + seam)
  **17 passed**; `test/providers/` + `test_terminal_service_full.py` + `test_draft_guard.py`
  + `test_wpq10_digest_ack_draft_defer.py` = **2050 passed, 0 failed**.
- **Full box suite** via `scripts/box-run.sh f435r2`, cursor-3, `-m "not live and not e2e"`,
  SHA `723c02be`: **13,244 passed, 1 failed, 42 skipped, 8 xfailed, 1 xpassed** in 286s.
  - The single failure is the pre-existing **F440** g7b
    `test_tmux_allows_only_manifest_pinned_blocked_plane_env` — the gate's own report
    labeled it "known F440 g7b failure"; its paths are byte-identical to base and outside
    F435. The r1 PII fixture failure is gone this round.
  - Test count rose by 8 (the new F435 tests), all green.

## box-actions ledger
- `box-run.sh f435r2 -- 'cd ~/cli-subagents/cli-agent-orchestrator && git fetch origin cao/81b6cd7e && git checkout -B cao/81b6cd7e origin/cao/81b6cd7e && git rev-parse HEAD && uv run pytest -q -m "not live and not e2e" | tee /tmp/f435r2-suite-run.txt | tail -20'`
  → box@cursor-3, checked out `723c02be`, full suite (result above).
- Raw ssh: none.
- Checkout left on cursor-3: branch `cao/81b6cd7e` @ `723c02be` (clean; no temp branches/stashes).
- Temp files on box: `/tmp/f435r2-suite-run.txt` (in /tmp).
- Env mutations: box-local `uv sync` only (no apt/global installs; no committed lockfile change).
- Deviations: (1) `git checkout -B <branch> origin/<branch>` instead of a bare-SHA checkout
  (local PreToolUse fence denies detached-SHA/`git reset`; branch tip == pushed SHA).
  (2) Rebased my r2 commit onto the gate artifact `455552f3` (remote had advanced with it);
  fast-forward-clean, no conflicts. (3) Report written in-worktree (write fence blocks
  `/data/cao-scratch/`).
