**Artifact-Path:** /data/cao-scratch/f497-root/orchestrator/build-reports/f497-build-report.md
**Artifact-SHA256:** 4ea3d6d7ad132361b1686ad886c980a4a2be1ee7c4648a137e95c13fd9a531f8
**Artifact-Repo-Path:** orchestrator/build-reports/f497-build-report.md
**Git-SHA-fork:** 41a97706e93fc1bc62cff3aaa029bb006736e53e
**Git-SHA-root:** 04a0c335c15fb68ff319e9a6feaa32648440f11f
**Ruling:** GATE-YES
**Report-SHA256:** 204f7a1b4f0cc4e9279763196eda478f46a448a129e031fd3daa1b7feb1a9231
**Counts:** 0 BLOCKER / 1 SHOULD / 3 NIT

---

# F497 #352 Phase 2 — EMPIRICAL GATE (cold reviewer, Codex lane superseded)

## VERDICT HEADER

**Ruling: GATE-YES — 0 BLOCKER / 1 SHOULD / 3 NIT.**
Zero-decision buildable is not the frame here (this is a build-diff gate, not a blueprint gate); the
empirical frame — *does the merge engine + first extraction survive contact with the real tree?* — is
answered YES. All 10 P2 acceptance criteria (AC1-narrowed, AC2, AC3, AC6, AC10, AC12, AC13, AC14,
AC15, AC16) verified against the REAL ROOT profile files (not just the tmp_path fixtures), plus the
F549 guard mechanism. The unified `empirical_reviewer` persona fits its position (D11): nothing a
gate needs is dropped from either legacy copy, and provider mechanics are correctly isolated to
overlays. Both worktrees end byte-identical to their start (baseline-relative check below).

| id | sev | claim | smallest amendment |
|----|-----|-------|--------------------|
| S1 | SHOULD | AC14 "the lint fails if a listed id is unknown" is NOT enforced through the public entrypoint. `lint_positions` pre-filters `requires:` to `[r for r in requires_extra if r in table.rules]`, so a BOGUS clause id in `requires:` is silently dropped, never reaching the `_position_required_ids` unknown-id raise. Empirically: injecting `requires: [..., "made-up-id"]` into the real `empirical_reviewer.md` and linting → PASSES (should FAIL). The unit test `test_ac14_requires_may_only_add` uses a KNOWN id (`extra-thing`) so it gives false confidence. The dispatch's explicit MUTATE requirement ("bogus id in `requires:` → fail") and the blueprint AC14 wording are both unmet in one of the two fail-closed directions. | Split intent: treat `requires:` entries as clause-id ADD-directives and raise on any that is not a known clause id (keeping free-text notes in a separate key, or requiring `requires:` to be ids only per AC14's literal wording). Add a `lint_positions`-level (not helper-level) test that injects a bogus id and asserts `ClauseLintError`. |
| N1 | NIT | `profile_composition.py` MODULE docstring's meta-key list ends "...`providers`, `replaces`) are stripped" — omits `requires` and `certification`, though the actual `_MERGE_META_KEYS` tuple correctly includes both. Stale doc. | Append `requires`, `certification` to the docstring list. |
| N2 | NIT | `install_service.py` still imports `AGENT_CONTEXT_DIR` and `KIRO_AGENTS_DIR` (lines 15/18) but no longer USES them — the write paths use call-time `agent_context_dir()` / `kiro_agents_dir()`. Dead imports. | Remove the two unused import names. |
| N3 | NIT | Report self-reports ROOT tip `87f2859` and blueprint `r8` in its header; the actual ROOT tip is `04a0c335` and the pinned blueprint is `r9`. Both are benign: the report was written then committed (advancing the tip past its own self-report), and the r9 fold is a superset of r8 that the report's r9-delta bullet already describes. | Optional: bump the header SHA/revision to the committed tip. |

**Diff-base advisory (not a finding, but the merge operator must heed it):** the dispatch's two-dot
`git diff quirks-merge-train..04a0c335` shows ~17 extra ROOT files (CLAUDE.md, ORCH_MAP.md, README.md,
USAGE.md, BUGS.md, MISTAKES.md, a `gates.md` line DELETION, and legacy profile edits) that F497 did
**not** author. These are base-advance artifacts: `quirks-merge-train` has commits after the
merge-base (`9a07210`) that the F497 branch lacks. The TRUE F497 delta is the merge-base three-dot
`9a07210..04a0c335` = exactly 13 files, matching the dispatch scope (install.sh, build-report, the two
5-key stubs, 5 overlays, `_clauses.toml`, 3 positions). Confirmed: the `gates.md` "In-house lineage
first" line was ADDED on the base by commit `8da08f9` AFTER the merge-base; F497 never touched
`gates.md`, so its apparent "deletion" is a diff artifact and merges cleanly (no conflict).

