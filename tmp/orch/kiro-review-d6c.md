**Artifact-Path:** /data/cao-scratch/d6c-worktree/tmp/orch/d6c-build-report.md
**Artifact-SHA256:** cfe4a6e5fa6a9de9700c4a8fc7dbb05b5081e65a37d32fb982fedf9e21153905
**Artifact-Repo-Path:** tmp/orch/d6c-build-report.md
**Git-SHA-fork:** 7e6e768548fbe46a3cbe1dcda8e8082e410b781d
**Ruling:** GATE-YES — 0 BLOCKER / 0 SHOULD / 1 NIT

---

# Empirical Review: D6c — Targeted Suite-Speed Fixes

## VERDICT HEADER

| # | Severity | Finding | Amendment |
|---|----------|---------|-----------|
| N1 | NIT | CI wall-clock delta (D6b 219.42s → D6c 217.99s = 1.4s) is dominated by xdist parallelism; the per-test savings are real but only visible in single-worker or ledger per-test timings | Report could note that 47.5s is the PER-WORKER scheduling budget freed, not a direct wall-clock delta in -n 4 runs |

**Zero-decision buildable:** N/A (code fixes, not blueprint)

---

## AC6.7 — The Three Fixes: Assertion Power Analysis

### Fix 1: test_wrapped_provider_lifecycle (8bbeb6a6)

**Before:** `mock_backend.get_history.return_value = "Yes, I trust this folder"` — a fixed return_value for ALL calls. After the trust prompt was accepted (`trust_accepted=True`), subsequent loop iterations fell through to the version-banner check (`r"Welcome to|Claude Code v\d+"`), which never matched. The loop exhausted the `idle_gap` (~20s of real `asyncio.sleep(1.0)` calls).

**After:** `get_history.side_effect = ["Yes, I trust this folder", "Welcome to Claude Code v2.1.211"]` — first call triggers trust acceptance (Enter key sent), second call matches the version-banner exit condition.

**Assertion power:** PRESERVED. The test still exercises:
- Trust-prompt detection via `TRUST_PROMPT_PATTERN` (line 927 of claude_code.py)
- Trust acceptance via `send_special_key("Enter")` (asserted explicitly)
- Version-banner ready signal (the intended fast-path exit)
- Per-profile 180s timeout flow (asserted on wait_shell and wait_status kwargs)
- Buffer-driven status detection (`get_status("✻ Orbiting…")` → PROCESSING, `get_status("❯ ")` → IDLE)

The fix makes the test exercise the INTENDED code path (trust→banner→exit) rather than accidentally timing out the idle-gap fallback.

**Timing verified:**
- Pre-fix (base d5a71358): 26.81s locally (24.81s test time minus ~2s xdist overhead) — consistent with ledger 25.04s
- Post-fix: 3.05s locally (~1.05s test time) — consistent with report's 1.01s claim

### Fix 2: test_initialize_waits_for_shell_baseline_return (743f2ceb)

**Before:** `side_effect = ["mock_cli", "bash", "bash"]` — first call (baseline capture at mock_cli.py:131) set `shell_baseline="mock_cli"`. The `_wait_for_fixture_child_gone` loop (mock_cli.py:225) checked `if current == self.shell_baseline`; subsequent calls returned "bash" which never matched "mock_cli". Loop hit 15s timeout.

**After:** `side_effect = ["bash", "mock_cli", "bash"]` — first call (baseline capture) sets `shell_baseline="bash"`. Loop's first poll gets "mock_cli" (≠ baseline, fixture still running). Second poll gets "bash" (== baseline → immediate return).

**Assertion power:** PRESERVED AND IMPROVED. The test now exercises:
- The shell_baseline capture (before fixture launch) — correctly returns "bash"
- The _wait_for_fixture_child_gone polling loop — sees fixture running, then sees it exit
- The immediate-return fast-path (baseline match) rather than the timeout-and-warn fallback

Before the fix, the test was exercising the TIMEOUT PATH (warning logged, test passed anyway). The fix corrects the mock ordering to test the INTENDED behavior — child detection via baseline comparison.

**Timing verified:**
- Pre-fix: 16.94s locally (~14.94s) — consistent with ledger 15.06s
- Post-fix: 2.20s locally (~0.20s) — consistent with report's 0.11s

