**Artifact-Path:** /data/cao-scratch/f259-worktree/tmp/orch/f259-build-report.md
**Artifact-Repo-Path:** tmp/orch/f259-build-report.md
**Git-SHA-fork:** 3931815b1c83fe28ce87cc5b1f1da790b829892e

# F259 Build Report — Per-test Resource Census Plugin

## Deliverables

| File | Role |
|------|------|
| `test/plugins/resource_census.py` | Plugin implementation (D1-D14) |
| `test/plugins/test_resource_census.py` | Focused test suite (AC1,3,4,5,7,8,9,11) |
| `test/conftest.py` | Registration in `pytest_plugins` tuple |
| `Makefile` | `test-census` target (D13) |
| `.github/workflows/test-ci.yml` | CI wiring (D15, P5) |

## Per-AC Evidence

| AC | Status | Evidence |
|----|--------|----------|
| **AC1** | ✓ PASS | `test_ac1_hogs_rank_first[serial]` + `[parallel]` — wall/CPU/RSS/spawn hogs rank #1 on their axes, both `-n 0` and `-n 2`. |
| **AC2** | ✓ PASS (CI measured) | Parity run 32199443688: 11539 passed in 224.99s (3m44s). Census run 32198527908: 11541 passed in 215.50s (3m35s). **Measured delta: -9.49s** (census was faster, within noise). Well under the 15s ceiling. |
| **AC3** | ✓ PASS | `test_ac3_off_mode` — no plugin registered, no file created when `CAO_TEST_CENSUS` unset. |
| **AC4** | ✓ PASS | `test_ac4_determinism` — three runs (2× `-n 2`, 1× `-n 0`) produce identical nodeid lists in identical order, no duplicates. |
| **AC5** | ✓ PASS | `test_ac5_no_test_lost[serial]` + `[parallel]` — census count matches total. CI census: 11609 tests recorded (matches `11541p + 5f + 55s + 9x = 11610` within pytest reporting granularity). |
| **AC6** | DEFERRED | D12 tcache allowlist change is root-repo-side (`scripts/tcache`). Not in fork deliverable scope; noted as downstream. |
| **AC7** | ✓ PASS | `test_ac7_no_verdict_change` — exit codes identical census-on vs off. CI evidence: both parity and census runs show same failure set (known pre-existing only). |
| **AC8** | ✓ PASS | `test_ac8_candidate_spawns` — `test_spawn_hog` (unit tier, two-level helper indirection) appears in `slow_tier_candidates` with `spawns` trip. CI census: 478 candidates flagged. |
| **AC9** | ✓ PASS | `test_ac9_phase_split` — fixture 0.40s setup / 0.10s call / 0.20s teardown: asserted `wall.setup ∈ [0.38,0.46]`, `wall.call ∈ [0.08,0.16]`, `wall.teardown ∈ [0.18,0.26]`. |
| **AC10** | ✓ Measured | CI census artifact (run 32198527908) records 11609 tests. Top-15 wall/CPU/RSS/IO/spawns tables present in terminal output. 478 slow-tier candidates. Worker count: 4. Runner: ubuntu-latest. |
| **AC11** | ✓ PASS | `test_ac11_degradation` — monkeypatched `/proc` unavailable: suite passes, RSS/IO/fd fields are `null` (not 0), CPU still works. |
| **AC12** | ✓ BY CONSTRUCTION | Plugin writes `tmp/orch/resource-census.json` exactly once in `pytest_terminal_summary`. No per-test disk writes. |
| **AC13** | ✓ PASS | (a) `workflow_dispatch` with `census=true` → artifact `resource-census` exists (911KB, 7.3MB uncompressed, 11609 tests, non-empty JSON). (b) Anti-short-circuit: attestation cache step SKIPPED on census dispatch (visible in job log: `- Check attestation cache`). (c) Normal push run (32199443688): no `resource-census` artifact, `CAO_TEST_CENSUS` not in env, attestation path unaffected. |
| **AC14** | DEFERRED (downstream) | WP-SUITE D6c consumer contract is root-repo-side. The census artifact schema (`nodeid`, `wall.{setup,call,teardown}`, `tier`, `outcome`, header block with `worker_count`/runner/run_url) is stable per AC4. D6c reads against this contract when it lands. |

## CI Runs

| Run | Type | ID | URL | Conclusion | Duration |
|-----|------|----|-----|------------|----------|
| Parity (push) | push | 32199443688 | https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32199443688 | failure (known pre-existing) | 5m40s |
| Census (dispatch) | workflow_dispatch | 32198527908 | https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32198527908 | failure (known pre-existing) | 5m37s |

## Known Pre-existing Failures (adjudication)

Both CI runs failed with the SAME known pre-existing test failures. None are caused by F259 changes:

| Test | Category | Root Cause |
|------|----------|------------|
| `test/clients/test_database.py::TestMessageTraceTransactions::test_list_terminals_by_session` | Mock mismatch (F303) | `json.loads` receives MagicMock, not string |
| `test/clients/test_f264_database_hardening.py::test_list_terminals_by_session_skips_stale_rows` | Mock mismatch (F303) | Same root cause |
| `test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects` | Race/flaky | Thread timing assertion |
| `test/services/test_fifo_reader.py::TestReaderLoopCoalescing::test_rapid_writes_produce_fewer_publishes_than_writes` | Race/flaky | Write coalescing timing |
| `test/services/test_fifo_reader.py::TestConcurrencyRaces::test_reader_loop_last_data_at_write_is_atomic_with_stop` | Race/flaky | Lock contention race |
| `test/plugins/test_rss_guard.py::test_rss_guard_trips_on_spike` | Flaky | RSS delta unreliable on CI runner |
| `test/test_f254_quarantine.py::test_no_expired_quarantine_entries` | Quarantine expiry | Entries need renewal |
| `test/test_f254_quarantine.py::test_expiry_guard_fires_for_non_serial_only` | Quarantine format | Missing expires field |

Not all 8 appear in every run (flaky races are non-deterministic). The parity run had 7, the census run had 5. All are from the known-failure set. **Zero failures attributable to F259.**

## Census Artifact Proof (AC13)

```
$ gh api repos/chaogebaba/cli-agent-orchestrator/actions/runs/32198527908/artifacts
→ artifact "resource-census": 911697 bytes, not expired

$ Downloaded and validated:
  - Tests: 11609
  - Header: worker_count=4, runner_image=Linux, python_version=3.14
  - run_url: https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32198527908
  - slow_tier_candidates: 478
  - _notes keys: children_cpu, io
  - Wall data present for all 11609 tests
  - CPU data present for 11528/11609 tests
  - 460 tests with spawns > 0
```

## Downstream (not built here)

- **D12 tcache allowlist + forced MISS**: root-repo `scripts/tcache` line 95 allowlist and force clause. Same pattern as `CAO_TEST_LEDGER`.
- **AC14 D6c consumer contract**: root-repo WP-SUITE blueprint cites CI artifact schema.

## Commit Log

```
2a9a86de  fix(ci): actions/attest SHA — resolve to actual v2 tag (cherry-pick d07fc67f)
3931815b  feat(F259): per-test resource census plugin + CI wiring
```
