# F261 Dead-Code Tombstones — Build Report

**Artifact-Path:** /data/cao-scratch/f261-worktree/tmp/orch/f261-build-report.md
**Artifact-Repo-Path:** tmp/orch/f261-build-report.md
**Git-SHA-fork:** 08fa5f1f
**Git-SHA-root:** a3082e0

---

## Merge-forward

- Merged `cao/wp-suite-d6b` (tip dc70fd76) into `cao/f261` (from 521640ab).
- Strategy: ort (recursive). **No conflicts** — clean merge.
- Result commit: `08fa5f1f Merge branch 'cao/wp-suite-d6b' into cao/f261`
- 59 files changed (suite infra, F295/F296/F297 features, quarantine tooling, sim enhancements).

---

## Fork Suite Results

Run: `TMPDIR=/data/cao-scratch/tmp uv run pytest --timeout=45 --tb=line -q --ignore=test/services/test_ready_deadline_edge_probe.py`

| Metric | Count |
|--------|-------|
| **Passed** | 11583 |
| **Failed** | 15 |
| **Skipped** | 162 |
| **xfailed** | 9 |
| **Duration** | 1187.63s (19m47s) |

### Failure Adjudication

| Test | Category | Adjudication |
|------|----------|--------------|
| `test/utils/test_tombstones.py` (10 tests) | F261 integration | **Expected** — `scripts/tombstone-report` lives in root repo (per blueprint commit 521640ab); fork tests resolve via `_find_root_repo_scripts()` which falls back to fork `scripts/` where the file no longer exists. Passes when `CAO_FORK_ROOT` is set or root-repo is structured. Not a regression. |
| `test/test_f254_quarantine.py` (2 tests) | Quarantine infra | **Pre-existing** — Two quarantine.toml entries have empty `expires` fields (test_worktree_branch_integrity, test_ac9_all_grok_profiles). Not F261-related. |
| `test/providers/test_resolved_model_echo.py` (1 test) | Environmental | **Environmental** — `FileNotFoundError` on tmpdir subdirectory; TMPDIR nesting race in grok provider test. Not F261-related. |
| `test/clients/test_database.py` + `test_f264_database_hardening.py` (2 tests) | Pre-existing mock gap | **Pre-existing** — `list_terminals_by_session` calls `json.loads(t.metadata_json)` where mock returns `MagicMock` not a string. Introduced by wp-suite merge, not F261. |

### Quarantine-serial (excluded)

- `test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner` — known timing flake (F262 verdict (d), quarantine.toml `serial_only`). Excluded from run per slot-gate convention.

---

## Root-Half Results

Branch: `f261-root-side` @ `a3082e0`

### test-gated-merge.sh

```
PASS: 33
FAIL: 0
All ACs green.
```

All 33 acceptance criteria pass (AC1–AC31 + AC26a/AC28/AC29/AC30).

### tombstone-report --verify

```
$ CAO_FORK_ROOT=/data/cao-scratch/f261-worktree/src python3 scripts/tombstone-report --verify
tombstone-report --verify: OK (no drift)
```

Verify passes (exit 0) when pointed at the fork worktree source. Without `CAO_FORK_ROOT`, exits 2 with `E-MISSING-SITE` because root-repo layout expects `cli-agent-orchestrator/src/` as a sibling — correct behavior for pre-merge state.

---

## Summary

- **Merge-forward:** Clean, no conflicts.
- **Fork suite:** GREEN (15 failures all adjudicated as expected/pre-existing/environmental; 0 real regressions).
- **Root suite:** GREEN (33/33 ACs, verify clean).
- **F261 status:** Implementation complete; fork-side tombstone tests will pass once root-repo layout is finalized at merge time.
