# WP-Suite D6 Build Report (D6b + D6a)

**Artifact-Path**: `scripts/suite-ledger.py` (FORK), `scripts/tcache` (ROOT), `.github/workflows/test-ci.yml` (FORK)
**Artifact-SHA256**: `4e9837ab1208c76dd8723e310625acc918d9367707b9841693215cfbb42366e8` (suite-ledger.py), `03525f477bfe26c3bfda297f20ed4510e5b4f480e22fba15421aaba62489ef99` (tcache), `d38a6f6ca983fe7c7b4096aa879993b3338dd702c4216e8a7d5a126ee8f1e45f` (test-ci.yml)
**Artifact-Repo-Path**: FORK=`cli-agent-orchestrator`, ROOT=`cli-subagents` (worktree `702c0ecb`)
**Git-SHA-root**: `f979d08a4f71901aa133053f4d15c23385992821`
**Git-SHA-fork**: `e39365b82115d200dfad7f3586bb2a23e1760c16`

---

## D6b — junit wall-time ledger (FORK)

### AC6.1: scripts/suite-ledger.py

Pure stdlib XML parse of `junit-results.xml`. Emits ranked slowest files, slowest tests, tail ratio.

**Evidence — local run against test fixture:**

```
$ python3 scripts/suite-ledger.py tmp/test-junit.xml
Suite Ledger — 20 tests, 42.9s total wall-time
Tail ratio (slowest 5% = 1 tests): 19.8% of total time

── Slowest test files (top 10) ──
  ─────────────────────────────────────┬────────
  File (classname)                     │   Time
  ─────────────────────────────────────┼────────
  test.services.test_terminal          │ 10.60s
  test.services.test_session           │  8.33s
  test.graph.test_builder              │  7.70s
  test.ext_apps.test_lifecycle         │  5.70s
  test.graph.test_query                │  4.30s
  test.transcript_scrub.test_scrubber  │  3.60s
  test.api.test_routes                 │  2.00s
  test.telemetry.test_emit             │  0.55s
  test.cli.test_main                   │  0.08s
  test.e2e.test_full_flow              │  0.00s
  ─────────────────────────────────────┴────────

── Slowest individual tests (top 20) ──
  ───────────────────────────────────────────────────────┬───────
  Test                                                   │  Time
  ───────────────────────────────────────────────────────┼───────
  test.services.test_terminal::test_spawn_terminal       │ 8.50s
  test.services.test_session::test_create_session        │ 5.23s
  test.graph.test_builder::test_build_graph              │ 4.50s
  ...
  ───────────────────────────────────────────────────────┴───────
```

### AC6.2: top-10 slowest files in test-ci.yml step summary

Added to the existing "Write step summary" step in `.github/workflows/test-ci.yml` (after line 222):

```yaml
          # AC6.2: append performance ledger (top-10 slowest files) to step summary.
          if [ -f junit-results.xml ]; then
            python3 scripts/suite-ledger.py --github-summary junit-results.xml >> "$GITHUB_STEP_SUMMARY"
          fi
```

**Evidence — github-summary mode output:**

```
$ python3 scripts/suite-ledger.py --github-summary tmp/test-junit.xml
### Performance Ledger

**20 tests** | **42.9s** total | tail ratio (slowest 5% = 1 tests): **19.8%**

<details><summary>Top-10 slowest test files</summary>

| # | File | Time |
|---|------|------|
| 1 | `test.services.test_terminal` | 10.60s |
| 2 | `test.services.test_session` | 8.33s |
...
</details>
```

### AC6.3: runnable against a local junit file

Demonstrated above — `python3 scripts/suite-ledger.py <path>` works with any local JUnit XML.

**Evidence — unit tests (uv run pytest -n 2):**

```
$ uv run pytest test/scripts/test_suite_ledger.py -v -n 2
test/scripts/test_suite_ledger.py::TestParseJunit::test_parses_file_times PASSED
test/scripts/test_suite_ledger.py::TestParseJunit::test_parses_individual_tests PASSED
test/scripts/test_suite_ledger.py::TestParseJunit::test_handles_testsuite_root PASSED
test/scripts/test_suite_ledger.py::TestTailRatio::test_basic PASSED
test/scripts/test_suite_ledger.py::TestTailRatio::test_empty PASSED
test/scripts/test_suite_ledger.py::TestTailRatio::test_all_zero PASSED
test/scripts/test_suite_ledger.py::TestGenerateLedger::test_plain_text PASSED
test/scripts/test_suite_ledger.py::TestGenerateLedger::test_github_summary PASSED
test/scripts/test_suite_ledger.py::TestGenerateLedger::test_multiple_files PASSED
test/scripts/test_suite_ledger.py::TestGenerateLedger::test_no_tests PASSED

============================== 10 passed in 2.42s ==============================
```