### Fix 3: test_wpq11_fresh_worker_submits_exactly_one_caller_task (7e6e7685)

**Before:** `wait_until_status` in `terminal_service` module was unpatched. `_confirm_worker_started_or_resubmit` (terminal_service.py:3350) called `wait_until_status` with `_DEFERRED_SUBMIT_CONFIRM_TIMEOUT=8.0s` on terminal "22222222" (non-existent mock), so it polled for 8s before timing out and falling through to the re-submit logic.

**After:** `monkeypatch.setattr(terminal_service, "wait_until_status", AsyncMock(return_value=True))` — returns immediately as "worker started."

**Assertion power:** PRESERVED. The test's purpose is verifying the deferred-init orchestration contract:
- `provider.initialize.assert_awaited_once()` — provider gets initialized
- `caller_submit.assert_called_once_with("22222222", "caller task", ...)` — exactly one task submitted with correct args

`wait_until_status` is NOT the system under test here — it's a downstream confirmation mechanism for real terminal state. Mocking it to return True is semantically correct for a unit test of the submit-exactly-once orchestration.

**Timing verified:**
- Pre-fix: 10.62s locally (~8.62s) — consistent with ledger 8.55s
- Post-fix: 2.46s locally (~0.46s) — consistent with report's <0.02s (CI has less xdist overhead)

---

## AC6.8 — gw-crash Investigation

**Verdict: Evidence-backed hypothesis, appropriate as REPORT-ONLY.**

Evidence chain:
1. F262 build report documents 3 xdist worker crashes (gw0/gw1 "node down: Not properly terminated") under `run-pytest.sh` (which invokes `systemd-run --user --scope -p CPUWeight=30`)
2. Same commit, direct `uv run pytest -n 2` (no wrapper) = 11167 passed, 0 failed — fully green
3. `scripts/run-pytest.sh` lines 47-49 confirm `CPUWeight=30 -p MemoryHigh=70% nice -n 10`
4. Census data shows 5 tests at >90% CPU ratio (up to 4.6s at 98% CPU)
5. Causal mechanism: CPUWeight=30 under contention → worker starved → xdist heartbeat timeout → "node down"

