# Upstream-Merge Regression Fix Build Report — realserver workflow 404 + xdist_group marker

Branch: `cao/26643eb7` (based at `af15b36d`, worktree
`.cao/worktrees/26643eb7/cli-agent-orchestrator`)
Baseline for diff: `4e873cfd`

## Summary

Two independent defects on the upstream-merge branch, both fixed test-side, zero
production-source changes:

1. **realserver 404** — `test/api/test_workflow_lifecycle_realserver.py` submitted a
   script spec that the spawned real `cao-server` could not resolve, returning
   `404 {"detail":"unknown workflow 'rs_fast_...'"}` on submit. 4 tests failed
   (`composed_flow_over_real_http_script_tier`, `run_listing_over_real_http`,
   `cancel_over_real_http`, `detached_run_answerable_over_real_http`).
2. **xdist_group INTERNALERROR** — collection under `-p no:xdist` (xdist plugin
   disabled) raised `pytest.PytestUnknownMarkWarning: Unknown pytest.mark.xdist_group`,
   which `filterwarnings=error` + `--strict-markers` promoted to a collection
   INTERNALERROR (4 errors).

## Root Cause

### Defect 1 — NOT a workflow-wiring regression (brief theory corrected)

The brief's hypothesis was "workflow registration/route wiring lost in the merge —
patches in `api/main.py` and `session_service.py`." **This is incorrect.** The
`4e873cfd..af15b36d` diff touches **no** workflow source at all:

```
git diff --name-only 4e873cfd af15b36d -- \
  src/cli_agent_orchestrator/api/main.py \
  src/cli_agent_orchestrator/services/workflow_*.py \
  src/cli_agent_orchestrator/services/session_service.py \
  src/cli_agent_orchestrator/constants.py
# → (empty)
```

The only workflow-relevant files the merge changed are the **test harness**:
`test/api/test_workflow_lifecycle_realserver.py` (the `_spec_dir` helper) and
`test/conftest.py` (`_hermetic_cao_env`).

The merge rewrote `_spec_dir` to:

```python
if os.environ.get("CAO_HOME_DIR", "").strip():
    return WORKFLOW_SPEC_DIR
return server.home_dir / WORKFLOW_SPEC_DIR.relative_to(CAO_HOME_DIR.parent.parent)
```

`test/conftest.py:78` sets `os.environ["CAO_HOME_DIR"] = /tmp/cao-pytest-*` in the
**test process**, so `constants.WORKFLOW_SPEC_DIR` (import-time home-derived) resolves
under that pytest tmp dir. The test therefore wrote the script spec into the *test
process's* `CAO_HOME_DIR/workflows`.

But the `cao_server` fixture's `_subprocess_env` (`test/fixtures/cao_server.py`)
**deliberately strips** `CAO_HOME_DIR` and `CAO_HOME` from the child env and redirects
`HOME` to `server.home_dir`. The subprocess therefore resolves `WORKFLOW_SPEC_DIR`
under its *own* redirected `$HOME` = `server.home_dir/.aws/cli-agent-orchestrator/
workflows`. Spec written in one directory, subprocess reads another → the index
rebuild finds nothing → `KeyError` → `404 unknown workflow`.

The baseline `_spec_dir` was correct precisely because it always wrote to the
subprocess's redirected-`$HOME` location.

### Defect 2 — mark provided only by the plugin

`test/plugins/quarantine.py:107` calls `item.add_marker(pytest.mark.xdist_group(...))`.
`xdist_group` is registered by `pytest-xdist` **only when the plugin is loaded**. Under
`-p no:xdist` it is unknown; with `--strict-markers` + `filterwarnings=error` the
resulting `PytestUnknownMarkWarning` becomes a hard collection error.

## Changes

| File | Change |
|------|--------|
| `test/api/test_workflow_lifecycle_realserver.py` | Reverted `_spec_dir` to the baseline `server.home_dir / ".aws" / "cli-agent-orchestrator" / "workflows"`; removed now-unused imports (`os`, `CAO_HOME_DIR`, `WORKFLOW_SPEC_DIR`); documented why the redirected-`$HOME` path is the only correct target. |
| `pyproject.toml` | Registered `xdist_group(name)` in `[tool.pytest.ini_options].markers` so collection is clean whether or not the xdist plugin is loaded. |

Net: 2 files, +13 / −11.

## Design Decisions

