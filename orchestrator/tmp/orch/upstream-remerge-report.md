# Upstream re-merge report — 45da6b3e

## Result
- **Merge tip:** `492481b5` (parents: `018d304f` upstream-merge + `622ba52f` fork main)
- **Onto:** fork main `622ba52f`
- **Pushed branch:** `origin/cao/upstream-remerge-45da6b3e` (= 492481b5)
- **Worktree:** `.cao/worktrees/45da6b3e`, branch `cao/45da6b3e` (contained; never touched fork root/root repo)

## Conflicts (1)
- `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt`
  - **Rationale:** machine-generated AST line-map (path:line:symbol) of agent_step.py + auto_responder.py; the three sides differed only because those source files were themselves merged and line numbers shifted. Not a semantic fork. Resolved by **regenerating** the manifest from the merged working tree (`trace_manifest.generate_manifest`), which is the canonical `cao verify manifest --regen` output. Verified byte-exact with generator; 40 hits (matches `test_trace_manifest_is_byte_exact_and_has_36_hits`'s `== 40` assertion). Zero conflict markers remain.
- None of the named fork-patch files (providers/codex.py, services/draft_guard.py, terminal_service.py, api/main.py, session_service.py) conflicted; git auto-merged them cleanly.

## Static checks (laptop) — all PRE-EXISTING baselines, NOT merge-introduced
- `black --check src test`: **FAIL** (300 files "would reformat"). Root cause: `black>=23.0.0` pin resolved to installed **black 26.3.1** (2024 stable style) vs tree last formatted with black 23.x. Proven pre-existing: `sim/world.py` is byte-identical to fork main (`git diff 622ba52f..HEAD` empty) yet flagged. Fixing = reformat 300 files = out-of-scope scope creep that would multiply upstream conflicts (see the `target-version=['py310']` ruling in pyproject.toml).
- `isort --check-only src test`: **FAIL** (same environment/version drift; many flagged files untouched by merge).
- `mypy --config-file mypy.ini src`: **FAIL** (473 errors / 44 files). Dominant class = spurious pydantic "Missing named argument" (e.g. HandoffResult display_name/window_name/resolved_model — all `Optional=Field(None)`, i.e. NOT required). mypy.ini has **no `plugins = pydantic.mypy`**, so this is a tree-wide pre-existing false-positive baseline, not merge breakage.

_None of the three static tools is currently green on fork main itself; the merge did not regress any of them._

## Box suite (grok-box-2)
- **Box:** box@grok-box-2 (acquired via scripts/box-run.sh)
- **Command:** `box-run.sh upstream-remerge -- 'cd ~/cli-subagents/cli-agent-orchestrator && git fetch origin cao/upstream-remerge-45da6b3e && git switch --detach 492481b5 && uv sync && uv run pytest -q -m "not live and not e2e" | tee /tmp/upstream-remerge-run.txt | tail -20'`
- **Exit:** pytest reported summary line; wall 453.91s
- **Counts:** **8 failed, 14531 passed, 57 skipped, 17 xfailed**
- **Log path:** `/tmp/upstream-remerge-run.txt` on box@grok-box-2 (removed on cleanup; counts cited above)

### 8 failures — all PRE-EXISTING (every failing test file is byte-identical to fork main 622ba52f; `git diff 622ba52f..HEAD` empty for each)
1-2. `test_suite_slot.py::TestLedgerSampling` (2) — PID/process ledger sampling; box-process/env.
3. `test_wpdt_delivery_truth.py::...doctrine_arming_section_exists` — expects `~/cli-subagents/doctrine/sections/shared/ws-arming.md`; box repo layout, path/env.
4. `test_sim_substrate.py::...600_virtual_seconds_under_2s` — timing flake (wall 4.36s > 2s budget on loaded box).
5. `test_f254_tier_guard.py::...no_real_io_in_unit_tier` — lint guard (subprocess.run in test_mcp_server.py:43); pre-existing tree state.
6. `test_fixtures_no_personal_pii.py` — PII (`quiye8584@gmail.com`) in `providers/fixtures/status_truth/cline_cli/idle-1.txt`, an **upstream-added** fixture; pre-existing in fork main.
7. `test_g7a_sandbox.py::...fork_context_service.py-display-message` — mutation site not uniquely found; sandbox/env.
8. `test_f497_composition.py::...codex_empirical_reviewer` — golden drift `{'contextPolicy'}`; f497 golden + codex profile inputs unchanged by merge.

## Box-actions ledger (grok-box-2)
- box-run.sh `upstream-remerge`: fetch + `git switch --detach 492481b5` + `uv sync` + pytest (above).
- box-run.sh `upstream-remerge-cleanup`: `git switch --detach 2fd3d25c` (restored box's original long-lived checkout) + `rm -f /tmp/upstream-remerge-run.txt`.
- Raw ssh (read-only): `grep -nE ... /tmp/upstream-remerge-run.txt` to triage failures.
- Left box repo at: **2fd3d25c** (original), clean. Env mutations: `uv sync` (venv only; no lockfile change). Temp files left: none.

## Could-not / notes
- Did not "fix" black/isort/mypy: pre-existing env/config drift, out of scope for a merge and would collide with upstream. Flagged above.
- Suite was run on box only (not locally), per brief.


## Fix-up (post gate r1)

The Codex EMPIRICAL gate (report `/data/cao-scratch/fork-gate-report.md`) measured one
merge-introduced mypy regression: `uv run mypy --config-file mypy.ini src` reported
**BASE 622ba52f = 472 errors / 314 files** vs **HEAD 09a1c07a = 473 errors / 322 files** (+1).

### Root cause — isolated, not guessed
A naive sorted-line diff is dominated by line-number shift noise (upstream added code above
existing errors, so every error line moved). Diffing at the **message level** (line:col
stripped) on one box isolated exactly ONE new diagnostic; the per-file error count confirmed
`clients/database.py` went 11 → 12 while every other file was unchanged.

Exact mypy diff line (verbatim, the +1):

```text
src/cli_agent_orchestrator/clients/database.py:2361: error: Name "_migrate_workflow_plan_approval" already defined on line 2092  [no-redef]
```

The merge duplicated `_migrate_workflow_plan_approval()` — two **byte-for-byte identical**
definitions (both 44-line bodies hash to the same sha256), at line 2092 and line 2361, with a
single call site at line 1501.

### Fix (real fix, not a `# type: ignore`)
Deleted the second, duplicate definition (lines 2359–2404, the def block plus its two leading
blank lines). Zero behavioral change: the functions were identical and the surviving
definition (line 2092) precedes the sole call site. `git diff` = 46 deletions, single file,
`python -m ast` parse OK. Chosen over `type: ignore[no-redef]` because the duplication is a
genuine defect (dead redefinition), and removing it narrows rather than widens the diff.

- File:line fixed: `src/cli_agent_orchestrator/clients/database.py` (removed duplicate def @ 2361; kept @ 2092)

### Box mypy counts (before → after)
Measured with `uv sync --frozen` + `uv run mypy --config-file mypy.ini src` on the offload boxes:

| | errors | files checked | box |
|---|---:|---:|---|
| BASE 622ba52f | 472 | 314 | grok-box-005 (A/B) |
| HEAD 09a1c07a (pre-fix) | 473 | 322 | grok-box-005 (A/B) |
| HEAD a50696ed (post-fix) | **472** | 322 | grok-box-002 (same box the gate used) |

Post-fix HEAD == BASE at 472 errors; the merge-introduced +1 is eliminated. HEAD still checks
322 files (the merge legitimately added 8 upstream files to the checked set) with no net error
increase. The `already defined on line 2092` diagnostic is gone (0 matches).

### Flake counts (test unmodified)
`test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner`
on grok-box-002, `pytest-repeat` available → `--count=20`: **19 passed, 1 failed** (`[20-20]`).
Confirms the known pre-existing flake at its usual low rate; the test was NOT modified.

### New tip
- Fix commit: `a50696ed` — `fixup(upstream-remerge): mypy +1 — clients/database.py duplicate _migrate_workflow_plan_approval`
- Branch `cao/upstream-remerge-45da6b3e` pushed `09a1c07a..a50696ed`
- (New tip after appending this report section is recorded in the callback.)

### Box-actions ledger (fix-up)
- box-run.sh `mypy-ab` (grok-box-005): fetch 622ba52f+09a1c07a, two `git worktree add --detach` snapshots under `/data/cao-scratch/mypy-ab/`, `uv sync --frozen`, `uv run mypy` both sides, `comm` sorted diff. Worktrees + scratch removed at end.
- box-run.sh `mypy-ab2` (grok-box-005): same, with message-level (line:col-stripped) normalized diff + per-file count diff. Worktrees + scratch removed.
- box-run.sh `mypy-head-verify` (grok-box-002): fetch a50696ed, one detached worktree under `/data/cao-scratch/mypy-head-verify/`, `uv sync --frozen`, `uv run mypy` HEAD-only + `--count=20` flake test. Worktree + scratch removed.
- Attempted pin to grok-box-002 for the A/B first; it was held by another lane (`upstream-gate-final`) so the A/B ran on grok-box-005. Cross-box is valid here: both A/B sides ran on the SAME box and mypy is deterministic static analysis, not a timing measurement. Final HEAD verify landed on grok-box-002 (the gate's box).
- Raw ssh: none. Env mutations: `uv sync --frozen` (venv only; no lockfile change). Long-lived box checkouts untouched (grok-box-002 left at 2fd3d25c, grok-box-005 at 44738edf). Temp files left: none (all scratch under `/data/cao-scratch/<label>/` removed; no laptop `/tmp` used).
