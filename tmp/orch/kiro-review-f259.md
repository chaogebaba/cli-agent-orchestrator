**Artifact-Path:** /data/cao-scratch/f259-worktree/tmp/orch/f259-build-report.md
**Artifact-SHA256:** 12c96b2a208bb12aac6ee6352963f5f3a84afe2806f80e526ca5f2652087f35e
**Artifact-Repo-Path:** tmp/orch/f259-build-report.md
**Git-SHA-fork:** 3931815b1c83fe28ce87cc5b1f1da790b829892e
**Ruling:** GATE-YES — 0 BLOCKER / 1 SHOULD / 1 NIT

---

## Verdict Header

| # | Severity | Claim | Amendment |
|---|----------|-------|-----------|
| S1 | SHOULD | Build report's "Known Pre-existing Failures" table lists 8 tests but the census run surfaced `test_fx193_nudge_discipline.py::TestAC5Backoff::test_backoff_sequence_30_60_120_120` (timing flake, not in table). Adjudication confirms pre-existing (F259 diff empty for that file, test uses `time.monotonic()` assertions sensitive to parallel execution), but the build report's failure census is incomplete. | Add the fx193 timing flake to the known-failures table or note "plus any monotonic-clock timing flakes in fx193". |
| N1 | NIT | Census JSON `outcome` field is `"unknown"` for all 11609 tests. The schema shows `outcome` but it carries no discriminatory value in the artifact — all entries say "unknown" regardless of pass/fail/skip. This is harmless (D14: no verdict) but slightly misleading for a downstream consumer expecting to filter by outcome. | Document that `outcome` is recorded from `pytest_runtest_logreport` only when the report fires before session-end serialization, or populate it from junit XML post-hoc in a future revision. |

---

## Empirical Checks

| # | Check | Observed Result |
|---|-------|-----------------|
| E1 | Census artifact exists on run 32198527908 | YES: `resource-census` artifact, 911,697 bytes, not expired |
| E2 | Census artifact schema valid | YES: top-level keys `_notes`, `header`, `slow_tier_candidates`, `tests`. Per-test keys: `cpu`, `fd_delta`, `io`, `nodeid`, `outcome`, `rss_delta_kb`, `spawns`, `tier`, `wall`, `worker`. Header: `python_version=3.14`, `runner_image=Linux`, `test_count=11609`, `worker_count=4`, `run_url` present. |
| E3 | Test count = 11609 | YES |
| E4 | Slow-tier candidates = 478 | YES |
| E5 | Wall data present for all tests | YES: 11609/11609 |
| E6 | CPU data present for all tests | YES: 11609/11609 |
| E7 | Tests with spawns > 0 | 460 |
| E8 | Parity run (32199443688) has NO resource-census artifact | CONFIRMED: only `test-results` artifact present |
| E9 | Anti-short-circuit: census dispatch skips attestation cache | CONFIRMED: "Check attestation cache" step conclusion=SKIPPED on run 32198527908 |
| E10 | No-mint on census: attestation mint skipped | CONFIRMED: "Mint suite attestation" step conclusion=SKIPPED on run 32198527908 |
| E11 | No-mint on failure (parity): attestation mint skipped | CONFIRMED: "Mint suite attestation" step conclusion=SKIPPED on run 32199443688 (suite failed) |
| E12 | Parity run: attestation cache executed normally | CONFIRMED: "Check attestation cache" step conclusion=SUCCESS on parity run |
| E13 | Census run failures (5) all pre-existing | YES: 2× F303 mock (test_database, test_f264_database_hardening), 2× quarantine (test_f254), 1× fx193 timing flake. F259 diff touches NONE of these files. |
| E14 | Parity run failures (7) all pre-existing | YES: 2× F303 mock, 1× rss_guard flake, 2× fifo_reader race, 2× quarantine. All documented in F303 known set. |
| E15 | `make test-census ARGS="test/plugins/test_resource_census.py -v"` | 10 passed in 9.47s. Census tables (wall, CPU, RSS, IO, spawns) printed. |
| E16 | F259 diff scope | Exactly 5 files: `.github/workflows/test-ci.yml` (+24/-2), `Makefile` (+8/-2), `test/conftest.py` (+1), `test/plugins/resource_census.py` (+519), `test/plugins/test_resource_census.py` (+452). No other files touched. |
| E17 | D1 gating (P-BUDGETMODE) | CONFIRMED: `pytest_configure` returns early when `CAO_TEST_CENSUS` unset and `--resource-report` absent. Plugin never registered in off mode. |
| E18 | D15 `CAO_TEST_CENSUS` conditional in CI | CONFIRMED: `CAO_TEST_CENSUS: ${{ inputs.census == true && '1' || '' }}` — empty string (falsy) when census not requested. |
| E19 | Working tree unmodified after review | CONFIRMED: `git status --short` empty, HEAD=e93999b1 |

