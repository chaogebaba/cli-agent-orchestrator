# F497 P2 — build report (position/provider decoupling, #352)

- Blueprint: `orchestrator/blueprints/f497-position-provider-decoupling.md` FROZEN **r8** (commit 02b7cb3), DESIGN GATE-YES.
- Scope: **P2 only** — D5 merge engine + D3 layout + `empirical_reviewer` extraction (the measured ~91% pair) with an AUTHORED unified persona (D11) + kiro install-time composition (D2 addendum) + F549 test guard + AC1(narrowed)/AC2/AC3/AC6/AC10/AC13/AC14/AC15(seed)/AC16. P3 (assign `provider=`, D7/D10) and P4 (routing.toml/generator/D9) are SEPARATE later briefs and are NOT in this branch.
- Worktrees (scratch, per supervisor Option-3; fx121 allowlists /data/cao-scratch):
  - FORK `/data/cao-scratch/f497-fork` on `cao/f497` (base main @156323d5, which already contains F497 Phase 1 commit 58543f9e).
  - ROOT `/data/cao-scratch/f497-root` on `cao/f497-root` (base quirks-merge-train @9a07210).

## Commit SHAs

- FORK `cao/f497`: `41a97706` (r9 tip; r8 base `dd773ee9513e9f32ccc9c8effdba2fd617a1fb86`)
- ROOT `cao/f497-root`: `87f2859` (r9 tip; r8 base `1270150375b090e662faca7072faacff11bcda09`)
- Nothing pushed.
- **Commit guard (F121 / `--no-verify`, supervisor-authorized):** both repos were committed
  with `git commit --no-verify`. The fx121 pre-commit guard trips on the cross-branch worktree
  layout the supervisor directed (this terminal is `a4d6d047`; the worktrees are on `cao/f497`
  and `cao/f497-root`, not `cao/a4d6d047`). The supervisor explicitly OK'd `--no-verify` for
  this layout; F121 settlement still records the commits, so they remain auditable.
- r9 delta: position_sha (D8/AC15) excludes the `certification:` block so a PASS row never
  self-invalidates (helpers `position_sha`/`overlay_sha` in `utils/profile_composition.py`;
  recorded cell shas recomputed); `certification` as a resolver meta-key + AC2 already landed
  in the r8 commit.

## What landed

### FORK (cli-agent-orchestrator)

**D5 merge engine — `utils/profile_composition.py` (new).** Six key-classes merged at the
dict layer, `AgentProfile` constructed ONCE:
- persona text (class 1): overlay body APPENDS under `## Provider notes (<provider>)`;
  `replaces: [...]` swaps named sections IN PLACE, byte-exact span substitution
  (positional overlay-section → target), inheriting the replaced span's trailing
  whitespace. A `replaces:` naming an ABSENT heading is a hard `CompositionError` (AC10).
- scalar last-write-wins + empty-string clear (class 2 + catch-all 6).
- list union with `_replace` escape, preserving the skills absent/null/[] tri-state (class 3, AC3).
- dict shallow key-wise (class 4). contextPolicy atomic-replace + extraLeaves UNION (class 5, AC6).
- Resolver meta-keys stripped before construction: `extends, _replace, position, providers,
  replaces, requires, certification`.
- `compose_source_body()` (persona-only, no AgentProfile) + `composed_profile_hash()` (D8,
  compute-only — see F127 note).

**Resolver — `utils/agent_profiles.py`.** `resolve_agent_profile` composes a stub declaring
`extends:`/`position:` from `positions/<pos>.md` + `overlays/<provider>[.<pos>].md` (D4 2→4),
stamps the LEGACY concrete `.name` (D6), enforces the D3 role-mirror strict-agreement check
and stub-owned `description` (AC12). New: `compose_agent_profile_source()` (raw-fragment,
`${VAR}`-preserving SOURCE composition for the kiro context file, AC16 — same module as the
engine per r8 S1/S3); `_read_composition_store(resolve_env=)`; call-time store dirs.

**D2 named edit — `providers/claude_code.py`.** `sessionBrief` routes through
`load_agent_profile` (resolver) instead of a raw parse, so a position-inherited sessionBrief
composes (AC12).

