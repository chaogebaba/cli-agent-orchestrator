**Artifact-Path:** /data/cao-scratch/f305-worktree/tmp/orch/f305-build-report.md
**Artifact-Repo-Path:** tmp/orch/f305-build-report.md
**Git-SHA-fork:** a0e5c4e2ec32139d25cde54cffd5d21eb98db1e1
**Branch:** cao/f305
**Base:** cao/wp-suite-d6b (21b06fa4)
**Issue:** #159 (F305)
**Option:** (1) — module-level pytest.skip when resolver fails

---

## Summary

Fixed fork CI permanently-red tombstone tests by making the cross-repo script
dependency skip-honest. When `tombstone-report` (root repo) cannot be located,
the module now emits `pytest.skip(allow_module_level=True)` instead of erroring
on a missing executable.

## Changes (test/utils/test_tombstones.py)

1. **`_find_root_repo_scripts()` returns `None`** when no candidate resolves —
   env override path checked for existence, structural resolutions unchanged,
   no fallthrough to a broken `fork_root/"scripts"`.

2. **Module-level skip** with descriptive message naming the root-repo dependency
   and `CAO_TOMBSTONE_REPORT_PATH` override env var.

3. **Deleted hardcoded path** `/home/chao/VScode_projects/cli-subagents/scripts` —
   committed absolute host path removed.

4. **Added `TestResolverSkipPath`** — one test asserting the resolver returns `None`
   when `CAO_TOMBSTONE_REPORT_PATH` points to a non-existent file, covering the
   skip code path directly.

## Verification

### Local (operator layout)
```
make test-quick ARGS="test/utils/test_tombstones.py -v"
→ 17 passed in 5.44s (all tombstone tests + new resolver test)
```

### Fork CI

| Run | Branch | Failures | Skipped | Passed | Tombstone status |
|-----|--------|----------|---------|--------|------------------|
| [32202617697](https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32202617697) (before) | cao/wp-suite-d6b | 16 | 55 | 11588 | 10 FAILED |
| [32212220734](https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32212220734) (after) | cao/f305 | 5 | 56 | 11583 | module-level SKIP (1 skip entry) |

**Failure reduction:** 16 → 5 (tombstone contribution: 10 → 0)

Remaining 5 failures are pre-existing, unrelated to tombstones:
- test_database.py / test_f264_database_hardening.py: MagicMock TypeError
- test_fifo_reader.py: flaky reader reconnect assertion
- test_f254_quarantine.py (x2): expired quarantine entries