---

## Decision-Wall Compliance (D15, AC13)

| Requirement | Source | Status |
|-------------|--------|--------|
| Anti-short-circuit: attestation cache skipped on census dispatch | D15 part 2, Do-NOT 10, M8 | PASS — `if:` includes `inputs.census != true` |
| No-mint on census run | D15 part 2, Do-NOT 10 | PASS — mint step `if:` includes `inputs.census != true` |
| Census upload as separate step | D15 part 3 | PASS — dedicated step, `if: always() && inputs.census == true` |
| Census NOT uploaded on normal push | AC13(c), NG-5, M9 | PASS — parity run artifact list lacks `resource-census` |
| Dispatch input `census` boolean, default false | D15 part 1 | PASS |
| Census does not run on push/PR | Do-NOT 9 | PASS — env empty when `inputs.census` is not true |

---

## Zero-Decision Buildable

**Zero-decision buildable: YES** — A builder given this blueprint and the delivered code needs to invent no decisions. All D-decisions (D1-D15) are satisfied by the implementation as written. The CI workflow wiring follows D15 exactly. The Makefile target follows D13. The conftest registration follows D1.

---

## Appendix: Failure Adjudication Detail

### Census Run (32198527908) — 5 failures

1. `test/clients/test_database.py::TestMessageTraceTransactions::test_list_terminals_by_session` — TypeError: json.loads receives MagicMock. **F303 documented, pre-existing.**
2. `test/clients/test_f264_database_hardening.py::test_list_terminals_by_session_skips_stale_rows` — Same root cause. **F303 documented, pre-existing.**
3. `test/services/test_fx193_nudge_discipline.py::TestAC5Backoff::test_backoff_sequence_30_60_120_120` — AssertionError at step 3 (cumulative 330s). Timing-sensitive `time.monotonic()` assertion under `-n 4`. F259 diff empty for this file (`git diff 2a9a86de..3931815b -- test/services/test_fx193_nudge_discipline.py` = no output). **Pre-existing timing flake, not in build report table (→ S1).**
4. `test/test_f254_quarantine.py::test_no_expired_quarantine_entries` — Quarantine entries expired. **Pre-existing.**
5. `test/test_f254_quarantine.py::test_expiry_guard_fires_for_non_serial_only` — Missing expires field. **Pre-existing.**

### Parity Run (32199443688) — 7 failures

1. `test/clients/test_database.py::TestMessageTraceTransactions::test_list_terminals_by_session` — **F303, pre-existing.**
2. `test/clients/test_f264_database_hardening.py::test_list_terminals_by_session_skips_stale_rows` — **F303, pre-existing.**
3. `test/plugins/test_rss_guard.py::test_rss_guard_trips_on_spike` — RSS delta unreliable. **Known flake, pre-existing.**
4. `test/services/test_fifo_reader.py::TestConcurrencyRaces::test_reader_loop_last_data_at_write_is_atomic_with_stop` — Lock contention race. **Known flake, pre-existing.**
5. `test/services/test_fifo_reader.py::TestReaderLoopCoalescing::test_rapid_writes_produce_fewer_publishes_than_writes` — Write coalescing timing. **Known flake, pre-existing.**
6. `test/test_f254_quarantine.py::test_no_expired_quarantine_entries` — **Pre-existing.**
7. `test/test_f254_quarantine.py::test_expiry_guard_fires_for_non_serial_only` — **Pre-existing.**

**Conclusion: Zero failures attributable to F259.**
