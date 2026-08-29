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


## r2 — root-side dump completion + AC15 box cert (lane 5ffd966f, fork commit 9d4a77f2)

This section completes the ROOT-side work the P4 fork commit (d5fd6520) deferred
("root-lane personas + routing.toml + AC15 cert to follow"). The fork lane cannot
write under the root repo (fx121 hook), so every root-destined file lives in the
dump `/data/cao-scratch/f497-p4-root/` and is catalogued in `MANIFEST.md`
(30 rows; sha256 + NEW/MODIFIED per file). The supervisor applies the dump to the
root repo at merge time.

### Step 1 — cert-sha fix (`profiles/positions/empirical_reviewer.md`)
The recorded `certification:` shas drifted: the persona body had moved but both
rows still recorded `position_sha 2d50a99eb57e9527`, while the r9 helper recomputes
`f03740a66e019558` over the current body (cert block excluded, r9(a)). Fix: based on
`patches/empirical_reviewer.orig.md` (byte-identical to root live), changed ONLY the
cert lines — both rows `position_sha → f03740a66e019558`. `patches/cert-fix.patch` =
`diff -u orig fixed`. Verified: `position_sha(body, meta)` = `f03740a66e019558`,
matching both recorded rows.

### Step 2 — F573 #430 gate Report-headers block
`profiles/kiro_reviewer.md` + `profiles/codex_empirical_reviewer.md` (the two
`extends: empirical_reviewer` alias stubs) had EMPTY bodies since F497 P2 (19bda3e),
so `scripts/test-gated-merge.sh` WGM-AC15 (raw-file grep, byte-identity across four
reviewer profiles) went red. Fix: added the F129 "Report headers (mandatory)" block
— byte-identical to `kiro_design_reviewer.md` / `claude_design_reviewer.md` — to the
stub bodies. Frontmatter unchanged (5-key stub). The resolver reads stub METADATA
only (`utils/agent_profiles.resolve_agent_profile`), so the added body does NOT enter
composition (verified: 22/22 `test_f497_composition.py` pass, composed persona
unchanged). AC15-faithful extra: the dump also extracted the **design_reviewer**
family to stubs (`kiro_design_reviewer` / `codex_design_reviewer` →
`extends: design_reviewer`), which WGM-AC15 ALSO checks — so the same block was added
to both, keeping the full 4-way byte-identity green after the dump lands
(supervisor-confirmed 2026-08-29). WGM-AC15 4-way check simulated on the dump copies:
PASS.

### Step 3 — verify
Verify store `CAO_HOME_DIR=/data/cao-scratch/f497-p4/verify-store`, agent-store
refreshed from ROOT baseline + DUMP overlay (`positions/`, `overlays/`, flat stubs).
```
CAO_HOME_DIR=/data/cao-scratch/f497-p4/verify-store \
CAO_F497_PROFILES_DIR=/data/cao-scratch/f497-p4/verify-store/agent-store \
uv run pytest test -k "r9_recorded_position_sha or routing" -q
→ 39 passed, 2 skipped
```
Includes `test_r9_recorded_position_sha_matches_helper` (green) and the 12
`test_f497_routing_d9.py` AC7/AC18 tests. `routing.toml` also validated through the
P4 loader: 11 bindings, providers {cline_cli, codex, grok_cli, kiro_cli}, the
in_harness opus DESIGN row round-trips.

Test relax (fork, this commit): `test_f497_composition.py::test_r9_recorded_position_sha_matches_helper`
asserted every cert row `== "UNCERTIFIED"` (a P2 seeding invariant). P4 records real
AC15 results, so per the blueprint AC15 three-outcome contract (PASS/FAIL/UNCERTIFIED;
only FAIL blocks merge) the assertion was widened to
`row["outcome"] in {"PASS","FAIL","UNCERTIFIED"}` (sha assertion kept). Supervisor
approved (Option 1, 2026-08-29).

### Step 4 — MANIFEST
`/data/cao-scratch/f497-p4-root/MANIFEST.md` — 30 rows, every dump file with its
destination under `cli-subagents/`, sha256, and NEW/MODIFIED/dump-internal status.

### Step 5b — AC15 cell smoke (box)
Driver `probes/f497-ac15-cell-smoke.sh` (unchanged, sha ae93fc0e) run via
`scripts/box-run.sh f497-ac15 -- 'bash ~/f497-p4-ac15-payload.sh'`. Fork code
delivered by git (pushed `cao/5ffd966f` @ 9d4a77f2, checked out in the box's
`~/cli-subagents/cli-agent-orchestrator`); root-side dump files rsynced ONLY from
`/data/cao-scratch/f497-p4-root/` onto the box's `~/cli-subagents/`. `REPO=$HOME/cli-subagents`.

Per-cell outcomes (D8 key position_sha=f03740a66e019558):