---

## APPENDIX — Empirical checks (each RAN; observed results)

Environment: FORK worktree `/data/cao-scratch/f497-fork` @ `41a97706` (clean), ROOT worktree
`/data/cao-scratch/f497-root` @ `04a0c335` (clean). Tests serial `-n0`, `CI=1
CAO_SKIP_RESOLVER_PROBE=1 CAO_F497_PROFILES_DIR=/data/cao-scratch/f497-root/profiles`. All scratch
under `/data/cao-scratch/b54de662/` (removed before this report). Neither worktree was written.

### Prescribed suite (verbatim run)
`test_f497_resolver.py test_f497_composition.py test_f497_clauses.py test_f497_install_composition.py
test_f497_install_guard.py test_agent_profiles.py test_install_service.py test_profile_validator.py
test_api_endpoints.py` → **359 passed, 2 skipped in 16.16s.** (Report claimed 357; the +2 is harmless
count drift — the 2 skips are exactly the WIRED stubs `kiro_reviewer`/`codex_empirical_reviewer` in
the AC1 harness, covered by the golden test instead. 0 failures.)

### AC1 (narrowed) — OBSERVED
`test_ac1_legacy_profile_resolves_byte_identically` parametrized over the live 22-profile corpus:
20 PASSED (field-for-field `model_dump()` equality resolver-vs-direct-parse), exactly 2 SKIPPED
(`kiro_reviewer`, `codex_empirical_reviewer` — the `extends:`-bearing stubs). `test_ac1_narrowed_
extracted_profile_matches_golden_except_body` PASSED for both stubs: every field except
`{system_prompt, position}` equals the pre-extraction golden. I independently confirmed the goldens in
`test/utils/f497_golden/` are BYTE-IDENTICAL to the merge-base originals (`git show 9a07210:profiles/
kiro_reviewer.md` == golden; same for codex_empirical_reviewer) — so "identical on every non-body
field" is genuinely verified against the real legacy profiles.

### AC2 — OBSERVED
`_RESOLVER_META_KEYS` and `_MERGE_META_KEYS` both include `requires` and `certification`;
`test_meta_keys_never_reach_agent_profile` PASSED. Install refusal: `test_f497_install_guard.py`
covers server-no-support, server-unreachable, non-200, malformed-body (all refuse), and the
`CAO_SKIP_RESOLVER_PROBE` escape (allows) — all PASSED.

### AC3 / AC6 / AC10 / AC12 / AC13 — OBSERVED
- AC3 skills tri-state (absent→inherit, null→full catalog, []→none, union-dedupe): 4 tests PASSED.
- AC6 contextPolicy atomic-replace + extraLeaves union: 2 tests PASSED. I resolved the REAL
  `codex_empirical_reviewer` → `contextPolicy.extraLeaves == ['gpt-unrestricted.md']` survives the
  position contextPolicy.
- AC10 replaces absent-heading hard error + present-heading in-place swap: 2 tests PASSED.
- AC12: I resolved both real stubs through the resolver → correct legacy `.name`, `.provider`,
  `.position=empirical_reviewer`, `.role=developer` (mirror agrees), stub-owned `.description`,
  `.skills`, kiro `engine=KAS` / codex `engine=None`, `mcpServers=[cao-mcp-server]`, body carries the
  appended `## Provider notes (...)`.
- AC13 `model_fields` catch-all coverage: PASSED.

### AC14 — MUTATIONS (against SCRATCH copies)
- MUT1 delete `<!-- clause:containment -->` from `empirical_reviewer.md` → `ClauseLintError: missing
  required clause 'containment'`. **Fails closed ✓**
- MUT3 remove the `empirical_reviewer = [...]` table row → `ClauseLintError: position 'empirical_
  reviewer' has no [required] row`. **Fails closed ✓**
- MUT4 delete `dev.md` while keeping its row → `ClauseLintError: [required] row 'dev' names a position
  with no ... file`. **Fails closed ✓**
- MUT2 / MUT2b bogus id in `requires:` (`"bogus-nonexistent-clause"`, `"made-up-id"`) → lint
  **PASSES** (should FAIL). **This is S1.** Real ROOT positions lint green with the exact required
  sets: empirical_reviewer=[callback-contract, containment, f129-pins, never-edit-artifact-branch,
  test-attachments]; dev/grunt as specified.

### AC15 — OBSERVED
`position_sha(body, frontmatter)` over the real `empirical_reviewer.md` = `2d50a99eb57e9527`, matching
BOTH recorded certification cells. Flipping every cell UNCERTIFIED→PASS and recomputing → still
`2d50a99eb57e9527` (UNCHANGED — the `certification:` block is excluded, so a PASS row never self-
invalidates). `overlay_sha`: kiro_cli=`86d80cda78a3c52a`, codex=`65944a876ddb7ab1` — both match the
recorded cells. `test_r9_position_sha_excludes_certification_block` and `test_r9_recorded_position_
sha_matches_helper` PASSED.

