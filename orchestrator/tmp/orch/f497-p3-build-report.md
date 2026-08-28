# F497 #352 Phase 3 — Build Report (r2)

> **r2 delta (2026-08-28)** — folds the empirical gate r1 (GATE-NO 1B/3S/1N,
> `orchestrator/tmp/orch/f497-p3-empirical-gate-r1.md` @ `ec4f5d50`):
> - **B1 (BLOCKER) FIXED** — `resolve_assignment_target` no longer hard-fails an
>   uninstalled legacy name pre-spawn. **Option (b)** (user/supervisor steering
>   2026-08-28, SWITCHED from the brief's option a to avoid a `codex_dev`/`grok_dev`
>   collision): the resolver ENGAGES only when the caller passes `provider=`
>   (position mode) OR the name is a bare position file (`positions/<name>.md`);
>   every other name passes through UNTOUCHED as a legacy name — no store lookup, no
>   `<provider>_<position>` shape inference (synthesis is P4). This restores exact
>   pre-D7 behaviour for `kiro_dev`/`codex_profile`/`codex_dev`/`grok_dev` on a clean
>   store. Clean-store regression tests added (see below).
> - **S1 FIXED** — Git-SHA-root re-stamped to the real root-tip lineage.
> - **S2/S3 FIXED** — install-service count corrected to **86** (not 68).
> - **N1** — box-env/infra failures flagged to fleet, not P3 (unchanged).
> The D7/D10/AC17/D13 deliverables and the r1 mutation ledger are otherwise unchanged.

**Blueprint authority:** `orchestrator/blueprints/f497-position-provider-decoupling.md`
@ ROOT `ef62d11`/`0821215`, FROZEN r13. Built ON merged P2 (fork main `b7a1f9d5`,
root `2531a8c`): `utils/profile_composition.py`, `utils/clause_lint.py`,
`compose_agent_profile_source()`, `positions/`, `overlays/`, stubs. Nothing re-derived.

**Paired-tree layout (as delivered):**

- **FORK** = worktree `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/9a61dd36`,
  branch `cao/9a61dd36`, base main `b7a1f9d5`. Holds the Python package — ALL code
  deliverables (D7, D10, AC17 lint code + wiring, tests) landed here.
- **ROOT** = `/data/cao-scratch/f497p3-root`, branch `cao/f497-p3-root`, base
  quirks-merge-train `0821215`. Holds `profiles/` + `_clauses.toml` — the D13 position
  trim landed here. (The `providers:` frontmatter allowlists per D7 already existed on
  the P2 positions in ROOT; see AC4/allowlist note below.)

---

## Per-deliverable evidence

### Deliverable 1 — D7: `assign(provider: Optional[str])` + position-name resolution

**Artifact-Path (FORK):**
- `src/cli_agent_orchestrator/utils/agent_profiles.py` (resolver)
- `src/cli_agent_orchestrator/mcp_server/server.py` (`_assign_impl` + `assign` tool)
- `test/mcp_server/test_f497_assign_provider.py` (new)

`resolve_assignment_target(agent_profile, provider) -> (name, provider)`:
- LEGACY name (`read_agent_profile_source` resolves) → returned UNCHANGED, provider
  passthrough (existing `resolve_provider` chain owns legacy provider resolution).
- POSITION name (no store profile, `positions/<name>.md` exists) → provider MANDATORY
  (`provider=` arg; D9 routing binding is P4). No provider → `E-POSITION-NEEDS-PROVIDER`.
  Provider outside the position `providers:` allowlist → `E-PROVIDER-NOT-ALLOWED`.
- Neither → `E-UNKNOWN-POSITION`.

`_assign_impl` gained a `provider` param and calls `resolve_assignment_target` FIRST;
on `AssignmentResolutionError` it returns a failure dict BEFORE any terminal is created.
The `assign` MCP tool gained the `provider` Field and passes it through.

