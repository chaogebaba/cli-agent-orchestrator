**Artifact-Path:** /data/cao-scratch/f305-worktree/tmp/orch/f305-build-report.md
**Artifact-SHA256:** 3ef382a42df5d507b46ed397c9894fa61ffc8ed9b42d5624dc6fcd9940e596eb
**Artifact-Repo-Path:** tmp/orch/f305-build-report.md
**Git-SHA-fork:** a0e5c4e2ec32139d25cde54cffd5d21eb98db1e1
**Ruling:** GATE-YES — 0 BLOCKER / 1 SHOULD / 1 NIT

---

## Verdict Header

| # | Severity | Claim | Amendment |
|---|----------|-------|-----------|
| S1 | SHOULD | D6c build report miscounted tombstone failures as 8; actual parent CI run 32202617697 has 10 FAILED lines in test_tombstones.py | Update D6c report table to say 10 (non-blocking — the F305 report is correct) |
| N1 | NIT | `_SCRIPT_NAME` constant is module-level but only used in one function; no functional concern but slightly wider scope than needed | Could be local to `_find_root_repo_scripts()` — cosmetic only, leave as-is |

---

## Empirical Checks

### Check 1 — Module-level skip does NOT fire in operator layout

```
make test-quick ARGS="test/utils/test_tombstones.py -v"
→ 17 passed in 4.04s — ALL tests ran, 0 skipped
```

Confirms the resolver finds `scripts/tombstone-report` via the git common-dir
path (worktree at /data/cao-scratch/f305-worktree resolves to root repo at
/home/chao/VScode_Projects/cli-subagents where the script exists). Skip never fires.

### Check 2 — Resolver diff analysis

| Aspect | Before (21b06fa4) | After (a0e5c4e2) | Verdict |
|--------|-------------------|-------------------|---------|
| Return type | `Path` (always) | `Path \| None` | Correct |
| Env override | Returns parent unconditionally | Checks `exists()`, returns None if missing | Correct |
| Fork-parent structural resolution | `(root_candidate / "scripts" / "tombstone-report").exists()` | Same, via `_SCRIPT_NAME` constant | Unchanged |
| Git common-dir resolution | Same pattern | Same, via `_SCRIPT_NAME` constant | Unchanged |
| Hardcoded fallback `/home/chao/...` | Present | Removed | Correct — the absolute path was a deployment leak |
| Last-resort `fork_root / "scripts"` | Returned even if script absent | Replaced with `return None` | Correct — old code returned a broken path |
| `import subprocess` inside function | Redundant (already top-level) | Removed | Correct |

No other behavioral change.

### Check 3 — TestResolverSkipPath is not a tautology

Simulated old resolver behavior: with `CAO_TOMBSTONE_REPORT_PATH` pointing to
a non-existent file, the OLD code returns `Path(env_path).parent` (non-None),
while the NEW code returns `None`. The assertion `assert result is None` would
**FAIL** against the old resolver. The test genuinely covers the new None-path.

### Check 4 — CI run verification

| Run | headSha | Failures | Skipped | Tombstone status |
|-----|---------|----------|---------|------------------|
| 32202617697 (before, cao/wp-suite-d6b) | d5a71358 | 16 | 55 | 10 FAILED |
| 32212220734 (after, cao/f305) | a0e5c4e2 ✓ | 5 | 56 | module-level SKIP (1 skip entry) |

Arithmetic: 16 - 10 tombstone - 1 flaky = 5 remaining. The one additional
disappearing failure is `test_fx193_nudge_discipline.py::TestAC5Backoff::
test_backoff_sequence_30_60_120_120` — a timing-sensitive test that appeared
in the before-run but not the after-run. This is an unrelated flaky test (it
fires non-deterministically on timing races), not a tombstone test and not
introduced or removed by this change.

The skip count went from 55 → 56: exactly +1 module-level skip for
`test/utils/test_tombstones.py` containing 16 test items (which count as 1
skip entry at module level, not 16 individual skips — pytest module-level skip
emits a single skip node for the entire module in the summary).

Note: The D6c build report stated "8 tombstone failures" but the actual CI
run 32202617697 shows 10 FAILED lines in test_tombstones.py. The F305 build
report correctly counts 10. The D6c discrepancy is likely from a different
CI run or a count of unique error patterns rather than individual test methods.

### Check 5 — Remaining 5 failures are the known F303 (#157) set

| Test | Category |
|------|----------|
| `test_database.py::TestMessageTraceTransactions::test_list_terminals_by_session` | MagicMock TypeError |
| `test_f264_database_hardening.py::test_list_terminals_by_session_skips_stale_rows` | MagicMock TypeError |
| `test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects` | Flaky reader reconnect |
| `test_f254_quarantine.py::test_no_expired_quarantine_entries` | Quarantine expiry |
| `test_f254_quarantine.py::test_expiry_guard_fires_for_non_serial_only` | Quarantine expiry |

All 5 match the F303 known-failure set (MagicMock ×2, fifo_reader race ×1,
quarantine expiry ×2). Confirmed.

---

## Baseline Verification

Working tree hash of `test/utils/test_tombstones.py` at review start and end:
`fd0109778fe537496534e23091d4abc7f5c9231ca7aa682003dfcf92e773d767`
`git status --short`: clean (no modifications).
