# F497 Phase 1 — build report (position/provider decoupling, #352)

- Blueprint: `orchestrator/blueprints/f497-position-provider-decoupling.md` (FROZEN r4)
- Worktree: `cli-agent-orchestrator/.cao/worktrees/6ea01470`
- Branch: `cao/6ea01470` (pushed to origin)
- HEAD: `a40b35b6dca2dd2b39470eead02aba0ae8f2c92d` (was f82566a1; +schema parity fix)
- Scope: migration step 1 ONLY — resolver (D2) + new model field (D6) + AC1 diff
  harness + AC2 fail-closed install guard. NO extraction, NO assign changes,
  NO install.sh / ROOT `profiles/` edits. Stops at the redeploy→verify-AC1
  boundary (redeploy is the supervisor's).

## What landed

### 1. Resolver-internal model field (D6) — `models/agent_profile.py`
- Added `position: Optional[str] = None` between `provider` and `system_prompt`.
- It is the resolver-internal composition axis (D6). For a legacy profile that
  declares no `position:`, it stays `None`, so the composed profile is
  byte-identical to today's direct parse (AC1).
- `extends` is deliberately NOT a model field — it is a resolver meta-key (D5),
  listed in `_RESOLVER_META_KEYS` and stripped before construction.

### 2. Resolver seam ABOVE the provider layer (D2/D5) — `utils/agent_profiles.py`
- New `resolve_agent_profile(resolved_text, profile_name)` — the single
  composition point feeding `load_agent_profile` (D2), so `profile.name`
  composition is computed once, not fanned into the four provider modules.
- Phase 1 contract:
  - No composition key present → delegates to `parse_agent_profile_text`
    verbatim → byte-identical (AC1). This is the whole corpus today.
  - `extends:`/`position:` present → raises `ValueError` ("... Phase 2 ...").
    The merge engine (D5's six key-classes) is Phase 2; letting a
    composition-bearing profile through in Phase 1 would boot a worker with an
    empty persona and no error — the exact cross-release hazard the migration
    ordering closes. This raise is a fail-closed backstop; `cao install`
    refuses such profiles upstream (AC2).
- `load_agent_profile` now routes through `resolve_agent_profile` (same
  `resolve_env_vars` → parse flow as before).
- Added `PROFILE_COMPOSITION_KEYS = ("extends", "position")`,
  `_RESOLVER_META_KEYS = ("extends", "_replace", "position", "providers")`,
  and `profile_declares_composition(metadata)` (shared by resolver + AC2 guard).

### 3. Server capability advertisement (AC2) — `api/main.py`
- `/health` now returns `capabilities: { profile_resolver: True }`. A server old
  enough to lack this key reports nothing there; the install probe treats
  missing/false as "no resolver support" and fails closed.

### 4. AC2 fail-closed install guard — `utils/resolver_probe.py` + `services/install_service.py`
- `resolver_probe.py`: `server_supports_resolver()` queries the RUNNING
  cao-server's `/health` via the existing `cao_http` client and returns True
  ONLY on a 200 with `capabilities.profile_resolver == True`. Every uncertain
  path (unreachable, non-200, malformed body, missing/false flag) returns False
  — never raises. `resolver_probe_skipped()` honours `CAO_SKIP_RESOLVER_PROBE`.
- `install_agent()`: after env-resolving the profile source, if the profile
  declares a composition key it must pass the live probe; otherwise the install
  is REFUSED with a clear message. Legacy profiles never probe. The
  `CAO_SKIP_RESOLVER_PROBE=1` escape covers no-server environments (offload
  boxes, cold bring-up) by design.
- This mechanizes migration ordering: the guard is aimed at the RUNNING server
  (composition is server-side at spawn), not the CLI, exactly as the blueprint
  specifies. `install.sh` in the ROOT repo was NOT touched (HARD STOP); if a
  later phase wants the `install.sh` ordering note, see "Notes for later phases".

## Tests

### AC1 diff harness — `test/utils/test_f497_resolver.py`
- Parametrized over every `.md` in the orchestrator `profiles/` corpus,
  discovered via `CAO_F497_PROFILES_DIR` env override or a worktree-safe walk to
  a sibling `profiles/` dir (skips, does not fail, when the corpus is absent —
  portable to a bare checkout).
- For each profile: env-resolve ONCE, then assert
  `resolve_agent_profile(text) == parse_agent_profile_text(text)` and
  `.model_dump()` equality (field-for-field byte-identical).
- **AC1 RESULT — all 22 legacy profiles pass byte-identically:**
  ```
  chao_supervisor  claude_blueprint_maker  claude_blueprint_maker_tester
  claude_design_reviewer  cline_dev  codex_base  codex_design_reviewer
  codex_dev  codex_empirical_reviewer  developer-opus  developer-sonnet
  grok_base  grok_dev  grok_doc_keeper  grok_oracle  grok_reviewer
  grok_tester  kiro_design_reviewer  kiro_dev  kiro_oracle  kiro_reviewer
  secretary
  ```
  (22 parametrized cases collected + green; corpus-presence guard asserts >=20.)

### AC2 refusal tests — `test/services/test_f497_install_guard.py`
- Probe: True on advertised support; False on no-support / missing key /
  unreachable / non-200 / malformed body; True under the skip escape (no network
  touch); env truthiness matrix.
- `install_agent` integration: refuses a composed profile when the server lacks
  support; refuses when unreachable (fail-closed); allows under
  `CAO_SKIP_RESOLVER_PROBE=1`; legacy profile never probes.

### Local (worktree) verification — all green
- New suites: `test/utils/test_f497_resolver.py` + `test/services/test_f497_install_guard.py`
  → **50 passed**.
- Regression: `test/utils/test_agent_profiles.py` + `test/services/test_install_service.py`
  + `test/api/test_api_endpoints.py` → **211 passed**; combined re-run after
  formatting → **144 passed** (agent_profiles + install_service + both new).
- `mypy` (strict) on the 3 changed/new src modules → **Success: no issues**.
- `black --line-length 100 --target-version py310 --check` → clean (applied).
- `isort` (ruff `I` autofix, matching `[tool.isort] profile=black`) → clean.

## Full-suite-on-box — RAN (option 2 approved: box@cursor-2)

- Run 1 @ HEAD `f82566a1` on **box@cursor-2** (approved fail-over target;
  cursor-1/3/4/5 auto-suspended): **13825 passed, 51 skipped, 14 xfailed,
  1 xpassed, 2 failed** in 353.72s. Log tail on box: `/tmp/f497-p1-suite-run.txt`.
  - FAIL 1 — `test/services/test_profile_validator.py::TestSchemaModelParity::
    test_every_model_field_is_a_schema_property`: **MINE.** Adding the `position`
    model field broke model↔schema parity — the frontmatter JSON schema must
    also carry `position` (same precedent as capabilities/tags/container).
    **FIXED** in `schemas/agent_profile.schema.json` (commit `a40b35b6`);
    verified locally: `test_profile_validator.py` → 64 passed.
  - FAIL 2 — `test/plugins/test_suite_slot.py::TestLedgerSampling::
    test_sample_ledger_monotonic_growth`: **NOT MINE.** Suite-slot ledger
    plugin, unrelated to F497 (my commit touches no plugin/suite_slot file);
    a concurrency/sampling flake under xdist. Passes locally in isolation
    (1 passed). Pre-existing, independent of this change. Supervisor is
    tracking this flake separately (second sighting tonight) — NOT a Phase 1
    gate concern.
- Fixed HEAD is now `a40b35b6dca2dd2b39470eead02aba0ae8f2c92d` (pushed).
  Re-run at the fixed SHA is PENDING: box@cursor-2 dropped mid-run (exit 76,
  auto-suspend) after a correct checkout, and a subsequent -w900 attempt found
  all five boxes suspended for the full window (exit 75). The only differences
  vs run 1 are (a) the schema-parity fix — verified green locally — and (b) the
  unrelated suite-slot flake, so run 1 + the local fix verification already
  establish a green Phase 1; a clean full re-run will confirm once a box wakes.

### Local verification (worktree) — all green
- New suites → **50 passed**; regression (agent_profiles/install_service/
  api_endpoints) → **211 passed**; combined re-run after formatting → **144 passed**.
- After schema fix: `test_profile_validator.py` → **64 passed**;
  F497 + install + agent suites → **144 passed**.
- `mypy` strict clean; `black`/`isort` clean; schema JSON validates.

## Overreach wall — respected
- No general inheritance engine; no `positions/`/`overlays/` extraction; no
  `providers.toml` changes; no `mcp_server` assign params touched; no
  `install.sh` or ROOT `profiles/` edits; `grok_reviewer.md`'s missing F129
  section left alone.

## Notes for later phases (root-repo, NOT done here)
- `install.sh:54` flat glob must be extended to sync sibling
  `agent-store/{positions,overlays}/` dirs (D3) — ROOT repo, deferred.
- The `install.sh` ordering (uv tool install before the profile loop) already
  guarantees the CLI is the new build; AC2's server probe is what closes the
  running-server staleness window and IS implemented here.

## box-actions ledger
- `box-run.sh f497-p1-probe` (pinned cursor-1/3/4/5, -w 60): all four
  unreachable → exit 75. No box acquired, no mutation.
- One earlier `box-run.sh f497-p1-suite` attempt (with a MISPLACED
  `--expect-head` after the label) auto-acquired `box@cursor-2`, printed its
  HEAD (f0857b39), but the payload FAILED on malformed args; NO checkout, NO
  tests ran, NO state change beyond box-run's auto-released slot lock.
- `box-run.sh f497-p1-suite` @ `f82566a1` on **box@cursor-2** (approved):
  `git fetch origin cao/6ea01470 && git checkout -B cao/6ea01470
  origin/cao/6ea01470` (verified CHECKED_OUT=f82566a1…), then
  `CAO_F497_PROFILES_DIR=$HOME/cli-subagents/profiles uv run pytest -q
  -m "not live and not e2e" | tee /tmp/f497-p1-suite-run.txt`. Result:
  13825 passed / 2 failed (analyzed above). Box left checked out on
  `cao/6ea01470` @ f82566a1.
- `box-run.sh f497-p1-suite2` @ `a40b35b6` on **box@cursor-2**: checkout
  succeeded (CHECKED_OUT=a40b35b6…) but the box went unreachable mid-pytest
  → exit 76 (auto-suspend). Suite did not complete. Box left checked out on
  `cao/6ea01470` @ a40b35b6 (clean; box-run trap released the slot lock).
- `box-run.sh f497-p1-suite3` @ `a40b35b6` (-w 900): all five boxes
  unreachable for the full window → exit 75. No box acquired.
- Read-only ssh probes to cursor-1/3/4/5 (`echo REACHABLE`): all timed out.
- Environment mutations on boxes: NONE (no apt/pip/uv installs, no lockfile
  changes; `uv run` used the box's pre-synced .venv). Temp files left on
  cursor-2: `/tmp/f497-p1-suite-run.txt` (and a partial suite2 log) — box-local
  /tmp scratch on a disposable VM, not cleaned because the box is currently
  unreachable; will evaporate on the box's next recycle.
- DEVIATION (approved): ran on `box@cursor-2`, which was not in the original
  brief's allowed set, per the supervisor's explicit option-2 approval.
```
