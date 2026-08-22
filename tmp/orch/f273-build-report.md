**Artifact-Path:** /data/cao-scratch/f273-worktree/tmp/orch/f273-build-report.md
**Artifact-Repo-Path:** tmp/orch/f273-build-report.md
**Git-SHA-fork:** d07fc67f

## CI Run

- **Run ID:** 32194254958
- **URL:** https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32194254958
- **Conclusion:** failure (6 failed, 11549 passed, 55 skipped, 9 xfailed — 223.57s)

## Failure Analysis

All 6 failures are pre-existing from `cao/wp-suite-d6b` (dc70fd76) — F273 does NOT touch any of the failing files.

| # | Test | Root Cause | Ownership |
|---|------|-----------|-----------|
| 1 | `test/clients/test_database.py::TestMessageTraceTransactions::test_list_terminals_by_session` | `json.loads(t.metadata_json)` added by wp-suite-d6b; test mocks return MagicMock not str | wp-suite-d6b |
| 2 | `test/clients/test_f264_database_hardening.py::test_list_terminals_by_session_skips_stale_rows` | Same — mock provides MagicMock where str needed | wp-suite-d6b |
| 3 | `test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects` | Known flaky — cold-start re-arm race in CI runner timing | pre-existing race |
| 4 | `test/services/test_fifo_reader.py::TestConcurrencyRaces::test_reader_loop_last_data_at_write_is_atomic_with_stop` | Known flaky — thread timing race on CI runner | pre-existing race |
| 5 | `test/test_f254_quarantine.py::test_expiry_guard_fires_for_non_serial_only` | `known_red` entries missing `expires` field (have `review_by` only) | wp-suite-d6b quarantine.toml |
| 6 | `test/test_f254_quarantine.py::test_no_expired_quarantine_entries` | Same quarantine malformation | wp-suite-d6b quarantine.toml |

## Deselect-List Caveat

F273 touches only `test/utils/test_scratch.py` (9 unit tests for scratch_dir helper). None of the deselected/failing tests overlap with F273 changes. No focused local re-run needed.

## Merge Conflicts Resolved

- **Makefile `test-quick`**: Combined F273 preflight + wp-suite-d6b `-m "not live"` marker.
- **Makefile `test-live`**: Combined F273 preflight + wp-suite-d6b `--run-live` flag.
- **scripts/run-pytest.sh**: wp-suite-d6b removed SUITE_LOCK entirely (D4/AC4.2 — flock removed); F273 had moved it to /data. Took the removal (variable unused in rest of script).

## Additional Fix

- `actions/attest@afd638...` SHA was unresolvable (bogus commit from wp-suite-d6b). Fixed to actual `v2` tag SHA `11bbd243972067817e9ed160cb123cab3601f436`.

## Root-Half State

`scripts/test-gated-merge.sh` and `scripts/test-tcache.sh` are **missing** from this worktree — they were never committed to `cao/f273`. They belong to the root repo, not this fork branch.