| cell (position, provider) via alias | box | outcome | wall | evidence |
|---|---|---|---|---|
| (empirical_reviewer, kiro_cli) via `kiro_reviewer` | grok-box-8 | **FAIL** | 158s | composed persona OK (context_file=PASS); spawn 500 "Kiro CLI initialization timed out with --legacy-ui (yolo mode)" |
| (empirical_reviewer, kiro_cli) via `kiro_reviewer` | grok-box-5 (retry) | **FAIL** | 93s | same kiro init-timeout 500; box-5 also missed positions/overlays store sync (context_file=FAIL) — box/install-env, not persona |
| (empirical_reviewer, codex) via `codex_empirical_reviewer` | grok-box-8 | **FAIL** | 96s | composed persona OK (context_file=PASS); `seed_resume_bootstrap failed: seed_exec_failed` (F530 class) |

Logs: `/data/cao-scratch/f497-p4-ac15.log` (grok-box-8), `/data/cao-scratch/f497-p4-ac15-box5.log` (grok-box-5).
Cert rows in the dump `empirical_reviewer.md` updated to the live results (both FAIL);
`cert-fix.patch` + MANIFEST shas regenerated. position_sha unchanged (cert block
excluded from the hash by design).

**AC15 finding (BLOCKING per "only FAIL blocks the merge"):** BOTH empirical_reviewer
cells record FAIL, but NEITHER is a persona/composition defect — composition passed
(context_file=PASS on box-8 for both). The kiro cell fails at CLI init
("--legacy-ui (yolo mode) timed out"), reproduced on TWO independent boxes (8 and 5),
so it is a real kiro-spawn issue, not a single-box auth flake — and a regression vs
the P2 probe's kiro PASS (grok-box-4, which required manual dismissal of the Kiro 3.0
upgrade prompt; the auto-answer did not unblock init this time). The codex cell fails
at seed exec (seed_exec_failed / F530 class), unchanged from P2. Recommendation for
the gate/supervisor: these FAILs are spawn-infrastructure (kiro legacy-ui yolo init,
codex seed), not F497 P4 persona work; the routing validator (D9) already refuses to
bind a non-PASS cell for production dispatch, so the extraction is safe to merge while
the cells stay uncertified/FAIL — but per AC15's literal "only FAIL blocks the merge"
this is the empirical gate's call. One bounded retry (box-5) was used; no further
retries taken per the stop-and-ask directive.

### black/isort/mypy (touched .py: `test/utils/test_f497_composition.py`)
- `black --check` → clean (1 file unchanged).
- `isort --check-only` → flags a PRE-EXISTING misplaced `import os` (present at HEAD,
  identical diff before/after this edit; NOT introduced by P4, imports untouched).
  Left as-is per scope discipline (no drive-by refactor).
- `mypy --strict` — not applicable (test module; no new source touched this lane).

### Box-actions ledger (F497 P4 AC15 smoke)
- `box-run.sh f497-ac15 -- 'bash ~/f497-p4-ac15-payload.sh'` on grok-box-8 (pinned
  `CAO_BOXES=box@grok-box-8`, watchdog 1800s). Payload: git fetch+checkout fork
  9d4a77f2, run probe, restore fork to main@22473fd3. probe_rc=0; slot released.
- `box-run.sh f497-ac15-r2 -- 'bash ~/f497-p4-ac15-payload.sh'` on grok-box-5 (pinned;
  one bounded retry). Payload restored fork to detached 7f9b7db5. probe_rc=0; slot
  released. (A prior attempt pinned to grok-box-4 was aborted — box busy with
  upstream-merge-suite — no slot acquired, no state change.)
- Raw ssh (READ-ONLY peeks): idleness probes (`pgrep -fc pytest; uptime`) on
  grok-box-3..8; harness/server-log tails on box-8/box-5. No raw ssh mutated box state.
- rsync (laptop→box state prep, NOT via box-run.sh — acknowledged deviation, low-risk):
  dump `profiles/` + `orchestrator/routing.toml` onto grok-box-8 and grok-box-5
  `~/cli-subagents/`; probe script onto grok-box-5 (absent there). scp payload to
  box-8/box-5.
- Checkout SHAs left: grok-box-8 fork → main @ 22473fd3 (clean, restored);
  grok-box-5 fork → detached 7f9b7db5 (restored to original). Box root repos
  (`~/cli-subagents`) carry the rsynced dump profiles (disposable trial VMs; the
  probe's own cleanup removed CAO_HOME scratch + restored kiro hooks/agents + uv tool).
- Temp files: `~/f497-p4-ac15-payload.sh` left on box-8/box-5 (harmless; disposable
  VMs). box-1 FROZEN — never touched. box-6/box-7 asleep/unreachable — not used.
- Deviations: (a) rsync used for laptop→box dump delivery (box-run.sh cannot carry a
  local→remote file transfer); (b) box's `~/cli-subagents` left with rsynced dump
  profiles rather than pristine (disposable VM, no authoritative state).
