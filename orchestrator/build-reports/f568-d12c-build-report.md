# F568 #425 D12c build report — children ledger + `delegating (N)` fleet state

- **Lane:** isolated worktree `.cao/worktrees/e5f4f5fd`, branch `cao/e5f4f5fd`,
  forked off fork main `a7e552a6` (`git fetch origin main && git reset --hard
  origin/main` in the worktree).
- **Authority:** blueprint `orchestrator/blueprints/f506-f507-status-truth.md`
  §10 FROZEN r16 — D12a (children ledger), D12b (bound realised via D12d),
  D12c (fleet projection). D12d (spinner veto, `80ddba99`/`bec2e369`) is
  MERGED and was NOT touched — this lane builds ON its already-shipped
  `_capture` sampling of `children_count` and its rule-3 `pane_delta_delegating`
  order.
- **Scope discipline:** no new regex in `get_status`; no second capture / second
  metadata read per tick (the fleet projection reads the metadata dict the loop
  already holds); frozen AC4/AC21 untouched; liveness-only-downgrades untouched;
  no change to the frozen D12d read path (`pane_liveness._children_count_from_metadata`).

## What D12c is (and is not)

D12d already shipped the READ side: `_capture` samples `children_count` from the
metadata dict it holds, and `fuse_status` rule-3 returns `pane_delta_delegating`
for `children_count > 0` (this IS D12b's "the pane-delta hold never expires while
children > 0" — there is nothing withholding). What was missing:

1. **D12a — the ledger WRITE side.** The `children` list on the terminal row that
   D12d counts had no writer. This lane adds the durable ledger + the seat's own
   Claude Code hooks that register/release entries.
2. **D12c — the fleet projection.** The `delegating: bool` + `children_count: int`
   sibling keys on the `fleet` JSON row, and the `delegating (N)` render in the
   `cao agents` listing.

## Files changed

| File | Change |
|---|---|
| `src/cli_agent_orchestrator/clients/database.py` | D12a ledger mutators: `register_terminal_child` / `release_terminal_child` (single-txn read-modify-write on the free-form `metadata_json["children"]` list — the SAME list D12d counts), `_prune_stale_children` (staleness bound), `_load_free_form_metadata`, `_children_ledger_max_age_s` (config `liveness.children_ledger_max_age_s`, default 3600s). |
| `src/cli_agent_orchestrator/hooks/children_ledger.py` | **NEW.** The seat's own hook transport — a `question_marker.py` sibling (same containment gate on `CAO_TERMINAL_ID`, dead-letter, `get_local_bearer`, `return 0` always). `_classify`: `PreToolUse` matcher `Agent|Task` ⇒ `register`, `SubagentStop` ⇒ `release`. POSTs to the children-ledger endpoint. |
| `src/cli_agent_orchestrator/api/main.py` | `ChildrenLedgerRequest` model + `POST /terminals/{id}/children-ledger` endpoint (404 unknown terminal, 400 body/route id mismatch + register-without-child_id, `require_any_scope(WRITE, ADMIN)`), returning `children_count`. |
| `src/cli_agent_orchestrator/providers/claude_code.py` | D12a overlay: additive `PreToolUse` matcher `Agent|Task` block + `SubagentStop` block wired to the ledger hook in `_write_terminal_settings`. The F507 marker blocks are untouched (the AskUserQuestion `PreToolUse` block stays index [0]). |
| `src/cli_agent_orchestrator/services/fleet_service.py` | D12c projection at the `:172-173` seam: `_children_count_from_row` (reads `row["metadata"]["children"]` already in hand — no extra DB call) + `delegating`/`children_count` sibling keys, computed over the FINAL projected status (after all three ERROR overrides). |
| `src/cli_agent_orchestrator/cli/commands/agents.py` | `_render_fleet` renders `delegating (N)` in the STATUS cell when the row is `delegating`. |
| `test/clients/test_children_ledger.py` | NEW — ledger arithmetic, idempotence, FIFO release, staleness prune, sibling-key preservation, read-path parity with the frozen D12d counter. |
| `test/api/test_children_ledger_endpoint.py` | NEW — endpoint routing/validation (404/400/422/register-count/release-no-id). |
| `test/hooks/test_children_ledger_hook.py` | NEW — classify matrix, containment, fail-open dead-letter, payload shape. |
| `test/services/test_fleet_delegating.py` | NEW — AC-F568-8 projection rows (IDLE/COMPLETED ⇒ delegating; PROCESSING ⇒ working; ERROR override ⇒ never delegating); AC-F568-4 JSON keys. |
| `test/providers/test_children_ledger_overlay.py` | NEW — D12a overlay additivity (register/release blocks present, F507 marker block untouched). |
| `test/cli/commands/test_delegating_render.py` | NEW — `delegating (N)` render vs working. |

`git diff --stat`: 12 files, +1158 / −3.

## The projection (D12c, AC-F568-8 / AC-F568-4)

`delegating = children_count > 0 AND final_status ∈ {IDLE, COMPLETED}`, where
`final_status` is the value AFTER the three ERROR overrides at the seam
(recovery_state / missing-window / init_health=failed). So:

- IDLE/COMPLETED + children>0 ⇒ `delegating (N)` (r11 S2 satisfied).
- PROCESSING + children>0 ⇒ stays `working` (`delegating=False`) — the seat's own
  turn is open.
- ERROR/quarantined + children>0 ⇒ never `delegating` (asserted with
  `recovery_state="failed"` forcing the seam's ERROR override).

The raw `status` enum value is UNCHANGED (r11 S3): `delegating`/`children_count`
are additive sibling keys mirroring `fusion_changed`/`fusion_reason` at the same
seam. No new persisted status value, no schema change, no watchdog coupling.

## The ledger (D12a) — where `children_count` comes from

- **Location:** `metadata_json["children"]`, a list of `{id, started_at}`. This is
  EXACTLY the list D12d's frozen READ side counts
  (`pane_liveness._children_count_from_metadata` →
  `metadata["metadata"]["children"]` → `len`). Choosing this location keeps the
  frozen read path untouched.
- **Register (increment):** `PreToolUse` on `Agent|Task` appends one entry
  (idempotent on `child_id` — a duplicate PreToolUse edge does not double-count).
- **Release (decrement):** `SubagentStop` removes the matching entry by id, or —
  when the stop carries no reliable id — pops the OLDEST entry (FIFO,
  count-correct because CC pairs each dispatch with exactly one stop).
- **Staleness bound:** a missed `SubagentStop` must not pin the row `delegating`
  forever. Each entry's `started_at` is checked against
  `liveness.children_ledger_max_age_s` (default 3600s) on every register/release;
  a provably-old entry is pruned. Enforced at WRITE time so the frozen READ side
  stays a pure `len`. An entry with a missing/malformed `started_at` is KEPT
  (fail toward in-flight — dropping a live child would under-count and
  prematurely admit an idle bound).
- **Additivity:** both mutators preserve every sibling free-form key and the
  reserved `cao` system namespace (a single read-modify-write inside one
  `SessionLocal` txn; cache invalidated after).

## Targeted tests (laptop)

```
$ uv run pytest -q \
    test/clients/test_children_ledger.py test/api/test_children_ledger_endpoint.py \
    test/hooks/test_children_ledger_hook.py test/services/test_fleet_delegating.py \
    test/providers/test_children_ledger_overlay.py test/cli/commands/test_delegating_render.py \
    test/api/test_interaction_marker.py test/hooks/test_question_marker_hook.py \
    test/providers/test_question_marker_overlay.py test/services/test_status_fusion.py \
    test/services/test_pane_liveness.py test/services/test_f72_fleet_lifecycle.py
150 passed
```

The frozen D12d suites (`test_status_fusion.py`, `test_pane_liveness.py`), the
F507 overlay/marker suites, and the fleet-lifecycle regression are all green and
UNMODIFIED — the overlay additivity assertion (`PreToolUse[0]` stays
AskUserQuestion) proves the D12a blocks did not disturb F507. No laptop full
suite was run, per the brief.

## Mutation ledger — 5/5 KILLED (ledger arithmetic)

Self-run driver (`/data/cao-scratch/e5f4f5fd/mutate.py`): each mutation applied to
a pristine byte-copy of `database.py`, anchor-count asserted (== 1), killing
tests run, source restored, `__pycache__` purged. Nonzero exit = KILLED.

| # | Mutation | Result |
|---:|---|---|
| m1 | Drop the register no-double-count guard (over-count on duplicate id) | **KILLED** (exit 1) |
| m2 | Release-by-id never deletes the matched entry (missing decrement) | **KILLED** (exit 1) |
| m3 | Release-without-id no longer pops the oldest (missing decrement) | **KILLED** (exit 1) |
| m4 | Staleness prune KEEPS stale entries instead of dropping | **KILLED** (exit 1) |
| m5 | `register` returns `len+1` (N off-by-one on the reported count) | **KILLED** (exit 1) |

Post-run `database.py` SHA-256 equals the pristine
`7b583bed181c78317ad6ce5153e77c4a8c1673c457eed9dcf259510db9e69255`.

## Lint / types

- `black --line-length 100` + `isort --profile black --line-length 100`: clean on
  all 12 changed files. `git diff --check`: clean.
- `mypy --strict`: `hooks/children_ledger.py` is strict-clean (0 errors). The new
  code ranges in `database.py` (ledger mutators), `api/main.py` (model +
  endpoint) and `fleet_service.py` (projection helper + keys) report ZERO mypy
  errors. The one `fleet_service.py` `datetime.__sub__` error is PRE-EXISTING
  (present at base `a7e552a6`, in the untouched `since_last_input` block). No
  drive-by typing refactor (§5 Do-NOTs).

## Spec-silent choices (minimal readings; noted per the brief)

1. **Dispatch edge = `PreToolUse` matcher `Agent|Task`, not `PostToolUse`.** The
   D12a blueprint parenthetical wrote "`PostToolUse` matcher covering in-harness
   Agent dispatch", but `PostToolUse` fires only AFTER the tool completes — the
   wrong edge for a "child IN FLIGHT" fact (the child is already gone by then).
   The blueprint EXPLICITLY defers the exact event string to the empirical gate
   ("the exact event string is settled by the EMPIRICAL gate — r11 N1"). The
   minimal correct reading that satisfies the stated intent (register at
   dispatch, release at stop) is `PreToolUse`. `Agent` is the current tool name
   (CC 2.1.63+), `Task` the historical one; the live root `.claude/settings.json`
   wires `Task|Agent` — this lane uses `Agent|Task`.
2. **Ledger at free-form `metadata_json["children"]` (not the `cao` namespace).**
   Forced by the frozen D12d read path, which reads the free-form `children` key.
   Trade-off: the worker metadata full-replace path (`update_terminal_metadata`)
   preserves only the `cao` namespace, so a supervisor that calls the
   `update_metadata` MCP tool would clobber its own ledger. Accepted because
   (a) the ledger is written by the seat's own hooks, (b) supervisors rarely
   full-replace their metadata, (c) moving to `cao.children` would require
   editing the frozen, merged, gated D12d read path (out of scope). The mutators
   themselves are additive and never clobber sibling keys.
3. **Count-based inc/dec.** `SubagentStop` id fields vary across CC versions, so
   release pops the oldest entry when no id is present (count-correct). `child_id`
   (from `tool_call_id`) is stored for observability but not required for the
   decrement.
4. **Staleness bound default 3600s**, configurable via
   `liveness.children_ledger_max_age_s`, enforced at write time. Deliberately
   generous (the motivating incident had a 10+ min subagent); it exists only so a
   missed release cannot pin the row indefinitely.

## Containment

No recursive grep of `~` or `~/.claude`; no `claude_code` children spawned; no
hooks/fleet-label changes outside the D12a overlay this spec mandates;
`~/sudo_passwd.txt` never read. All edits confined to the provisioned worktree
`.cao/worktrees/e5f4f5fd`. Mutation scratch under `/data/cao-scratch/e5f4f5fd/`.
No box run (targeted laptop tests only, per the brief).
