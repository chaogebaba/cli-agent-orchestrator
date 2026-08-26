# Build report — F507 #362 + F506 #361 (status truth, one merge train)

Blueprint: FROZEN r9 `orchestrator/blueprints/f506-f507-status-truth.md`
Fork picks (user 2026-08-26): A=(i) sample ALL live terminals · B=(iii) matcher-scope Notification to permission_prompt|elicitation_dialog (full D7 set) · C=defer F482.
Build order honored: **F507 first, F506 second**, one train.

## Branch + commits

- Branch: `cao/f2f0797c` (isolated fork worktree `.cao/worktrees/f2f0797c`)
- HEAD: `26e54b94` (pushed to `origin/cao/f2f0797c`)
- Base: `b5425ca5` (quirks-merge-train tip at spawn)

Commits (oldest→newest):
| SHA | Subject |
|---|---|
| `49bd63a4` | docs(f507): fix base.py resolve_native_status docstring — 10s both branches, not 300s (§7a rider) |
| `95841719` | feat(f507 #362): interaction-marker hook, endpoint, question_state, overlay, config |
| `d7947730` | test(f507 #362): AC7-9, AC10/14/15, AC11/14, AC12 (24 tests) |
| `f5cad0bd` | feat(f506 #361): pane-delta liveness fusion + D13 hatch + fleet marker |
| `9664a146` | chore(f506): regen trace_manifest (hits=39 unchanged) |
| `3ebd33b0` | fix(f506): mypy --strict (Callable clock type, narrow monitor local) |
| `26e54b94` | test(f507): valid 8-hex TerminalId in AC11 codex fixture |

## Diff summary (b5425ca5..HEAD)

25 files, +2368 / −68. New source: `services/pane_liveness.py`, `services/question_state.py`, `hooks/question_marker.py`. New tests: `test_pane_liveness.py`, `test_question_state.py`, `test_status_fusion.py`, `test_f506_admission_seam.py`, `test_question_marker_hook.py`, `test_question_marker_overlay.py`, `test_interaction_marker.py`. Edited seams: `status_monitor.py`, `receiver_state_view.py`, `stalled_callback_watchdog.py`, `inbox_service.py`, `fleet_service.py`, `cli/commands/agents.py`, `claude_code.py`, `base.py`, `config_service.py`, `api/main.py`, `trace_manifest.txt`.

## Test evidence

**Full suite on box@cursor-4** (offload; M8 — never on laptop) at HEAD `3ebd33b0`:
`uv run pytest -q -m "not live and not e2e"` → **13826 passed, 5 failed, 52 skipped, 14 xfailed, 1 xpassed** in 398.69s.

Failure triage (all 5 accounted for):
| Failure | Cause | Resolution |
|---|---|---|
| `test_interaction_marker::test_ac11_...codex...` | MINE — used invalid `TerminalId` `codexterm` (route param is `^[a-f0-9]{8}$`) → 422 | Fixed in `26e54b94` (use `c0de1234`); passes locally (6/6) |
| `test_suite_slot::test_sample_records_child_process` | Environmental — suite-slot plugin child-process sampling under concurrent box load | PASSES locally (45/45 with sim) |
| `test_suite_slot::test_sample_ledger_monotonic_growth` | Environmental — same | PASSES locally |
| `test_sim_substrate::test_600_virtual_seconds_under_2_real_seconds` | Environmental — <2s wall-clock perf assertion; box was concurrently running f497 jobs | PASSES locally |
| `test_wpdt_delivery_truth::test_doctrine_arming_section_exists` | Pre-existing — asserts `doctrine/sections/shared/ws-arming.md` exists in ROOT repo; absent on box layout; unrelated to F506/F507 | Not my scope |

Net: **zero regressions attributable to F506/F507**; the one genuine failure was my test's invalid fixture id, now fixed and reverified locally. The 4 others are environmental/root-layout, confirmed green locally where reproducible.

**Targeted suites (worktree, laptop — unit/targeted only per M8):** all green.
- `test_status_fusion.py` 10 · `test_pane_liveness.py` 13 (incl AC1 grep) · `test_f506_admission_seam.py` (AC5/13/18) · `test_question_state.py` · `test_question_marker_hook.py` · `test_question_marker_overlay.py` · `test_interaction_marker.py` 6
- `test_stalled_callback_watchdog.py` 72 (incl the 4 fingerprint tests, AC2) · `test_fx181_quiescence_watchdog.py` 68 · `test_status_monitor.py` 46 · `test_inbox_service.py` · `test_seam_parity_promotion.py` 39 · `test_stage0_flip_machinery.py` + `test_cli_verify.py` 46 (manifest) · `test_wpm2_delivery_soundness` + `test_f72_fleet_lifecycle` + `test_f228b` batch 240.
- mypy --strict on the new modules (pane_liveness, question_state, receiver_state_view, question_marker): clean. status_monitor's pre-existing object-typed errors are untouched (none at fusion edit sites).

## Per-AC evidence

| AC | What | Evidence |
|---|---|---|
| AC1 | PaneLivenessService is the only `get_history` liveness caller | `test_pane_liveness::test_ac1_watchdog_has_no_get_history_in_refresh` (grep body) + `test_ac1_pane_liveness_is_a_get_history_caller` |
| AC2 | Watchdog byte-unchanged after extraction | 4 fingerprint tests in `test_stalled_callback_watchdog.py` pass; updated only at the injection point (patch `clients.database.get_terminal_metadata` + `list_all_terminals`) |
| AC3 | Liveness downgrade fires (PROCESSING + pane_delta + fusion_changed) | `test_status_fusion::test_ac3_liveness_downgrade_fires` |
| AC4 | Liveness never promotes (frozen 100 samples ⇒ PROCESSING, reason None) | `test_status_fusion::test_ac4_liveness_never_promotes` |
| AC5 | §1 regression — marker open + byte-stable ⇒ WAITING at admission seam; marker absent ⇒ IDLE | `test_f506_admission_seam::test_ac5_marker_open_bytestable_resolves_waiting` + `..._marker_absent_...resolves_idle` |
| AC6 | Frozen-fp/live-process readable by wedge without 2nd capture | `test_pane_liveness::test_ac6_unchanged_for_s_readable_via_peek`; wedge msg reads `peek()` |
| AC7 | Hook containment (no CAO_TERMINAL_ID ⇒ exit 0, 0 HTTP, 0 dead-letter) | `test_question_marker_hook::test_ac7_containment_no_terminal_id_zero_side_effects` |
| AC8 | Hook fail-open (unreachable ⇒ exit 0, 1 dead-letter line) | `test_ac8_fail_open_unreachable_server` |
| AC9 | Idempotence/storm (50 opens ⇒ 1 POST per cooldown) | `test_ac9_storm_control_one_post_per_cooldown` |
| AC10 | Lost-clear healing (transcript closed ⇒ reconcile clears) | `test_question_state::test_ac10_lost_clear_heals_from_transcript` |
| AC11 | Marker channel provider-agnostic (codex terminal) | `test_interaction_marker::test_ac11_provider_agnostic_codex_no_claude_field` |
| AC12 | Overlay additivity (SessionStart intact + new blocks) | `test_question_marker_overlay.py` (5 tests); `test_session_brief_contract` canary still green |
| AC13 | Fusion never fights force_status / provider WAITING | `test_f506_admission_seam::test_ac13_force_status_waiting_not_cleared_by_stable_pane` |
| AC14 | A1 tolerance + TTL (pre-transcript 200 not 400; holds; clears once at TTL) | `test_interaction_marker::test_ac14_pre_transcript_marker_returns_200_not_400` + `test_question_state::test_ac14_unreadable_holds_then_ttl_clears_once` |
| AC15 | Layer-2 parse id-matching (open/closed/id-not-positional) | `test_question_state` AC15 tests (4) |
| AC16 | Fresh-path fusion (get_raw_status bare status; follow-on reports reason) | `test_status_fusion::test_ac20a...` (get_raw_status path) + fusion wired at `return fresh`/`return cached` |
| AC17 | Episode-free sampling | `test_pane_liveness::test_ac17_observe_works_without_any_episode` + watchdog Fork A widening |
| AC18 | Parity honesty (fusion-only diff ⇒ zero mismatch) | `test_f506_admission_seam::test_ac18_parity_zero_mismatch_on_fusion_only_difference` |
| AC19 | Fusion leaves publish-time fields (status_gen/seqs) | `test_status_fusion::test_ac19_fusion_leaves_publish_time_fields` |
| AC20 | No-evidence behavior (a/b/c) | `test_ac20a_before_first_sample_no_downgrade` + `test_ac20c_marker_raise_is_sampler_independent` |
| AC21 | Fusion idempotence (status AND reason) | `test_status_fusion::test_ac21_idempotence_status_and_reason` |
| AC22 | Pane-hold bound (expire once + admit pane_delta_expired; arm c never arms on PROCESSING) | `test_ac22_hold_bound_expires_once_then_admits` + `test_ac22_arm_c_processing_never_arms_bound` |

Pass count: **22/22 ACs covered by passing tests** (AC5 both arms + third-arm mechanism via D13; AC16 via get_raw_status wiring).

## Fork/decision conformance

- Fork A (i): watchdog `refresh_screen_fingerprints` widened to `list_all_terminals ∪ armed episodes`; F351 early-continue relaxed to still run the sampler + tick_wedge with no episode. F351 idle-backoff tests re-derived (not just re-run).
- Fork B (iii): overlay Notification matcher = `permission_prompt|elicitation_dialog|elicitation_url_dialog|elicitation_complete|elicitation_response`; PreToolUse `AskUserQuestion`; clear via PostToolUse/PostToolUseFailure `AskUserQuestion` + Stop (no matcher). Module classifies internally; excluded types (idle_prompt/auth_success) dropped.
- Fork C: F482 deferred. `get_local_bearer()` returns None under auth-off (default) so layer 1 works today; auth-on degrades to layers 2/3 (documented).
- D1 sampler ownership (a): single sampler in pane_liveness; watchdog delegates. D2 read-time fusion. D3 K=3 (`liveness.stable_samples`). D4 downgrade-only. D5 additive-only on WAITING. D11 marker TTL 300. D12 pane-hold bound 300. D13 s4_initial hatch excludes ANY WAITING at inbox_service.

## Deviations (require sign-off)

**None to the design/mechanism.** Precision notes:
1. **Overlay Notification block**: I placed the open AND clear notification_types in ONE Notification matcher block (the hook module classifies each into open/clear). This is faithful to D7 (both edge sets must reach the module) and keeps the settings additive. Not a design change — flagging for visibility.
2. **`test_session_brief_contract` literal-bytes canary**: the blueprint (§7 test-break list) predicted this would need a deliberate update. It did NOT — its assertion only pins the `SessionStart` hooks list (`len==1` + transcript_binding), which my additive overlay leaves byte-identical. No change made; AC12 satisfied. Flagging because the blueprint expected an edit here.
3. **Config keys**: added `liveness.stable_samples`/`pane_delta_max_hold_s`/`question_marker_ttl_s` to `ENV_REGISTRY` only (not `_OWNED_DEFAULTS`), matching the existing `liveness.session_confirm_samples` precedent; defaults are passed explicitly at every `ConfigService.get` call site, so resolution is correct.

## box-actions ledger (box@cursor-4)

- `box-run.sh f506-f507-suite -- 'cd ~/cli-subagents/cli-agent-orchestrator && git fetch origin cao/f2f0797c:cao/f2f0797c && git checkout cao/f2f0797c && … uv run pytest -q -m "not live and not e2e" | tee /tmp/f506-f507-suite-run.txt'` — full suite (13826 passed / 5 failed, triaged above). Watchdog 7200s.
- `box-run.sh f506-peek -- 'grep … /tmp/f506-f507-suite-run.txt'` — READ-ONLY peek at failure detail.
- `box-run.sh f506-peek2 -- 'grep … /tmp/f506-f507-suite-run.txt'` — READ-ONLY peek.
- Raw ssh: none.
- Checkout SHA left on box: `cao/f2f0797c` @ `3ebd33b0` (the suite-run SHA). **DEVIATION (guard-forced):** I could NOT restore the box to `quirks-merge-train` — the local fx121 PreToolUse guard denies `git checkout <other-branch>` / `git branch -D` / `git reset`/`git merge` inside the box-run command string (it evaluates git-mutating verbs against the laptop cwd and cannot tell they run on the remote box). Box repo is left on a valid pushed branch at the tested SHA; no dirty state introduced beyond the checkout. Temp file left on box: `/tmp/f506-f507-suite-run.txt` (in /tmp, ephemeral).
- Env mutations on box: none (no installs/lockfile changes; `uv run` used the pre-synced venv).
- The AC11 test fix (`26e54b94`) was reverified LOCALLY (6/6) rather than re-running the full 6.6-min box suite (capture-once discipline); the fix is a one-line fixture-id change with no source impact.

## Activation note (for close comments)

Per §8: overlay is written once at spawn; workers alive across `install.sh`+`systemctl --user restart cao-server` keep their old overlay and produce no markers (fall through to layers 2/3). Only terminals spawned after restart get layer 1. This train rotates `seam_parity.build_identity()` for the 4 changed hashed modules — all parity ops restart evidence collection from zero (intended).



---

## Rebase addendum (pin-before-gate ceremony)

Rebased onto CURRENT fork main **`b9be7a87`** ("Merge 'cao/6ea01470' into main (F244 gated)"), which landed three merges since base `b5425ca5`: F514 teammate_push_service, F512/513 (mcp_server/server.py + session_lifecycle_lease.py + terminal_service.py delete path + F72 test), F497-P1 resolver files.

**Rebase outcome: CLEAN — zero conflicts.** All 7 commits replayed. My only file overlapping main's changeset was `api/main.py` (main +9 lines; my InteractionMarkerRequest model + endpoint) — git auto-merged (disjoint regions); endpoint + model both verified present post-rebase. I did NOT touch `terminal_service.py` (main's delete-path change) or `test_f72_fleet_lifecycle.py` (main's +17), so the flagged conflict-prone files produced no conflict from my side. `trace_manifest.txt`: main did NOT modify it, and `cao verify manifest --regen` post-rebase reports `files_touched=0 changed=no` (no consumer-module line shift) — no hand edits, no regen needed (F492 discipline moot here).

New commit chain (on top of `b9be7a87`):
| SHA | Subject |
|---|---|
| `516cb0e4` | docs(f507): base.py docstring rider |
| `c31c36dd` | feat(f507 #362): hook + endpoint + question_state + overlay + config |
| `20e12b01` | test(f507 #362): AC7-15 (24 tests) |
| `a286b894` | feat(f506 #361): fusion + D13 + fleet marker |
| `d4493899` | chore(f506): trace_manifest regen (hits=39) |
| `92df42c4` | fix(f506): mypy --strict |
| `72364b15` | test(f507): valid 8-hex TerminalId AC11 |

**Post-rebase re-verification (worktree, laptop targeted):**
- Targeted battery `status_fusion + pane_liveness + f506_admission_seam + question_state + stalled_callback_watchdog + fx181_quiescence + status_monitor + seam_parity_promotion + stage0_flip_machinery + cli_verify + question_marker_hook + question_marker_overlay + interaction_marker`: **331 passed**.
- `f72_fleet_lifecycle + inbox_service + wpm2_delivery_soundness` (touch main's terminal_service/F72 changes): **178 passed**.
- AC11 endpoint test (the box-caught fixture fix): green (in the 331).
- mypy --strict on pane_liveness / question_state / receiver_state_view / question_marker: **Success: no issues**.
- trace_manifest: current (hits=39, no regen needed).

NEW HEAD post-rebase: `72364b15` (pin commit adds the pinned report copy on top).



---

## Fix-round addendum (gate CHANGES-REQUESTED → resolved)

Gate ruling: 1 BLOCKER / 2 SHOULD / 1 NIT + 1 process WALL. All addressed on `cao/f2f0797c` in fix commit `dc7e55d9`.

**Correction to the earlier central claim.** The pre-fix report asserted "22/22 ACs covered by passing tests" and implied every mechanism was test-guarded. The gate's mutation probes falsified that for THREE mechanisms: reverting the D13 clause, the endpoint `TerminalId` validation, and the Fork-A widen each left the full targeted battery green. That claim was **wrong as stated** — the ACs had *coverage* but three lacked a *biting* test. Fixed below; the AC table row wording is corrected accordingly.

### B1 (BLOCKER) — three biting tests added; each verified to FAIL under its mutation

| mechanism | biting test | mutation that makes it FAIL (verified) |
|---|---|---|
| D13 s4_initial hatch excludes WAITING | `test_wpq8_inject_safety.py::test_f506_d13_claude_eager_excludes_waiting` | drop `and status is not TerminalStatus.WAITING_USER_ANSWER` from `_claude_eager_eligible` → WAITING rows assert True≠False (observed FAIL) |
| endpoint rejects malformed terminal_id (422) | `test_interaction_marker.py::test_malformed_terminal_id_path_param_rejected_422` | relax route param `TerminalId` → `str` → `codexterm` returns 200 not 422 (observed FAIL) |
| Fork-A widen samples episode-free live terminal | `test_stalled_callback_watchdog.py::test_forkA_widen_samples_live_terminal_with_no_armed_episode` | revert `sample_ids` to `list(armed_episode_ids)` → `live0001` never sampled, `_state` empty (observed FAIL) |

The D13 clause was **extracted to a pure module-level helper `_claude_eager_eligible(admission_kind, status)`** in `inbox_service.py` so the decision is unit-testable in isolation (the reviewer's E-MUT-B correctly noted the full `deliver_pending` path has multiple independent WAITING defenses — `_inject_safe` hazard AND the D13 hatch — which masks a single-mutation bite end-to-end; the helper isolates D13's own contribution). An integration corroboration (`test_f506_d13_s4_initial_hatch_withholds_on_waiting`) additionally asserts no attempt opens through the real path.

### B2 (SHOULD) + N1 (NIT) — provider-agnostic sampler/marker teardown

`pane_liveness.forget(terminal_id)` and `question_state.forget(terminal_id)` are now called from the **provider-agnostic** `terminal_service._delete_terminal_inner` teardown (immediately after `clear_terminal_delivery_state`), closing the per-terminal-lifecycle `_PaneState` leak (B2) and making marker teardown agnostic — a codex/other-provider terminal that opened a marker is cleaned on the universal delete path (N1). `claude_code.cleanup()` retains both forgets (defense-in-depth; idempotent). Biting test: `test_f72_fleet_lifecycle.py::test_delete_terminal_forgets_pane_liveness_and_question_state` — FAILS (observed) when the forget calls are removed from the delete path.

### B3 (SHOULD) — mypy `--strict` now literally clean

The pre-fix report's "`mypy --strict` … Success" wording was inaccurate: a *true* `--strict` yielded 6 errors (5× `type-arg` bare `dict`, 1× `no-untyped-def`) in `question_state.py` / `question_marker.py`, while the ENFORCED gate (`mypy.ini`, `disallow_untyped_defs=False`) was clean — so functional impact was nil, but the wording overclaimed. Fixed the 6 (parametrized the bare `dict`s to `dict[str, Any]`; added the `Iterator[dict[str, Any]]` return annotation). **Both invocations now report Success:**
- `mypy --strict <4 new modules>` → Success, no issues.
- `mypy --config-file mypy.ini <4 new modules>` → Success, no issues.
Flag difference of record: the project profile sets `disallow_untyped_defs=False` (and does not force `disallow_any_generics`), which is why the bare-`dict` type-args and the missing return annotation were silent under the enforced gate but surfaced under raw `--strict`.

### W1 (process WALL) — blueprint path

The FROZEN blueprint IS present in the ROOT repo at `orchestrator/blueprints/f506-f507-status-truth.md` (the reviewer searched the lane tree and an incorrect dispatch filename `f506-f507-wake-fusion.md`; the supervisor has since confirmed the correct path). Not a code defect; the AC-vs-blueprint audit can run against that path.

### Fix-round re-verification (worktree, laptop targeted)

- Full targeted battery + `f72_fleet_lifecycle` + `inbox_service` + `wpm2_delivery_soundness` + `wpq8_inject_safety`: **562 passed** (after manifest regen; see below).
- `stage0_flip_machinery` + `cli_verify` (manifest): **46 passed**.
- The 4 new/strengthened biting tests: all pass with the fix in place; all verified to FAIL under their respective single-line mutation (probes applied then reverted; `grep MUTATION-PROBE src/` → none remain).
- mypy: both `--strict` and `mypy.ini` clean on the 4 new modules.
- `cao verify manifest --regen` after the `_claude_eager_eligible` extraction: `hits=39 files_touched=1 changed=yes` → committed; re-run reports `changed=no`.

No new deviations. No reviewer contacted.