### AC16 — install==spawn + overlay MUTATE (against SCRATCH copies)
`compose_agent_profile_source(kiro_reviewer stub)` body == `resolve_agent_profile(...).system_prompt`:
**byte-equal ✓**. Appended a mutation marker to `kiro_cli.empirical_reviewer.md` overlay → recomposed
source CHANGED and contained the marker ✓; restored → recompose returned to the original ✓. Suite:
`test_install_context_body_equals_raw_spawn_fragments`, `test_legacy_context_file_is_byte_identical_
to_raw`, `test_var_deferral_preserved_in_context_body`, `test_reinstall_recomposes_after_overlay_
edit`, `test_d8_hash_invalidates_on_position_and_overlay_edit` — all PASSED. `_write_context_file`
uses call-time `agent_context_dir()` + `compose_agent_profile_source`; kiro write path uses call-time
`kiro_agents_dir()`.

### F549 guard — OBSERVED (throwaway test, NEVER against real dirs)
Reproduced the conftest guard's snapshot/compare logic verbatim, pointed `watched` at a SCRATCH dir:
control (no write) → guard quiet; violation (write `kiro_reviewer.json` under the watched dir) → guard
detects the change (`assert not changed` would raise). Both proof tests PASSED. The real autouse
`_f549_guard_real_home_dirs` fixture stays green while `test_f497_install_guard.py` calls
`install_agent` (positive proof the `CAO_AGENTS_DIR` pin redirects writes into the test home).

### mypy / black
- `uv run mypy <6 touched modules>` (pyproject `strict=true`) → **Success: no issues found in 6 source
  files.** (An explicit `--strict` CLI flag on file paths surfaces pre-existing `type-arg` noise in
  `agent_profiles.py` — present identically at the BASE commit `156323d5`, an invocation artifact, not
  an F497 regression. The two greenfield files `clause_lint.py`/`profile_composition.py` are strict-
  clean even under the harsher invocation.) Report's mypy claim reproduces via the canonical run.
- `black --line-length 100 --check` on all 6 touched fork files → clean.

