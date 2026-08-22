# F254 Phase 5 Hotfix — Build Report R1

Artifact-Path: /home/chao/VScode_projects/cli-subagents/orchestrator/tmp/orch/f254-p5-hotfix-r1.md
Artifact-SHA256: (computed post-write)
Artifact-Repo-Path: tmp/orch/f254-p5-hotfix-r1.md
Blueprint-SHA256: 6d721e78d8c7be00288ade64ce269120131fdd367f4b097ffbad2db7e6b3cf30
Git-SHA: c433af86bd95e481cb9c9baace445f31f1f6b909 (fix) + report attached at HEAD
Git-Branch: cao/f254-p5-hotfix
Base-Commit: f8b8e746 (merge tip of cao/f254-p5 into f254-phase3-enforcement)

---

## Diagnosis

### Root Cause (hypothesis b confirmed)

The `test/ux/contract/conftest.py` autouse cleanup fixture (introduced in R2, F4 fix) diffed the **global** tmux session list before/after each test. Under xdist `-n 2`, this caused cross-worker interference:

1. Worker gw0 creates tmux session `cao-contract-xxxxxxxx` via POST /sessions
2. Worker gw1 finishes its test, runs teardown
3. gw1's cleanup sees gw0's session as "new" (wasn't in pre-test snapshot) → kills it
4. gw0's cao_server subprocess detects the killed session → background `_f218_confirmed_gone_pipeline` fires → tries to query DB → fails with `sqlite3.OperationalError: unable to open database file`
5. The poisoned server state causes all subsequent `POST /sessions` calls to 500

### Red Artifact (before fix)

From `/home/chao/VScode_projects/cli-subagents/orchestrator/tmp/orch/f254-p5-test-quick.txt`:
```
12 failed / 11513 passed
FAILED test/ux/contract/test_canary_d8_2.py::TestCanaryD8_2::test_assign_creates_terminal_row_in_subprocess_db
FAILED test/ux/contract/test_assign_contract.py::TestAssignContractUX6::test_assigned_worker_visible_in_fleet
... (all 12 are contract tests, all 500 on POST /sessions)
```

### Fix (2 changes to conftest + track_session in test methods)

1. **Targeted cleanup**: replaced global tmux diff with per-test session tracking. Each test registers its created session names via `track_session` fixture; teardown kills only THOSE sessions.

2. **xdist_group serialization** (defense-in-depth): `pytest_collection_modifyitems` assigns all `test/ux/contract/` tests to `xdist_group("contract-server")` so they prefer the same worker under `--dist loadgroup`. Blueprint D34 pattern: group by shared resource.

### Envelope

6 files modified, all under `test/ux/contract/`. Zero `src/` changes.

---

## Amendment Log Entry

| # | Decision amended | Forced by | Change |
|---|------------------|-----------|--------|
| 7 | D34 | P5 hotfix (xdist cross-worker tmux kill race) | Contract tests serialized via `xdist_group("contract-server")` — the cao_server's tmux namespace is the shared resource. Cleanup conftest changed from global tmux diff to per-test tracked session cleanup. |

---

## Proof — Green After Fix

### make test-quick (full)

```
$ TCACHE_BIN=/home/chao/VScode_projects/cli-subagents/scripts/tcache make test-quick
==== 11518 passed, 159 skipped, 9 xfailed, 2 warnings in 314.12s (0:05:14) =====
```

### C-kind at -n 0 (gate regression check)

```
$ uv run pytest test/ux/contract/ -v -n 0
======================== 17 passed in 66.50s (0:01:06) =========================
```

### C-kind at -n 2 (xdist, the failure mode)

```
$ uv run pytest test/ux/contract/ -v -n 2 --dist loadgroup
============================= 17 passed in 38.68s ==============================
```

---

## Evidence Logs

- Red artifact: /home/chao/VScode_projects/cli-subagents/orchestrator/tmp/orch/f254-p5-test-quick.txt
- Green make test-quick: /data/cao-scratch/logs/hotfix-test-quick.txt