**SCOPE / supervisor ruling (2026-08-28, option b):** P3 lands the RESOLUTION +
VALIDATION layer only; no agent-store writes at assign time. The live on-disk spawn
wiring for a position-name target (materialising/keying the composed
`<provider>_<position>` for server-side `load_agent_profile`) is **D9/P4**. Tests mock
`_create_terminal` (matching `test_fork_assign_errors.py` style).

**Tests — `test/mcp_server/test_f497_assign_provider.py` (5, all PASS):**
- `test_d7_legacy_name_unchanged` — legacy name spawns unchanged, provider= ignored.
- `test_d7_position_plus_provider_spawns_composed` — position + allowed provider → spawn.
- `test_d7_position_without_provider_hard_fails` — `E-POSITION-NEEDS-PROVIDER`, no spawn.
- `test_d7_disallowed_provider_hard_fails_no_terminal` — `E-PROVIDER-NOT-ALLOWED`, no spawn.
- `test_d7_unknown_name_hard_fails` — `E-UNKNOWN-POSITION`, no spawn.

Result: `5 passed`.

**Meta-key strip / allowlist (AC2/AC4):** the `providers` meta-key is already stripped
from composed output by P2 (`_MERGE_META_KEYS`/`_RESOLVER_META_KEYS`, verified by P2's
`test_meta_keys_never_reach_agent_profile`). The position `providers:` allowlist is
enforced in BOTH `_resolve_composition_layers` (P2, composition-time) and now
`resolve_assignment_target` (assign-time, D7). No `providers:` frontmatter needed to be
added in P3 — the P2 positions already carry the allowlist shape the D7 checker reads.

### Deliverable 2 — D10: routing-flip fork-base cold-fallback degradation

**Artifact-Path (FORK):**
- `src/cli_agent_orchestrator/mcp_server/server.py` — `_cold_fallback_preamble()` helper
  (new, D12 single-line grammar) + the mismatch-site conversion.
- `test/mcp_server/test_fork_assign_errors.py` — AC11 tests (appended).

At the `provider_mismatch` site (`if row is not None: … if provider != row["provider"]`):
when `defaulted_fork` is FALSE the `raise ValueError("provider_mismatch")` is unchanged;
when `defaulted_fork` is TRUE it degrades — sets the `[COLD-FALLBACK base=stale]` preamble
via `_cold_fallback_preamble(..., base_stale=True)`, nulls `fork_from` AND `row`, and falls
through to the cold path (D10 exactly). `_cold_fallback_preamble` implements the D12
single-line grammar (fields `position`, `cell`, `base` — optional, FIXED order): it folds a
new field set into any existing structured `[COLD-FALLBACK f=v …]` line (r11 S1 — one line
per spawn) rather than emitting a second bare marker.

**Tests — `test/mcp_server/test_fork_assign_errors.py` (18, all PASS):**
- `test_ac11_defaulted_fork_provider_mismatch_degrades_and_spawns` (NEW) — asserts the
  worker message preamble contains EXACTLY ONE `[COLD-FALLBACK` line and it starts with
  `[COLD-FALLBACK` with `base=stale` (D10-alone form). `create.assert_called_once()`.
- `test_ac11_explicit_fork_from_provider_mismatch_still_raises` (NEW) — explicit
  `fork_from="base"` (defaulted_fork False) still raises `provider_mismatch`, no terminal.
- Pre-existing `test_validation_errors_do_not_spawn[provider_mismatch]` (line 59 — explicit
  `fork_from="base"`) stays GREEN unmodified (blueprint requirement).

Result: `18 passed`.

### Deliverable 3 — AC17: persona byte-budget lint

**Artifact-Path (FORK):**
- `src/cli_agent_orchestrator/utils/clause_lint.py` — `[budget]` parse in
  `load_clause_table`, `lint_budgets()` (new), `_body_bytes`, `_overlay_provider_cells`,
  `_POSITION_KEY_SHAPED`.
