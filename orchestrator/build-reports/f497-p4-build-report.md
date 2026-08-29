# F497 P4 build report — position/provider decoupling, migration step 4

Authority: `orchestrator/blueprints/f497-position-provider-decoupling.md` FROZEN r13.
Brief: `orchestrator/tmp/orch/BRIEF-f497-p4.md`. Issue #352, milestone wp-provider-routing.

Lanes:
- FORK: `cao/f1d255f4` (worktree of the fork) — src/tests. Base includes F566 (69bd527a).
- ROOT: `cao/f497-p4-root` (worktree `/home/chao/VScode_projects/cli-subagents/.cao/worktrees/f497-p4`
  from `quirks-merge-train`) — profiles/positions, overlays, routing.toml, _clauses.toml,
  <provider>_general.md.

## Status: IN PROGRESS (checkpointed per family)

### DONE — Fork side (D6 + D9 validator + tests)
- `utils/routing.py` (NEW): D9 routing.toml loader + validator + assign-time resolver.
  - `load_routing_table` — AC7 malformed rejection (kind discriminator cao|in_harness;
    cao row must name provider; in_harness must NOT; missing position/kind rejected).
  - `resolve_routing_binding` — D9/D12 order: (1) provider-cert-first
    (`E-PROVIDER-UNCERTIFIED`), (2) row-clause satisfaction reusing AC14 matcher
    (`E-ROW-CLAUSES-MISSING` naming missing ids), (3) cell cert — non-gate non-PASS →
    `<provider>_general` substitution (fallback_profile + preamble fields), gate non-PASS
    → refusal (no spawn).
  - `cell_certified` — reads the position file `certification:` block, matches at the
    CURRENT (position_sha, overlay_sha) via `profile_composition.position_sha/overlay_sha`.
  - Gate membership DERIVED from clause table (f129-pins + never-edit-artifact-branch),
    never a hard-coded roster (D12).
- `utils/agent_profiles.py`: D6 `<provider>_<position>` synthesis in
  `resolve_assignment_target` (prefers a legacy alias stub for the cell, else synthesizes);
  bare-position-no-provider now consults routing.toml (D9) before the
  `E-POSITION-NEEDS-PROVIDER` hard fail. Legacy passthrough (codex_dev/grok_dev) preserved
  (option-b: engages only on provider= or a bare position file). Added `E-PROVIDER-UNCERTIFIED`
  / `E-ROW-CLAUSES-MISSING` are defined in `routing.py`.
- `mcp_server/server.py` `_assign_impl`: routing-driven path (bare position, provider from
  routing) runs the D9 validator BEFORE terminal creation; refusals fail with no spawn;
  general substitution sets `result["fallback_profile"]` and folds
  `[COLD-FALLBACK position=<pos> cell=<outcome>]` into the ONE preamble line (reuses the
  existing `_cold_fallback_preamble` D12 grammar helper; combined with D10 base=stale
  appends fields, never a second marker). Explicit `provider=` stays an operator override
  (allowlist + D6 synthesis only, no cert/fallback validator — D7).
- `constants.py`: `routing_toml_path()` (CAO_ROUTING_TOML override; default
  agent-store/routing.toml, synced by install.sh — sync line pending).

### DONE — Tests (targeted, local)
- `test/mcp_server/test_f497_routing_d9.py` (NEW, 12 tests): AC7 roundtrip + 5 malformed
  cases; AC18 provider-uncertified, gate refusal, gate clause-missing (named id),
  certified gate binds, non-gate fallback → general, and end-to-end `_assign_impl`
  fallback with single `[COLD-FALLBACK position=dev cell=UNCERTIFIED]` preamble +
  `fallback_profile`.
- `test/mcp_server/test_f497_assign_provider.py` (UPDATED): D6 synthesis assertion
  (`empirical_reviewer`+codex → `codex_empirical_reviewer`); other 6 P3 cases unchanged.

Test commands + counts (see "Test evidence" for exit codes):
- `uv run pytest test/mcp_server/test_f497_routing_d9.py -q` → 12 passed
- `uv run pytest test/mcp_server/test_f497_assign_provider.py -q` → 7 passed

### TODO — Root side (families + routing.toml + general personas)
- [ ] `general` position + `<provider>_general.md` per provider in routing.toml (D12/AC18).
- [ ] `design_reviewer` family (positions + overlays + alias stubs).
- [ ] `oracle` + `base` families.
- [ ] identity positions (secretary, grok_doc_keeper, chao_supervisor, claude_blueprint_maker*).
- [ ] `orchestrator/routing.toml` (D9) with kind discriminator + in_harness opus DESIGN row.
- [ ] install.sh sync of routing.toml into agent-store.

### TODO — Certification (AC15, box)
- [ ] box-run cell smoke per extracted (position, provider) cell → PASS/FAIL/UNREACHABLE
  rows in each position file `certification:` block (D8 key).
- [ ] Owed from P3: empirical_reviewer position_sha moved (2d50a99e→f03740a6) → kiro AC15
  re-run; record new cert rows.

## AC map (running)
| AC | Where | Status |
|----|-------|--------|
| AC7 (routing validator + malformed) | routing.py / test_f497_routing_d9 | DONE (generator=P5) |
| AC14 (clause reuse in D9) | routing.py `_present_clause_ids` reuses ClauseRule.matches | DONE |
| AC15 (cell smoke) | box run + position cert blocks | TODO |
| AC17 (budgets) | clause_lint.lint_budgets (P3) + new personas | pending new personas |
| AC18 (general mandatory + non-gate) | routing.py + _assign_impl + tests | DONE (fork) / personas TODO |

## Deviations
- Implementation note (not a frozen-row deviation): the blueprint authors routing.toml at
  `orchestrator/routing.toml` but is silent on the RUNTIME resolution path for the
  server-side D9 validator. Resolved additively via `routing_toml_path()` +
  `CAO_ROUTING_TOML`, synced into agent-store by install.sh (same sibling-sync pattern D3
  uses for positions/overlays). No frozen decision row contradicted.