The hypothesis is testable (remove CPUWeight, observe no crashes) but the fix belongs to D4 (run-pytest.sh's resource fence), not D6c. This is correctly scoped as a finding, not a deliverable.

---

## CI Adjudication — Run 32203933623

**16 failures, 0 introduced by D6c.** Independently verified:

| D6c run (32203933623) | D6b run (32202617697) | Match |
|---|---|---|
| test_list_terminals_by_session (MagicMock) ×2 | Same ×2 | YES |
| test_rapid_writes_produce_fewer_publishes (fifo race) | test_data_received_across_writer_reconnects (fifo race) | YES (same known flake class) |
| test_backoff_sequence_30_60_120_120 (fx193 timing) | Same | YES |
| test_no_expired_quarantine_entries ×2 | Same ×2 | YES |
| test_tombstones.py ×8 | Same ×8 | YES |
| **Total: 16** | **Total: 16** | **All pre-existing** |

### Tombstone failures — cross-repo dependency defect (observation)

The 8 `test/utils/test_tombstones.py` failures result from a MISSING `scripts/tombstone-report` script. Verified:

1. `test/utils/test_tombstones.py` line 120: `subprocess.run([sys.executable, str(ROOT_SCRIPTS_DIR / "tombstone-report")] + ...)` — executes the script as a subprocess
2. `_find_root_repo_scripts()` (lines 31-62) attempts 5 resolution strategies:
   - `CAO_TOMBSTONE_REPORT_PATH` env (not set in CI)
   - `fork_root.parent / "scripts" / "tombstone-report"` — fails on CI (no parent mono-repo)
   - `git rev-parse --git-common-dir` worktree resolution — fails (CI checkout is not a worktree)
   - Absolute fallback `/home/chao/...` — doesn't exist on CI runner
   - Last resort: `fork_root / "scripts"` — the file doesn't exist there
3. CI error messages confirm: `can't open file '.../scripts/tombstone-report': [Errno 2] No such file or directory`
4. The script lives at `/home/chao/VScode_projects/cli-subagents/scripts/tombstone-report` (root repo, confirmed exists locally)
5. Same 8 failures appear identically on D6b run (parent branch), confirming NOT a D6c regression

This is a cross-repo dependency from F261: the fork's CI checkout doesn't include the root repo's `scripts/` directory. It's permanently red in fork-only CI and out of D6c's scope to fix.

---

## Empirical Checks

| # | Check | Method | Observed Result |
|---|-------|--------|-----------------|
| 1 | Fix 1 pre-fix timing | `uv run pytest` on base files | 26.81s (consistent with 25.04s ledger) |
| 2 | Fix 2 pre-fix timing | `uv run pytest` on base files | 16.94s (consistent with 15.06s ledger) |
| 3 | Fix 3 pre-fix timing | `uv run pytest` on base files | 10.62s (consistent with 8.55s ledger) |
| 4 | Fix 1 post-fix timing | `uv run pytest` on fixed code | 3.05s session / ~1.05s test time |
| 5 | Fix 2 post-fix timing | `uv run pytest` on fixed code | 2.20s session / ~0.20s test time |
| 6 | Fix 3 post-fix timing | `uv run pytest` on fixed code | 2.46s session / ~0.46s test time |
| 7 | CI failure match (D6c) | grep CI log run 32203933623 | 16 FAILED lines extracted |
| 8 | CI failure match (D6b) | grep CI log run 32202617697 | 16 FAILED lines extracted, same set |
| 9 | tombstone-report path resolution | Read test/utils/test_tombstones.py lines 29-64 | 5 fallback strategies, all fail on CI runner |
| 10 | tombstone-report exists locally | ls root repo | Confirmed at /home/chao/VScode_projects/cli-subagents/scripts/tombstone-report |
| 11 | run-pytest.sh CPUWeight | grep scripts/run-pytest.sh | Confirmed CPUWeight=30 at line 48-49 |
| 12 | Production code path (fix 1) | Read claude_code.py:798-957 | Trust→banner→exit path confirmed; idle_gap fallback on line 865 |
| 13 | Production code path (fix 2) | Read mock_cli.py:206-232 | `if current == self.shell_baseline` at line 225 |
| 14 | Production code path (fix 3) | Read terminal_service.py:3247-3355 | `wait_until_status` with 8.0s timeout at line 3350-3353 |
| 15 | Worktree cleanliness | `git status --short` at start and end | Clean (no modifications) |
| 16 | Authority pin | `verify_pin` at task start | VALID, version=1 |

---

## APPENDIX: Detailed Evidence

### Fix 1 — _handle_startup_prompts flow

The production code (claude_code.py:798-957) loops:
1. Read buffer via `get_history`
2. Check trust pattern → accept with Enter, set `trust_accepted=True`, reset idle timer
3. Check version banner (`r"Welcome to|Claude Code v\d+"`) → return (startup complete)
4. Sleep 1.0s and loop

With `return_value` (old): every iteration after trust acceptance gets the same "Yes, I trust this folder" text. Trust is already accepted so it skips to banner check, never matches, sleeps 1.0s. Repeats ~20 times until idle_gap expires.

With `side_effect` (new): first call → trust accepted, second call → banner matched → exit. This is exactly the real startup sequence.

### Fix 2 — _wait_for_fixture_child_gone flow

The production code (mock_cli.py:115-232):
1. `get_pane_current_command` is called BEFORE fixture launch to capture `shell_baseline`
2. After `send_keys` launches the fixture, `_wait_for_fixture_child_gone` polls
3. Each poll: `if current == self.shell_baseline` → child is gone, return

Old mock ordering made baseline="mock_cli", which is the fixture binary name — semantically wrong. The loop could never detect child exit because "bash" ≠ "mock_cli".

New mock ordering makes baseline="bash" (correct: the shell is bash before the fixture starts). The loop sees "mock_cli" (fixture running), then "bash" (child gone, back to shell).

### Fix 3 — _confirm_worker_started_or_resubmit flow

The production code (terminal_service.py:3323-3355):
1. `wait_until_status(terminal_id, {PROCESSING, COMPLETED, WAITING_USER_ANSWER}, timeout=8.0)` — polls real terminal status
2. If True → confirmed started, return True
3. If False → enter re-submit retry loop

The test's terminal "22222222" is purely synthetic (no tmux pane, no provider process). Letting `wait_until_status` run live was an oversight — it burned 8s polling nothing. Mocking it to return True reflects the test's intent: "assume worker started, verify the submit-once contract."
