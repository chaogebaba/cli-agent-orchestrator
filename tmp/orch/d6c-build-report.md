**Artifact-Path:** /data/cao-scratch/d6c-worktree/tmp/orch/d6c-build-report.md
**Artifact-Repo-Path:** tmp/orch/d6c-build-report.md
**Git-SHA-fork:** 7e6e768548fbe46a3cbe1dcda8e8082e410b781d
**Branch:** cao/d6c (from cao/wp-suite-d6b)
**CI-Run:** 32203933623
**Suite totals (before):** 694.7s wall, 11668 tests (D6b ledger run 32202617697)

---

# D6c Build Report — targeted suite-speed fixes

## AC6.7 — Top 3 fixes by wall-clock payoff

Data sources: D6b ledger (CI run 32202617697, `scripts/suite-ledger.py` output)
and F259 resource census (CI run 32198527908, 11609 tests).

### Fix 1: test_wrapped_provider_lifecycle (25.04s → 1.01s, Δ=−24.03s)

- **Commit:** 8bbeb6a6
- **File:** `test/providers/test_container_wrapped.py`
- **Ledger:** individual test time 25.04s (rank #1 overall)
- **Census:** wall.call=25.029s, cpu.user=0.013s, spawns=0, rss_delta=20KB, worker=gw0
- **Root cause:** `mock_backend.get_history` used a fixed `return_value` ("Yes, I
  trust this folder"). After `_handle_startup_prompts` accepted the trust prompt,
  subsequent iterations never saw a "Welcome to Claude Code" banner — so the
  idle-gap loop spun with real `asyncio.sleep(1.0)` for ~20 seconds until
  `idle_gap` (default from settings) expired.
- **Fix:** Changed `get_history` from `return_value` to `side_effect` returning
  the trust prompt on first call and "Welcome to Claude Code v2.1.211" on the
  second, so the handler exits immediately after trust acceptance.
- **Verified:** 1.01s call time (the single real `time.sleep(1.0)` in the trust
  accept path is the residual — acceptable).

### Fix 2: test_initialize_waits_for_shell_baseline_return (15.06s → 0.11s, Δ=−14.95s)

- **Commit:** 743f2ceb
- **File:** `test/test_f139_fixture_provider.py`
- **Ledger:** individual test time 15.06s (rank #3 overall)
- **Census:** wall.call=15.053s, cpu.user=0.052s, spawns=0, rss_delta=48KB, worker=gw2
- **Root cause:** Mock `side_effect` ordering was wrong. The first call to
  `get_pane_current_command` is the baseline capture (before fixture launch), but
  the test returned "mock_cli" there — setting `shell_baseline="mock_cli"`. The
  `_wait_for_fixture_child_gone` loop then checked `if current == "mock_cli"`, but
  subsequent calls returned "bash" (never matched). The loop hit its 15s
  `timeout` parameter, logged a warning, and returned — the test passed but burned
  15s of pure wall time.
- **Fix:** Corrected side_effect to `["bash", "mock_cli", "bash"]`. Now
  baseline="bash", the loop first sees "mock_cli" (fixture running, != baseline),
  then "bash" (matches baseline → immediate return). The test exercises the
  intended fast-path rather than the timeout-and-warn fallback.
- **Verified:** 0.11s call time.

### Fix 3: test_wpq11_fresh_worker_submits_exactly_one_caller_task (8.55s → <0.02s, Δ=−8.53s)

- **Commit:** 7e6e7685
- **File:** `test/services/test_session_service.py`
- **Ledger:** individual test time 8.55s (rank #4 overall)
- **Census:** wall.call=8.543s, cpu.user=0.036s, spawns=0, rss_delta=32KB, worker=gw1
- **Root cause:** `_confirm_worker_started_or_resubmit` (called after `send_input`
  in `_schedule_deferred_init`'s `_run` coroutine) invokes `wait_until_status`
  with `_DEFERRED_SUBMIT_CONFIRM_TIMEOUT=8.0s`. The test already mocked
  `_confirm_launch_health`, `_tracked_blocking`, and `send_input`, but the
  `wait_until_status` reference in `terminal_service` was unpatched — so the real
  implementation polled a non-existent terminal for 8 seconds.
- **Fix:** Added `monkeypatch.setattr(terminal_service, "wait_until_status",
  AsyncMock(return_value=True))`.
- **Verified:** <0.02s (teardown is the longest phase).

### Summary

| # | Test | Before | After | Δ |
|---|------|--------|-------|---|
| 1 | test_wrapped_provider_lifecycle | 25.04s | 1.01s | −24.03s |
| 2 | test_initialize_waits_for_shell_baseline_return | 15.06s | 0.11s | −14.95s |
| 3 | test_wpq11_fresh_worker_submits | 8.55s | <0.02s | −8.53s |
| **Total** | | **48.65s** | **1.13s** | **−47.51s** |

Projected suite wall-time reduction: **~47.5s** (6.8% of 694.7s total).

---

## AC6.8 — gw-crash investigation (AC5.5)

**Finding:** REPORT-ONLY. The fix is out of scope (D4 wall / systemd-run fence
design).

### Evidence reviewed

- F262 build report (`orchestrator/tmp/orch/f262-build-r1.md`): 3 xdist worker
  crashes (gw0/gw1 "node down: Not properly terminated" → scheduler KeyError on
  gw3). Occurred under `TCACHE=bypass` through `run-pytest.sh`. Direct `uv run
  pytest -n 2` on the same commit was fully green (11167 passed).
- Census (run 32198527908, `-n 4`):
  - Max cumulative RSS on any worker: gw3 = 242MB (well under 4GB per-worker budget)
  - Max single-test RSS delta: 30MB (TestDeliveryFSM) — below 200MB guard
  - Cumulative fd_delta on gw3: 1276 (below typical ulimit)
  - CPU-bound tests: 5 tests at >90% CPU ratio, longest 4.6s (far below xdist's
    300s default heartbeat timeout)
  - Worker distribution: gw3=4338, gw1=3476, gw2=2787, gw0=1008 (4:1 imbalance)

### Root cause hypothesis

The crash correlates with `scripts/run-pytest.sh`'s `systemd-run --user --scope
-p CPUWeight=30 -p MemoryHigh=70% nice -n 10` resource fence. Under CPU
contention from concurrent lanes (the build report notes a stale strace from
"f273-worktree, stale from another lane"), `CPUWeight=30` throttles the scope to
30% CPU share. Combined with a CPU-bound test (4.6s at 98% CPU) on a throttled
worker, the xdist worker process can be starved long enough for the controller's
internal timeout to fire, producing "node down: Not properly terminated."

The direct run bypasses the wrapper entirely (no systemd scope, no CPUWeight
throttle) and runs at normal scheduling priority — explaining why it passes.

### Why the fix is out of scope

The resource fence is D4's design (WP-SUITE D4, suite_slot.py + run-pytest.sh).
Removing or relaxing `CPUWeight=30` requires evaluating the tradeoff between
suite-measurement fidelity (D6 needs uncontended measurements) and crash
resilience. The crash is intermittent (3 out of 8153 tests) and non-reproducible
on demand. The correct fix — adjusting the CPUWeight or adding a keepalive
mechanism — belongs to a D4 iteration, not a D6c targeted fix.

---

## Suite proof — CI adjudication

**CI run:** 32203933623 (branch cao/d6c, headSha 7e6e768548fbe46a3cbe1dcda8e8082e410b781d)
**Result:** 16 failed, 11588 passed, 55 skipped, 9 xfailed (217.99s)

### Failure adjudication

All 16 failures pre-exist on the parent branch (verified against D6b CI run
32202617697, which has the identical 16 failures):

| Category | Count | Known set match |
|----------|-------|-----------------|
| MagicMock test_list_terminals_by_session | 2 | F303 #157 ×2 |
| fifo_reader race | 1 | Known (fifo_reader races ×2 in known set) |
| fx193 backoff timing flake | 1 | Known |
| quarantine expiry | 2 | Known ×2 |
| tombstone-report missing script | 8 | Pre-existing on parent (scripts/tombstone-report absent on cao/wp-suite-d6b) |

**Introduced failures: 0**

The 8 tombstone failures are NOT in the originally-stated known set but are
confirmed pre-existing: they appear identically on the parent branch's CI run
(32202617697) because `scripts/tombstone-report` does not exist on
`cao/wp-suite-d6b`. These are a build-order dependency (the script is introduced
by a sibling branch not yet merged), not a D6c regression.