**D2 addendum / AC16 — `services/install_service.py`.** `install_agent` composes via
`resolve_agent_profile` (composed AgentProfile fields) and `_write_context_file` writes the
COMPOSED, UNRESOLVED body to `agent-context/<name>.md` via `compose_agent_profile_source`
(kiro delivers persona at INSTALL time; a composition stub's raw body is empty). Legacy
(non-composition) profiles: byte-identical context file. Install re-composes on every run,
so a D8 hash change (position/overlay edit) never leaves a stale kiro context file.

**F549 (#405) — `constants.py` + `test/conftest.py`.** Call-time dir accessors
(`kiro_agents_dir`, `agent_context_dir`, `positions_store_dir`, `overlays_store_dir`);
`install_agent`/`_write_context_file` resolve dirs at CALL time. conftest pins
`CAO_AGENTS_DIR` into the test home at import AND an autouse `_f549_guard_real_home_dirs`
fixture snapshots + asserts `~/.kiro/agents`, `~/.aws/.../agent-store`,
`~/.aws/.../agent-context` are UNCHANGED at teardown.

**AC14 clause-lint — `utils/clause_lint.py` (new).** Loads the supervisor-owned clause table
(`profiles/positions/_clauses.toml`), maps clause ids → heading/marker match rules and
positions → required id sets, composes every position and asserts required clauses match.
Fail-closed both directions (unknown id, position without a row, row without a file);
`requires:` may only ADD.

### ROOT (cli-subagents)

- `profiles/positions/`: `empirical_reviewer.md` (AUTHORED unified persona: best-of
  kiro_reviewer+codex_empirical_reviewer; keeps callback-contract, containment, F129
  frozen-pin section, never-edit-artifact-branch, AC14 test-attachments clause + `requires:`
  set; inline `<!-- clause:* -->` markers), `dev.md` (unified kiro/codex/grok dev), `grunt.md`
  (cline_dev's grunt persona — its own position, never dev). Enumeration only for dev/grunt
  (no dev/grunt extraction in P2).
- `profiles/overlays/`: `kiro_cli.md` (engine:kas), `kiro_cli.empirical_reviewer.md`,
  `codex.empirical_reviewer.md` (contextPolicy + neutral-vocabulary notes), `codex.dev.md`,
  `grok_cli.dev.md` (serial-tool constraint; NO model/reasoningEffort — ruling 3).
- `profiles/positions/_clauses.toml`: seeded clause table (callback-contract, containment,
  f129-pins, never-edit-artifact-branch, test-attachments, never-emit-verdict, grunt-scope).
- WIRED stubs (D11): `profiles/kiro_reviewer.md` + `profiles/codex_empirical_reviewer.md` are
  now 5-key stubs (`name`, `provider`, `extends: empirical_reviewer`, `description` verbatim,
  `role` verbatim). dev/grunt legacy names are NOT wired in P2.
- AC15 certification block in `positions/empirical_reviewer.md`: kiro_cli + codex cells
  recorded UNCERTIFIED (position_sha 8eb05184b57e6908; overlay_sha kiro d2ea6ed1923d23d1,
  codex 0e230e7cd92f9901; date 2026-08-28). The box smoke is run by the tester AFTER this
  report; D9 routing (P4) must refuse to bind an UNCERTIFIED cell until a PASS row replaces it.
- `install.sh`: syncs `profiles/{positions,overlays}/` into the agent-store sibling dirs
  BEFORE the profile loop (D3), skipping `# FROZEN:` fragments.

## Test summary (verbatim)

Run serially (`-n0`), `CI=1` (suite-slot bypass for scoped file runs, slot contended by a
sibling terminal), `CAO_SKIP_RESOLVER_PROBE=1`, `CAO_F497_PROFILES_DIR=<root>/profiles`:

```
test/utils/test_f497_resolver.py test/utils/test_f497_composition.py test/utils/test_f497_clauses.py
test/services/test_f497_install_composition.py test/services/test_f497_install_guard.py
test/utils/test_agent_profiles.py test/services/test_install_service.py
test/services/test_profile_validator.py test/api/test_api_endpoints.py
=> 357 passed, 2 skipped
```

The 2 skips are the WIRED extracted stubs (kiro_reviewer, codex_empirical_reviewer) in the
Phase-1 total-identity corpus harness — they carry `extends:` and are covered instead by the
AC1-narrowed golden test (`test_ac1_narrowed_extracted_profile_matches_golden_except_body`).

- `mypy` (strict) on `clause_lint.py`, `profile_composition.py`, `agent_profiles.py`,
  `constants.py`: **Success: no issues found**.
- `black --line-length 100`: clean on all touched files.
- Collateral check (F549 guard no false-positive): `test/services/test_terminal_service.py`
  + `test/cli/commands/test_redeploy.py` => **38 passed**.

AC coverage: AC1(narrowed) ✓, AC2 (meta-keys incl. `requires`/`certification` stripped;
resolver-less refusal) ✓, AC3 ✓, AC6 ✓, AC10 ✓, AC13 ✓, AC14 (clause-lint fail-closed both
ways + marker-verbatim) ✓, AC15 (schema + 2 UNCERTIFIED cells; box smoke deferred to tester) ✓,
AC16 (install body == raw spawn fragments; overlay-edit recompose) ✓. D8 hash computed +
tested; NOT stamped (F127 #130 open).

## Named deviations (blueprint)

1. **D2 addendum (kiro install-time persona delivery)** — implemented per r8's folded
   addendum: install composes RAW fragments and writes the composed unresolved body to the
   kiro context file. (Blueprint originally assumed compose-at-spawn covers all providers.)
2. **AC1 narrowed (D11)** — persona BODIES are authored, not byte-reconstructed; AC1 excludes
   `system_prompt` for the exact set of names carrying `extends:`. Unextracted profiles remain
   fully byte-identical (Phase-1 corpus harness green).
3. **Proof family = `empirical_reviewer`, not `dev`** — per r8 migration step 2 (the builder's
   divergence finding folded into D11): the dev family diverges 40–60 of ~80 lines pairwise;
   `empirical_reviewer` is the measured ~91% pair. `dev`/`grunt` personas are drafted +
   enumerated but NOT extracted (no wired stubs) in P2.
4. **D8 hash compute-only** — `composed_profile_hash()` computed and tested but NOT stamped
   into terminal metadata; the spawn-time stamp rides F127's echo channel (F127 #130 OPEN).
   No second channel built (per hard-rule d).
5. **`certification:` cells UNCERTIFIED** — the box smoke (AC15) is run by the tester after
   this report; both cells seeded UNCERTIFIED. This build report's frozen-pin attestation is
   NOT re-issued for the uncertified lines (the report names them uncertified).

## F549 import-time HOME-path sweep (constants.py, per ruling 2)

Module-level constants bound at IMPORT that a write path could otherwise send to a REAL home
dir (now fronted by call-time accessors for the install write paths; listed for completeness):
- `CAO_HOME_DIR` (→ AGENT_CONTEXT_DIR, LOCAL_AGENT_STORE_DIR, POSITIONS/OVERLAYS_STORE_DIR,
  SKILLS_DIR, DB_DIR, LOG_DIR, FIFO_DIR, LOCK_DIR, MEMORY_BASE_DIR, WORKFLOW_SPEC_DIR, …).
- `KIRO_AGENTS_DIR` (`CAO_AGENTS_DIR` → `~/.kiro/agents`) — the incident path; now pinned in
  conftest + call-time `kiro_agents_dir()`.
- `COPILOT_AGENTS_DIR` (`~/.copilot/agents`), `OPENCODE_CONFIG_DIR`/`OPENCODE_AGENTS_DIR`
  (`~/.aws/opencode`) — not written by P2 code paths, but same import-time shape; flagged for
  a later F549 sweep to move behind call-time accessors if any write path uses them.

## Mutation incident (disclosed, remediated)

During install-path testing an `install_agent("kiro_reviewer")` call resolved the import-time
`CAO_AGENTS_DIR` and briefly wrote the REAL `~/.kiro/agents/kiro_reviewer.json` with broken
scratch paths. RESTORED by re-installing from the intact real store under the default
CAO_HOME; the real context file was never touched. Mechanized against recurrence by the F549
conftest pin + guard (filed F549 #405). No other real-dir writes.

## Not done in P2 (later briefs)

- P3: `assign(provider=)` + position-name dispatch resolution + `providers:` allowlist
  hard-fail + D10 routing-flip cold-fallback.
- P4: `orchestrator/routing.toml` loader/validator + ROUTING.md generator (D9) + reviewer/
  oracle/base/identity extraction + dev/grunt wiring.
- AC15 box smoke (tester), AC9 spawn-time hash stamp (F127 #130), AC5/AC7/AC8/AC11.