- `src/cli_agent_orchestrator/services/install_service.py` — lint wired into
  `install_agent` (composition profiles only).
- `test/utils/test_f497_clauses.py` — AC17 block (appended).

`lint_budgets(positions_dir, overlays_dir, clause_table_path)` reads `[budget]` from
`positions/_clauses.toml` and fails CLOSED, naming file/bytes/budget:
- position body > `[budget].<position>` → fail;
- each overlay fragment body > `[budget].overlay` → fail;
- composed cell body > `[budget].<position>` + `[budget].overlay` + `[budget].composed_slack`
  → fail (per (position, provider) cell, composed via P2's `compose_source_body`);
- a composed position with no `[budget]` row → fail;
- no `[budget]` section at all → fail (AC17 requires one).

**D13 clarification (supervisor ruling 2026-08-28, option 1a) — NOT a deviation:** an
"unknown budget key" = not a reserved key (`overlay`/`composed_slack`) AND not a
`_POSITION_KEY_SHAPED` identifier → hard fail. An id-shaped key with no `positions/<key>.md`
and no `[required]` row is a legitimate FORWARD DECLARATION (the frozen r13 `_clauses.toml`
forward-declares `design_reviewer` and `general`): it logs exactly one WARNING
`forward-declared budget key <key> (no position file)` and continues. This reconciles AC17's
"unknown budget key → fail" with the frozen table (which the brief mandates building ON, not
re-deriving).

Wiring: `install_agent` runs `lint_positions` (AC14) + `lint_budgets` (AC17) against the
live positions store when installing a composition-bearing profile and REFUSES the install
(`InstallResult(success=False, …)`) on `ClauseLintError`. A legacy profile skips both.

**Tests — `test/utils/test_f497_clauses.py` AC17 block (7, all PASS):**
- `test_ac17_position_one_byte_over_fails` — 101 B body, budget 100 → fail.
- `test_ac17_position_at_budget_passes` — 100 B body, budget 100 → pass (`<=`).
- `test_ac17_composed_over_sum_fails` — composed body over position+overlay+slack → fail.
- `test_ac17_unknown_budget_key_fails` — `"bad key!"` (non-id-shaped) → fail.
- `test_ac17_forward_declared_budget_key_warns_and_continues` — `design_reviewer` id-shaped,
  no file, no `[required]` row → exactly ONE warning (caplog), lint continues.
- `test_ac17_missing_budget_row_fails` — position file, no `[budget]` row → fail.
- `test_ac17_no_budget_section_fails` — table with no `[budget]` → fail.

Result (AC14+AC17 file): `18 passed`. Live ROOT corpus: `lint_positions` green
(empirical_reviewer=5, dev=2, grunt=3 clauses); `lint_budgets` green (see byte counts below;
one forward-decl warning each for `design_reviewer`, `general`).

### Deliverable 4 — D13: position body trims (ROOT)

**Artifact-Path (ROOT):** `profiles/positions/empirical_reviewer.md`
(and `profiles/positions/grunt.md` — see note).

**Byte counts (UTF-8 body, frontmatter excluded):**

| position           | before | after | budget | note |
|--------------------|-------:|------:|-------:|------|
| empirical_reviewer |  9 419 | 7 972 |  8 000 | trimmed ≥1 419 B → within budget |
| grunt              |  3 969 | 3 969 |  4 000 | ALREADY within budget → left as-is |
| dev (untouched)    |  5 568 | 5 568 |  6 000 | untouched |

`empirical_reviewer.md` trims (imperative bullets, no narrative, doctrine cited by name —
D13 non-restatement): F129 section compressed to the heading + the load-bearing gate
REPORT-HEADERS grammar, citing `cao-worker-protocols` for the `verify_pin` cadence the skill
already delivers; Offload-box → one-line `box-ops` cite; Stop-and-ask compressed; rule-3 memo
incident narrative dropped; rule-8 callback cites the skill's callback contract (marker + the
verdict-count/zero-decision specifics kept); rule-9 containment compressed; sandbox-probes
compressed.

