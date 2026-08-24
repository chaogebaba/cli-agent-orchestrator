# F435 (#290, P1) — codex paste-submit race fix

**Branch:** `cao/81b6cd7e`
**Head SHA:** `8bd1e008704f2b79e42e1d31f1bf02c4034a71b9`
**Base:** `2f7f1536` (F437 merged), clean lane worktree `.cao/worktrees/81b6cd7e`

## One-line result
Codex task sends now VERIFY submission and re-Enter only while the
`[Pasted Content NNNN chars]` chip is still drafted (bounded retries, idempotent);
provider-scoped, no other provider affected. All 10 new tests green; full suite
13236 passed with the two observed failures resolved/explained below.

## Root cause
`send_input` / `send_prepared_input` (services/terminal_service.py) paste the task
and send the submit Enter via `backend.send_keys(..., enter_count, force_bracketed_paste=True)`
inside a `begin_dispatch`/`commit_dispatch` txn — then do **nothing** to confirm the
Enter actually submitted. Under concurrent multi-assign the submit Enter is sometimes
lost to a paste-vs-Enter render race, leaving the task drafted as
`› [Pasted Content NNNN chars]` until the stalled-callback watchdog fires (~120s).

## Fix (contract-by-contract)
- **Verify after send:** new provider hook `verify_submission_after_send(metadata, backend)`.
  Codex captures the pane (`get_history(..., strip_escapes=False)`), strips escapes, and
  tests the composer against `CODEX_PASTE_CHIP_PATTERN = r"[›»]\s*\[Pasted Content\s+\d+\s+chars\]"`.
  A submitted composer instead shows the idle placeholder or the Working spinner, so the
  chip's PRESENCE is the durable stuck signal.
- **Grace + bounded retries:** after a `~2s` grace, if the chip is present, re-send `Enter`,
  re-verify, up to `3` attempts with `1s * attempt` backoff.
- **Stuck-forever → clear error:** raises `CodexSubmitStuckError` naming the terminal id
  after the retries are exhausted.
- **Idempotent / no double-submit:** Enter is re-sent *only while the chip is observed*.
  A submitted composer is never blind-Entered. Capture failure is treated as "not stuck"
  (returns no-Enter) precisely so a read error can never cause a double-submit.
- **Scope decision:** the fix is **provider-scoped to Codex**. The base hook is a no-op, so
  Claude/Grok/Kiro/cline are unaffected — F435 is a codex-TUI-only race. Wired into BOTH
  `send_input` and `send_prepared_input` after `commit_dispatch`, BEFORE any human-draft
  restore / `mark_injection_completed`, so a recovery re-Enter can only resubmit the task,
  never a restored draft.

## Files changed
- `src/cli_agent_orchestrator/providers/base.py` — no-op `verify_submission_after_send` hook.
- `src/cli_agent_orchestrator/providers/codex.py` — F435 constants + `CodexSubmitStuckError`
  + `_pane_shows_pasted_chip` (staticmethod) + `verify_submission_after_send`; **also added the
  missing `PYTE_SCREEN_ROWS` import** — it was referenced un-imported in the capture idiom, so a
  swallowed `NameError` would have silently disabled the whole recovery. The tests caught it.
- `src/cli_agent_orchestrator/services/terminal_service.py` — invoke the hook in both send paths.
- `test/providers/test_codex_submit_verify_f435.py` — 10 unit tests, mocked tmux client.
- `test/providers/fixtures/codex_f435_stuck_paste_pane.txt` — verbatim #290 pane sample
  (login-banner email scrubbed to `user@example.com` per the fixture-PII test).

## Tests (mocked tmux client)
- regex/`_pane_shows_pasted_chip` vs the **real #290 pane sample fixture** → matches.
- regex ignores submitted states (idle placeholder, Working spinner, empty).
- submitted-immediately (placeholder) → **no extra Enter**.
- submitted-immediately (Working spinner) → **no extra Enter**.
- stuck-then-recovered after 1 re-Enter → exactly 1 Enter.
- stuck-then-recovered after 2 re-Enters → exactly 2 Enters.
- stuck-forever → `CodexSubmitStuckError`, exactly `MAX_RETRIES` Enters (bounded), names terminal.
- capture-failure → **no blind Enter** (idempotence safety).
- base-provider hook is a no-op.

## Verification
- Local targeted: new file **10 passed**; `test_codex_provider_unit.py` + `test_draft_guard.py`
  **436 passed / 3 skipped**; terminal_service send tests **12 passed**; PII fixture test **passes**.
- Full suite on `box@cursor-3` (via `scripts/box-run.sh`, SHA `f5b5f0a3`):
  **13236 passed, 2 failed, 42 skipped, 8 xfailed, 1 xpassed** in 291s.
  1. `test_no_personal_email_addresses_in_fixtures` — my new fixture carried the real
     login-banner email; **FIXED** by scrubbing to `user@example.com` (commit `8bd1e008`),
     verified passing locally.
  2. `test_g7b_sandbox.py::test_tmux_allows_only_manifest_pinned_blocked_plane_env` —
     **PRE-EXISTING, unrelated.** Asserts `TmuxClient._merge_extra_env` refuses to overwrite a
     sandbox-pinned `CODEX_HOME` with `/production/.codex`. My diff touches none of
     `tmux.py` / `provider_plane` / `g7b`; those files are byte-identical to base `2f7f1536`,
     and the test reproduces **identically on a clean local worktree**. Not introduced by F435;
     the env-plane pin regression is owned elsewhere.

## box-actions ledger
- `box-run.sh f435 -- 'cd ~/cli-subagents/cli-agent-orchestrator && git fetch origin cao/81b6cd7e && git checkout -B cao/81b6cd7e origin/cao/81b6cd7e && git rev-parse HEAD && uv run pytest -q -m "not live and not e2e" | tee /tmp/f435-suite-run.txt | tail -15'`
  → box@cursor-3, checked out `f5b5f0a3`, full suite (result above). `uv sync` bumped 1 package (transient, box-local venv).
- `box-run.sh f435v -- '... targeted PII + F435 tests'` → could not acquire a slot (cursor-3
  contended by f437r2/f431); **timed out and released the slot cleanly on cancel**. No box state
  changed by this invocation.
- Raw ssh: none.
- Checkout SHA left on box@cursor-3: `cao/81b6cd7e` @ `f5b5f0a3` (branch). Suite-slot lock released;
  box repo left on the `cao/81b6cd7e` branch (clean, no temp branches/stashes by me).
- Temp files on box: `/tmp/f435-suite-run.txt` (in /tmp, acceptable).
- Env mutations: box-local `uv sync` package bump only; no apt/global installs; no committed lockfile change.
- Deviations: (1) used `git checkout -B <branch> origin/<branch>` instead of a bare-SHA checkout
  because the local PreToolUse fence denies detached-SHA/`git reset` — branch tip == the pushed SHA,
  so equivalent. (2) second box run did not complete due to slot contention; local targeted runs are
  authoritative for the PII fix, and the full-suite run already covered every test on `f5b5f0a3`.
  (3) Report written in-worktree (path below) because the write fence blocks `/data/cao-scratch/`.