1. **Fix the harness, not the source.** The empirical diff proves the workflow
   service/route wiring is untouched by the merge. Adding source "wiring" would have
   been a spurious change chasing a wrong theory; the correct fix is to make the test
   write where the subprocess actually reads.
2. **Revert to baseline `_spec_dir` rather than teach it about the stripped env.** The
   fixture's documented contract is that the subprocess always uses the redirected
   `$HOME` (it strips `CAO_HOME_DIR`/`CAO_HOME`). The baseline expression encodes that
   contract directly and is the minimal, intention-revealing fix.
3. **Register `xdist_group` in `markers`, not via a conftest shim.** This is the
   upstream-documented convention for a mark that must be known independent of plugin
   load order. It coexists harmlessly with the plugin's own registration (the plugin's
   definition wins when loaded).
4. **Did NOT touch the load-bearing `addopts` contract** (`-n 2 --dist loadgroup
   --strict-markers`, F254 D20–D22).

## Known / Disclosed

- The **literal** command `uv run pytest -p no:xdist --collect-only -q` still exits 4
  with a pytest **usage** error (`unrecognized arguments: -n --dist`), because default
  `addopts` hardcodes `-n 2 --dist loadgroup` and those options vanish with the plugin.
  This is **pre-existing** (identical at baseline `4e873cfd`) and is a *usage* error,
  **not** an INTERNALERROR. The INTERNALERROR the brief targets reproduces when the
  xdist-only addopts are neutralized (the realistic way to disable xdist), and that is
  now fully fixed. Making the literal invocation succeed would require gating the
  xdist-only addopts — a change to the F254 addopts contract that affects every test
  run and is out of scope here.

## Test Results (local, worktree; `uv run pytest`)

```
# Acceptance #1 — 4 realserver tests, -n0 --dist=no
uv run pytest -n0 --dist=no \
  test/api/test_workflow_lifecycle_realserver.py::test_composed_flow_over_real_http_script_tier \
  test/api/test_workflow_lifecycle_realserver.py::test_run_listing_over_real_http \
  test/api/test_workflow_lifecycle_realserver.py::test_cancel_over_real_http \
  test/api/test_workflow_lifecycle_realserver.py::test_detached_run_answerable_over_real_http
→ 4 passed in 9.52s

# Full realserver file (default -n 2 --dist loadgroup)
uv run pytest test/api/test_workflow_lifecycle_realserver.py
→ 6 passed, 1 skipped in 14.68s   (skip = disclosed YAML/agent-tier deferral)

# Acceptance #2 — collection with xdist DISABLED (addopts neutralized), no INTERNALERROR
uv run pytest -p no:xdist -o addopts="--strict-markers -p no:randomly" --collect-only -q
→ 14248 tests collected in 11.58s   (BEFORE fix: 4 errors / INTERNALERROR)

# Collection with xdist ENABLED (default) — sanity
uv run pytest --collect-only -q
→ 14248 tests collected

# Acceptance #3 — targeted regression for touched files
uv run pytest test/plugins/    # quarantine/serial_only machinery (pyproject markers)
→ 148 passed, 1 xfailed in 39.67s

# Lint
uv run black --check --line-length 100 test/api/test_workflow_lifecycle_realserver.py  → unchanged
uv run isort --check-only --profile black --line-length 100 test/api/test_workflow_lifecycle_realserver.py  → OK
```

Full-suite verification deferred to a box per the brief (not run locally). No merge
performed (F244).


---

# Round 2 — Full-Suite Verdict Remediation (23 failed / 14005 passed on 8dab9fcb)

Supervisor's baseline A/B attributed the 23 full-suite failures: 9 merge regressions,
12 merge-added failing params, 2 pre-existing (not mine). In-scope: 21. This round fixes
the 2 real code/test clusters and empirically classifies the 2 flake clusters.

## Cluster 1 — tmux copy-mode cancel vs g7a guards (6 in-scope: 1 send_keys test + 5 g7a)

**Root cause.** The merge added upstream's #654 copy-mode punch-through: a
`send-keys -X cancel` before the paste and again before each submitting Enter, so a
wheel-scrolled pane sitting in copy mode cannot eat the delivery/submission. Two
fallouts:

- `TestSendKeys::test_force_bracketed_paste_uses_tmux_wrapping` asserted against the raw
  `mock_subprocess.run.call_args_list`, whose indices shifted once the leading cancel
  became `calls[0]`. The merge added a `payload_calls()` helper (filters out the
  `-X cancel` guards so the paste pipeline is asserted at its pre-guard indices) and
  updated the sibling `test_basic_message` to use it — **but missed this test.**