**ALL AC14 required clauses/markers survived (lint green):**
`<!-- clause:callback-contract -->`, `<!-- clause:containment -->`,
`<!-- clause:never-edit-artifact-branch -->`, heading
`## Frozen Authority Pin protocol (F129)`, heading
`## Reviewer test attachments & suite recommendations (gates.md; AC14)`.

grunt kept `<!-- clause:callback-contract -->`, `<!-- clause:containment -->`,
`<!-- clause:grunt-scope -->`. grunt was already 3 969 ≤ 4 000, so per scope discipline it
was NOT rewritten (budget ratchets down; it already passes).

**`certification:` block — UNTOUCHED (recorded NOTHING; supervisor records).**

**NEW `position_sha` (via `utils.profile_composition.position_sha`, first-16-hex, EXCLUDES
the `certification:` block):**

| position           | old position_sha   | NEW position_sha   |
|--------------------|--------------------|--------------------|
| empirical_reviewer | `2d50a99eb57e9527` | `f03740a66e019558` |
| grunt              | (unchanged)        | `cdce5aefd7cfc2a3` |
| dev (untouched)    | —                  | `5d6bfac1de67e742` |

Because empirical_reviewer's body changed, its `position_sha` changed → the committed
`certification:` rows (still `2d50a99eb57e9527`, PASS/FAIL) are STALE BY DESIGN. The AC15
kiro cell is owed a re-run on the box (blueprint migration step 4). **Supervisor action
required:** record the new empirical_reviewer cert rows keyed on `f03740a66e019558`.

---

## Test status summary

**15 P3-authored tests — ALL GREEN (r2):** AC17 = 7, AC11 = 2, D7 = 6 (a
clean-store legacy-passthrough test added for B1). Combined r2 invocation (ONE
`pytest`, `-p no:randomly`) over the 6 gate-named regression tests + all P3 tests +
the install-service trio:

```
179 passed in 7.39s
```

files: `test/mcp_server/test_f172_display_names.py`, `test/mcp_server/test_fx155_window_name.py`,
`test/services/test_offline_base_registration.py`, `test/mcp_server/test_f497_assign_provider.py`,
`test/mcp_server/test_fork_assign_errors.py`, `test/utils/test_f497_clauses.py`,
`test/services/test_install_service.py`, `test/services/test_f497_install_composition.py`,
`test/services/test_f497_install_guard.py`.

**B1 regression — FIXED and verified in that same run:** the 6 gate-named tests
(`test_f172_display_names` ×2, `test_fx155_window_name` ×3, `test_offline_base_registration`
×1) assign uninstalled legacy names (`kiro_dev`, `codex_profile`) with `_create_terminal`
mocked; under option (b) those pass through (no provider=, not a position file) and the tests
are green. Root cause (r1): the resolver ran a store-dependent legacy check first and
hard-failed uninstalled names `E-UNKNOWN-POSITION` pre-spawn.

**install-service = 86 passed (S2/S3 corrected)** across the three files
(`test_install_service.py` = 63, `test_f497_install_composition.py` + `test_f497_install_guard.py`
= 23), 0 failed.

**5 pre-existing failures — NOT caused by P3 (proven by `git stash` at r1, re-confirmed):**

1. `test/utils/test_f497_resolver.py::test_ac1_legacy_profile_resolves_byte_identically`
   `[codex_design_reviewer]`, `[claude_design_reviewer]`, `[kiro_design_reviewer]`,
   `[kiro_dev]` (4) — `yaml.scanner.ScannerError: mapping values are not allowed` from an
   UNQUOTED colon in the `description:` frontmatter (`--v3):`, `Binding:`) of legacy profiles
   in the SHARED checkout `/home/chao/VScode_projects/cli-subagents/profiles`. Fail
   IDENTICALLY with all P3 code stashed. P3 touches none of those files.