### Named deviations vs r9 blueprint — all SANCTIONED
1. D2 addendum (kiro install-time persona) — r9 "D2 addendum" section. ✓
2. AC1 narrowed (authored bodies) — r9 D11 + AC1-narrowed wording. ✓
3. Proof family = `empirical_reviewer` not `dev` — r9 migration step 2 + D11. ✓
4. D8 hash compute-only (stamp gated on F127 #130) — r9 D8 + migration "reusing F127's echo"; AC9 is
   listed under "Not done in P2". ✓
5. `certification:` cells UNCERTIFIED (box smoke deferred to tester) — r9 AC15. ✓
No unsanctioned deviation found. The `--no-verify` cross-branch commits ARE stated (report "Commit
guard (F121 / `--no-verify`, supervisor-authorized)" bullet).

### Persona position-fit (D11) — I am this persona's future user
The unified `positions/empirical_reviewer.md` keeps every gate-critical element of BOTH legacy copies:
F129 frozen-pin protocol + report headers, never-edit-artifact-branch, callback contract (one terse
callback via cao-mcp-server), containment/SCRATCH-F462, sandbox probes, stop-and-ask, offload-box,
zero-decision ruling, English-only. It FOLDS the "Reviewer test attachments & suite recommendations"
authority as an explicit section (D11 / gates.md) — stronger than either legacy copy, which only
implied it. Provider mechanics (findings-file naming, neutral-vocabulary, engine=KAS, extraLeaves) are
correctly in the overlays, not the position. The one softening — Hard-wall now scoped "on the artifact
branch" — is the exact reconciliation D11 sanctions ("a gate is not 'never author anything' — it is
'never commit to the artifact branch'"). Nothing a gate needs is dropped. Fit: GOOD.

### Baseline-relative clean check
FORK `git status --short` empty at START and END; ROOT likewise. HEADs unchanged (fork `41a97706`,
root `04a0c335`). Re-hashed in-scope files post-review == start hashes (e.g. empirical_reviewer.md
`4dffe187…`, _clauses.toml `0054dfd8…`, profile_composition.py `6e0d897b…`, clause_lint.py
`a595de45…`). All mutations were on scratch copies under `/data/cao-scratch/b54de662/`, now removed.

**Ruling: GATE-YES (0 BLOCKER / 1 SHOULD / 3 NIT).** S1 is a fail-closed gap in ONE AC14 direction
that overclaims in the build report but blocks nothing live (no real position carries an id-shaped
`requires:` entry); it should be closed before the AC14 guard is relied upon as the sole regression
net for future extractions.



---

## Delta r2 — S1 fix re-gate (fork 41a97706 → 559dd4ae)

**Ruling: GATE-YES — S1 CLOSED. 0 BLOCKER / 0 SHOULD / 3 NIT** (the 3 NITs from r1 are
unaddressed but were never merge-blocking; see note below).

**Scope of delta:** fork `cao/f497` advanced `41a97706` → `559dd4ae`, exactly 2 files
(`src/cli_agent_orchestrator/utils/clause_lint.py` +30/-6, `test/utils/test_f497_clauses.py`
+51/-6). ROOT `cao/f497-root` UNCHANGED at `04a0c335` (verified: clean, HEAD identical). Frozen
pin re-verified VALID at delta-start and pre-callback. No edits to either branch by this review.

**The fix (clause_lint.py):** `lint_positions` now classifies each `requires:` entry with
`_ID_SHAPED = ^[a-z0-9]+(-[a-z0-9]+)*$`. An id-shaped token that is NOT a known clause id raises
`ClauseLintError(f"position '{pos}' requires: names unknown clause id '{r}'")` — fail-closed at the
PUBLIC entrypoint, not just the internal helper. A non-id-shaped string (prose sentence: spaces,
capitals, punctuation) is still ignored as free text. This is exactly the split my r1 S1 asked for.

**Empirical checks (RAN; scratch copies of the REAL positions, worktrees never touched):**
1. **S1 MUTATE re-run** — injected `requires: [..., "bogus-nonexistent-clause"]` into a copy of the
   real `empirical_reviewer.md` → `ClauseLintError: position 'empirical_reviewer' requires: names
   unknown clause id 'bogus-nonexistent-clause'`. **Now FAILS closed ✓** (r1: it wrongly passed).
   Variant `requires: [callback-contract, "madeupid"]` → also fails on `'madeupid'`. ✓
2. **Real positions still green** — `lint_positions` over the real ROOT positions returns the exact
   required sets: empirical_reviewer=[callback-contract, containment, f129-pins,
   never-edit-artifact-branch, test-attachments]; dev=[callback-contract, containment];
   grunt=[+grunt-scope]. ✓ The real `requires:` entries are prose sentences (not id-shaped), so they
   remain legal — regression-checked directly. ✓
3. **Targeted `test/utils/test_f497_clauses.py`** — **11 passed** (was 9; +2 new S1-covering tests).
   The corrected `test_ac14_requires_may_only_add` now uses a BOGUS id (`no-such-clause`) and asserts
   `ClauseLintError match="unknown clause id 'no-such-clause'"` — precisely the case r1 flagged as
   giving false confidence. New `test_ac14_requires_prose_sentence_is_accepted` pins the prose path.
   `test_ac14_requires_known_id_must_be_present` retains the known-id-must-match check. ✓
4. **Corpus skips vs the real corpus** (CAO positions pointed at the root worktree via
   `CAO_F497_PROFILES_DIR`): the AC1 resolver harness runs 20 PASSED + **2 SKIPPED** — exactly
   `kiro_reviewer` and `codex_empirical_reviewer`, the two `extends:`-bearing stubs whose body
   byte-identity is proven against the goldens in `test_f497_composition.py`. **Observed skip count
   is 2, not 3** — this matches the build report's own text ("The 2 skips are the WIRED extracted
   stubs"). The dispatch's "3 corpus skips" is a benign miscount; no third stub is wired (dev/grunt
   are enumerated but not extracted in P2).

**Full prescribed suite with the new code:** 361 passed, 2 skipped (was 359/2 at r1; +2 = the two new
clause tests). 0 failures.

**Regression confirmations:** prose `requires:` entry stays legal (no false unknown-id error); a known
id added via `requires:` still fails if its clause marker is absent from the body; the other three
fail-closed directions (missing clause, position without row, row without file) are unchanged and
still green.

**Residual NITs (r1, unaddressed, non-blocking):** N1 stale `_MERGE_META_KEYS` docstring in
`profile_composition.py`; N2 dead imports `AGENT_CONTEXT_DIR`/`KIRO_AGENTS_DIR` in `install_service.py`;
N3 build-report header self-reports an earlier tip/rev. None gate the merge; carry as cleanup.

**Baseline-relative clean check:** both worktrees `git status --short` empty at delta-start and end;
HEADs fork `559dd4ae` / root `04a0c335` unchanged. All mutations were on scratch copies under
`/data/cao-scratch/b54de662/`, removed before this attestation.

**Delta r2 ruling: GATE-YES — S1 CLOSED (0 BLOCKER / 0 SHOULD / 3 NIT).**