- The merge wrote the two new cancel calls as **raw** `["tmux", "send-keys", …, "-X",
  "cancel"]` `subprocess.run` literals. The g7a AST guard (`test_tmux_ast_guard_is_closed`)
  forbids any raw `["tmux", …]` argv literal outside a 3-file allowlist — every tmux
  call must route through `tmux_argv()` (the G7A closed-world socket choke point). The
  raw literals both tripped the closed-world guard AND broke the 4 legacy mutation-kill
  params (whose invariant is "the un-mutated tree has zero raw tmux sites, so mutating
  exactly one site yields exactly one new violation" — a pre-existing raw site makes the
  count arithmetic fail).

**Fix (invariant-preserving, guards NOT weakened).**
- `test/clients/test_tmux_send_keys.py`: `test_force_bracketed_paste_uses_tmux_wrapping`
  now uses `payload_calls()` exactly as `test_basic_message` does. The behavioral
  invariant it pins is unchanged: raw content (no `\x1b`) through `paste-buffer -p`, `-p`
  present, `-r` absent.
- `src/cli_agent_orchestrator/clients/tmux.py`: both new cancel sites rewritten from raw
  literals to `tmux_argv("send-keys", "-t", target, "-X", "cancel")` — the SAME choke
  point every other tmux call in the file already uses. This satisfies the g7a guard by
  *honoring* its invariant (all tmux exec routes through `tmux_argv`), not by silencing
  it. The `pane.cmd("send-keys", "-X", "cancel")` libtmux calls in the other paste paths
  are unaffected (the guard only flags `subprocess.*`/`return` with `["tmux", …]`
  literals, not libtmux `pane.cmd`).

## Cluster 4 — grok_cli added to the shared native-status matrix (12 in-scope params)

**Root cause.** Upstream added `grok_cli` to the `TestSharedNativeStatus` provider
matrix. Two defects surfaced:

1. `GrokCliProvider.__init__` → `_allocate_session_uuid()` calls
   `get_backend().get_pane_working_directory(...)`. In the matrix's mocked-backend unit
   context that returns a `MagicMock` (truthy → the `or os.getcwd()` fallback does not
   fire), then `quote(cwd, safe="")` raises `TypeError` **outside** the `try/except`, so
   every one of the 12 params died at construction.
2. The param declared grok's provider-flag as `"_turns"`, copied from `antigravity_cli`.
   Grok has **no** `_turns` attribute — it tracks dispatch via `self._input_received`
   (a bool set True in `_after_dispatch_commit_locked`, invoked by base
   `mark_input_received → _commit_dispatch_locked`). So the 12th param
   (`test_mark_input_received_sets_dispatch_flags`) `AttributeError`'d.

**Fix.**
- `src/cli_agent_orchestrator/providers/grok_cli.py`: `_allocate_session_uuid` now guards
  `if not isinstance(cwd, str): cwd = os.getcwd()` before `quote()`. This is a genuine
  robustness fix — the method already *intended* to fall back to the real cwd; a non-str
  backend return (mock, or a future Path-returning backend) now falls back instead of
  raising. Real behavior is unchanged (the real backend returns a path string).