2. `test/utils/test_f497_composition.py::test_r9_recorded_position_sha_matches_helper` (1) —
   the harness walks to the SHARED `profiles/positions/empirical_reviewer.md` (NOT ROOT / NOT
   the fork), whose `certification:` rows are already `PASS`/`FAIL`; the test asserts
   `outcome == "UNCERTIFIED"` (P2 seeding). Shared-checkout fixture mismatch, independent of
   the ROOT trim.

Both harnesses resolve their corpus by an ancestor walk that lands on the shared outer
checkout, so they never read ROOT or the fork worktree in this environment. The full suite
runs on a grok box by the tester (r1 gate: `14051 passed, 18 failed`; of the 18, 5 are the
above shared-corpus items, 7 are box-env/infra (N1), and the 6 D7 regressions are FIXED here).

**Verification tooling note:** `ruff`/`pyright` are not installed in this venv; all changed
source files pass `py_compile`. Targeted runs only on the laptop (per containment).

---

## Deviations

- **B1 fix = option (b), switched from the brief's option (a)** (user/supervisor steering
  2026-08-28). The brief specified option (a) — legacy passthrough for names not
  `<provider>_<position>`-shaped, hard-fail position-shaped misses. Flagged (callback 1674)
  that (a) would hard-fail real legacy names `codex_dev`/`grok_dev` (a `<provider>_<position>`
  of the real `dev` position) on a clean store — the same B1 class. Supervisor switched to
  option (b): engage only on `provider=` OR a bare position file; no shape inference in P3
  (synthesis = P4). Implemented as directed; not a self-initiated deviation.
- Prior rulings folded (unchanged): D7 = resolution-layer-only (live spawn wiring = P4); AC17
  unknown-key = "not reserved AND not id-shaped", id-shaped file-less keys warned + continued
  (D13 clarification). grunt left untouched (already within budget). The `providers:` allowlist
  was already present on the P2 ROOT positions, so no frontmatter add was required.

## SHAs / diffstat

**Git-SHA-fork (r2):** `984e570a` (branch `cao/9a61dd36`, base `b7a1f9d5`; r1 build `b6c0f4ca`)
**Git-SHA-root (r2):** this commit on `cao/f497-p3-root`, lineage
`d841817a` (P3 build) → `ec4f5d50` (gate r1) → **this r2 commit** (base `0821215`).
(S1 fix: the r1 report's stale `7a4349ae` is superseded — that amend-orphan is not in the
branch's actual lineage.)

FORK r2 delta `git diff --stat b6c0f4ca 984e570a` (125 insertions, 88 deletions):
```
 src/cli_agent_orchestrator/utils/agent_profiles.py |  76 ++++++------
 test/mcp_server/test_f497_assign_provider.py       | 137 +++++++++++++--------
```

FORK cumulative `git diff --stat b7a1f9d5 984e570a`:
```
 src/cli_agent_orchestrator/mcp_server/server.py        | 113 ++++-
 src/cli_agent_orchestrator/services/install_service.py |  31 +++
 src/cli_agent_orchestrator/utils/agent_profiles.py     | 118 +++++-
 src/cli_agent_orchestrator/utils/clause_lint.py        | 216 +++++++-
 test/mcp_server/test_f497_assign_provider.py           | 175 ++++++
 test/mcp_server/test_fork_assign_errors.py             |  73 +++
 test/utils/test_f497_clauses.py                        | 137 +++++
```

ROOT r2 delta (this commit): `orchestrator/build-reports/f497-p3-build-report.md` (r2 rewrite).
ROOT cumulative from `0821215`: build report + `profiles/positions/empirical_reviewer.md` trim
+ the r1 gate report.

**Combined r2 test line (ONE invocation):** `179 passed in 7.39s`
(6 gate-named regression tests + all P3 tests + install-service trio; install trio alone = 86).