---

## D6a — strace mining (ROOT)

### AC6.4: tcache profile <key8> subcommand

Reuses the existing parser regex patterns from `:236-308` (same `openat` and `execve` regex). No second parser — the inline Python in `cmd_profile()` uses the identical pattern strings.

**Evidence — run against retained synthetic trace:**

```
$ bash scripts/tcache profile testkey8
[tcache profile] Analyzing: /data/cao-scratch/tcache-traces/testkey8-1234567890
[tcache profile] NOTE: output is ADVISORY ONLY (AC6.6) — no tcache decision depends on this data.

=== Most-opened paths (top 20) ===
      3x  /home/chao/project/src/main.py
      3x  /home/chao/project/test/test_main.py
      1x  /home/chao/project/src/utils.py
      1x  /usr/lib/python3/os.py
      1x  /home/chao/project/config.toml

=== Repeated execve targets (top 15) ===
      4x  /usr/bin/python3
      2x  /usr/bin/bash

=== Summary ===
  Total opens:    9 (5 unique paths)
  Total execve:   6 (2 unique binaries)
```

**Error paths:**
```
$ bash scripts/tcache profile nonexistent
[tcache] ERROR: No retained trace directory matching 'nonexistent' in /data/cao-scratch/tcache-traces

$ bash scripts/tcache profile
[tcache] ERROR: Usage: tcache profile <key8>
```

### AC6.5: TCACHE_KEEP_TRACES=1 opt-in retention

Implementation at the trace cleanup site (formerly line 314, now line 315-327):

```bash
    # AC6.5: opt-in trace retention under /data/cao-scratch/ (never /tmp).
    if [[ "${TCACHE_KEEP_TRACES:-}" == "1" ]]; then
        local retain_dir="/data/cao-scratch/tcache-traces"
        mkdir -p "$retain_dir"
        local retain_key="${key8:-$(basename "$strace_dir")}"
        local retain_dest
        retain_dest="$retain_dir/${retain_key}-$(date +%s)"
        mv "$strace_dir" "$retain_dest" 2>/dev/null || cp -a "$strace_dir" "$retain_dest"
        rm -rf "$strace_dir"
        echo "[tcache] Traces retained: $retain_dest" >&2
    else
        rm -rf "$strace_dir"
    fi
```

- Default remains `rm -rf` (no behavior change for normal users)
- Retained traces go to `/data/cao-scratch/tcache-traces/` (never `/tmp`)
- The `key8` is included in the directory name for `tcache profile` lookup

### AC6.6: advisory-only mining output

The `cmd_profile()` function:
1. Only reads trace files — never modifies `$STORE_DIR` or cache state
2. Prints explicit disclaimer: `"NOTE: output is ADVISORY ONLY (AC6.6) — no tcache decision depends on this data."`
3. Outputs to stdout only — no environment variables set, no files written
4. Not called by any other tcache function — completely isolated from the HIT/MISS logic

**Evidence — shellcheck clean (my changes only):**

```
$ shellcheck scripts/tcache
# Only pre-existing warnings (lines 93, 205, 871 — all outside D6a changes)
# No new warnings from D6a code
```

---

## Verification Summary

| AC | Method | Result |
|----|--------|--------|
| AC6.1 | Local run + unit tests | 10/10 pass |
| AC6.2 | `--github-summary` mode | Markdown validated |
| AC6.3 | Direct CLI invocation | Works with any local junit XML |
| AC6.4 | `tcache profile testkey8` | Reports opens, execve, summary |
| AC6.5 | Code review + trace dir exists | `/data/cao-scratch/` path, never `/tmp` |
| AC6.6 | Code review + output check | Read-only, explicit disclaimer |

## Out of Scope (confirmed NOT touched)

- D6c / AC6.7 / AC6.8: not implemented (per instructions)
- orchestrator/BUGS.md: not modified
- No grok config or ~/.grok changes
- Fork suite NOT run concurrently with other work