- `test/providers/test_native_status_shared.py`: corrected the grok param flag
  `"_turns"` → `"_input_received"` (the supervisor authorized "fix the provider OR the
  param wiring"). The matrix now asserts grok's *actual* dispatch-tracking contract.

## Clusters 2 & 3 — SUSPECTED BOX-EXECUTION FLAKES (4 nodeids, no code change)

Both authorized-for-flake by the supervisor ("verify locally; if it passes repeatedly,
mark as suspected box-perf flake instead of code-fixing"). Empirical basis:

- **`git diff 4e873cfd..8dab9fcb` touches ZERO of the relevant files** —
  `test/plugins/{suite_slot,test_suite_slot}.py` and `test/simulation/**` +
  `src/cli_agent_orchestrator/sim/**` all have an empty diff-stat. A merge cannot cause a
  content-regression in files it did not change.
- **Cluster 2** `TestLedgerSampling::{test_sample_records_child_process,
  test_sample_ledger_monotonic_growth}`: spawn a child and assert `_sample_ledger`
  records it by scanning the process group. Pass 3/3 in isolation (`-n0`) and the full
  `test_suite_slot.py` passes 44/44 under default parallel addopts. Under a loaded
  full-suite box run the pgid scan / process-accounting timing can race.
- **Cluster 3** `TestAC4VirtualTimeIsFree::test_600_virtual_seconds_under_2_real_seconds`:
  a pure wall-clock budget assertion (`wall_elapsed < 2.0`). Passes 5/5 locally in
  0.72–1.27s; a CPU-saturated box can exceed 2s.

## Out-of-scope, NOT a regression (observed while running the g7a file)

`test/test_g7a_sandbox.py::test_mcp_identity_forced_and_overrides_rejected` fails **only
inside a live CAO terminal**: `CAO_TERMINAL_TOKEN` is present in this worker's ambient
env (len 43) and the test does not `monkeypatch.delenv` it, so
`resolve_mcp_server_config` injects it into the resolved env and the exact-dict assertion
fails. Passes under `env -u CAO_TERMINAL_TOKEN` (verified). Not in the in-scope 21, not
touched by the merge, box has a clean env. Left alone.

## Round-2 Changes

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/clients/tmux.py` | 2 new copy-mode cancel sites routed through `tmux_argv()` instead of raw `["tmux", …]` literals (honors g7a closed-world invariant) |
| `test/clients/test_tmux_send_keys.py` | `test_force_bracketed_paste_uses_tmux_wrapping` uses `payload_calls()` (matches the merge's own `test_basic_message` reconciliation) |
| `src/cli_agent_orchestrator/providers/grok_cli.py` | `_allocate_session_uuid`: `cwd` type-guard → fall back to `os.getcwd()` for non-str backend returns |
| `test/providers/test_native_status_shared.py` | grok_cli matrix flag `"_turns"` → `"_input_received"` (grok's real dispatch-tracking attr) |

Net: 4 files, +20 / −4.

## Round-2 Test Results (local worktree, `uv run pytest -n0 --dist=no`)

```
# All 21 in-scope tests
9 non-grok in-scope nodeids (bracketed-paste + 5 g7a + 2 ledger + sim) → 9 passed
12 test_native_status_shared[grok_cli] params                          → 12 passed
  == 21/21 in-scope PASS

# No collateral damage
test/clients/test_tmux_send_keys.py + test/clients/test_tmux_client.py → 130 passed
test/providers/test_native_status_shared.py (full matrix)              → 156 passed
grok provider suite (9 files)                                          → 122 passed, 1 xpassed
test/plugins/test_suite_slot.py (default parallel)                     → 44 passed

# Round-1 fixes still hold
test/api/test_workflow_lifecycle_realserver.py                         → 6 passed, 1 skipped

# Lint (all 4 touched files)
black --check --line-length 100   → unchanged
isort --check-only --profile black → OK
```

No g7a guard was weakened — the tmux invariant is preserved by routing through
`tmux_argv`, and its self-test + all 4 mutation-kill params pass. Full-suite box rerun
follows. No merge (F244).


---

# Round 3 — Post-Box Adjudication (6 failed / 14022 passed on b0de6f6a)

The box full-suite at b0de6f6a: all 21 round-1/2 in-scope fixes held. The 3 known
flakes flipped as predicted (2× `TestLedgerSampling` pgid, 1× `test_600_virtual_seconds`
at 4.38s). Three NON-flaky failures remained; adjudicated below.

## #1 — test_fixtures_no_personal_pii — FIXED via `origin/main` merge

Not a defect on this branch: the personal email (`quiye8584@gmail.com`) lived in 9
`test/fixtures/codex_dialogs/*.ansi.txt` frames, and the F534 scrub (fork main
`030b166c`, merge `8c0493c0`) landed on `main` AFTER this branch forked at `4e873cfd`.

**Fix:** merged `origin/main` (`8c0493c0` — F534 PII scrub + F524 `33c15128`) into
`cao/26643eb7`. The 3-way auto-merge completed with **ZERO conflicts**: this branch's
r1/r2/r3 edits and main's F524/F534 touched disjoint lines.

Post-merge verification:
- `quiye8584@gmail.com` scrubbed from all 9 fixtures; `test_fixtures_no_personal_pii`
  passes.
- Every prior fix survived intact: r1 `_spec_dir` baseline form; r2's two
  `tmux_argv(...-X cancel)` sites; r2 grok flag `_input_received`; round-1 `xdist_group`
  marker in `pyproject.toml`; this report.
- Merge-brought code is coherent here: new `test_f524_direct_delivery_stall.py` → 11
  passed; `inbox_service` suite → 347 passed; collect-only (xdist disabled) → 14259
  collected, no INTERNALERROR.

## #2 — test_worker_terminal_cap ... test_thread_race_barrier_inside_listing — BOX FLAKE

**Adjudication: load-induced timing flake, NOT a regression, NOT caused by the r2
`tmux_argv` change.**

- `test/services/test_worker_terminal_cap.py` AND `src/.../fifo_reader.py` are
  **untouched** by the merge (`4e873cfd..8dab9fcb` empty diff) AND by the r2 diff
  (`8dab9fcb..b0de6f6a` touches only `clients/tmux.py` [two cancel call-shapes],
  `grok_cli.py`, and two test files). The failing code path is byte-identical at both
  commits → behavior identical by construction. The r2 change swapped
  `["tmux",…,"-X","cancel"]` for `tmux_argv(…,"-X","cancel")`, which is the same argv
  when no socket name is set — and touches `send_keys`, not the worker-admission
  pipe-pane forwarder.
- The test rendezvouses 4 threads on `barrier.wait(timeout=10)`. Under saturation a
  thread's pipe-pane forwarder (`fifo_reader.py:1166`) starts late and one thread misses
  the 10 s window → `BrokenBarrierError`. A wall-clock-bounded thread rendezvous is
  inherently load-sensitive.
- Empirical: **10/10** isolated (`-n0`); the single nodeid **8/8** under `-n2`; the full
  `TestConcurrentAdmission` class under `-n2` was **5/6** (the one failure stalled to 15 s
  vs the ~3 s norm — the load signature, not a logic error).
- The test's own line-805 comment calls `BrokenBarrierError` "the r2 bug" — that names a
  **historical F439-round-3** bug this test guards against, not this task's round 2. A
  coincidental label collision, easy to misread as implicating the r2 change; it does
  not.

**No code change** — raising the barrier timeout would weaken a real concurrency guard.

## #3 — test_wpdt ... test_doctrine_arming_section_exists — ROOT-REPO ENVIRONMENT

`_root_repo()` resolves to `fork_root.parent` — the **outer `cli-subagents` repo**, a
SEPARATE repo from this fork. The test asserts
`doctrine/sections/shared/ws-arming.md` exists there and only `pytest.skip`s when the
`doctrine/` **directory** is absent. On the box the stale root checkout has the directory
but not the file, so the skip does not fire and the assertion fails.

`ws-arming.md` is **not tracked anywhere in the fork repo** (no branch, not `origin/main`,
no `doctrine/` dir in the fork) — it is exclusively a root-repo artifact this fork branch
cannot create or fix. It fails locally too, for the identical root-repo-staleness reason
(my local `cli-subagents` checkout also lacks the file). **Box/root-repo environment; no
fork code change; test not weakened** (matches the supervisor's pre-diagnosis).

## Round-3 Changes

| Change | Detail |
|--------|--------|
| Merge `origin/main` (`8c0493c0`) into `cao/26643eb7` | F534 PII scrub of 9 codex_dialogs fixtures + F524; zero conflicts |
| (report) | this round-3 section |

No fork source/test edits this round — #1 is resolved by the merge, #2 and #3 are
adjudicated non-defects.

## Round-3 Test Results (local worktree, merged tree, `uv run pytest -n0 --dist=no`)

```
test_fixtures_no_personal_pii.py                       → 1 passed  (was: 1 failed / 9 fixtures)
6 tmux/g7a in-scope + realserver + PII                 → 13 passed, 1 skipped
test/providers/test_native_status_shared.py (matrix)   → 156 passed
test/services/test_f524_direct_delivery_stall.py (new) → 11 passed
inbox_service suite (merge touched inbox_service.py)   → 347 passed
collect-only, xdist disabled                           → 14259 collected, no INTERNALERROR

# Flake adjudication (#2)
barrier test isolated -n0                              → 10/10 passed
barrier nodeid -n2                                     → 8/8 passed
full TestConcurrentAdmission class -n2                 → 5/6 (1 load stall @15s)

black --check / isort --check-only (r-touched files)   → clean
```

Full-suite box rerun follows; the 4 flake nodeids (#2 + clusters 2/3) may still flip
under load — not code-fixable without perf/harness changes, which were not made. No merge
to main (F244 — supervisor runs the gate).
