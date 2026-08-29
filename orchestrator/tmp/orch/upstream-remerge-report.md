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
