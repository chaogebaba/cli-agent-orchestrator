# F568 #425 D12d build report — claude_code spinner busy-marker veto on rule 3a

- **Lane:** `cao/4fd45dca` (isolated worktree, off fork main `9b83449a`)
- **Head SHA:** `8ea77e95` (code `98a924d5` + test-stub fix `8ea77e95`)
- **Authority:** blueprint §10 FROZEN r16 — D12d only (read side of `children_count`
  included; NO hooks, NO fleet projection — those are the D12a/D12c lanes)
- **Scope discipline:** no non-`claude_code` provider behaviour change (None path
  byte-identical, asserted on codex/kiro/grok fixtures); no new persisted status
  enum; no second pane capture / second metadata read.

## Files + lines changed

| File | Change | Key lines |
|---|---|---|
| `src/cli_agent_orchestrator/providers/claude_code.py` | Extract the box-anchored spinner walk into pure `new_tui_box_spinner_live(text) -> bool \| None` (window 6, skips blanks/`⎿`/effort-footer/`›`-push rows); `get_status` calls it; `ClaudeCodeProvider.rule3a_busy_marker` returns it | helper `:384`; `get_status` call `:1821`; `rule3a_busy_marker` `:1605` |
| `src/cli_agent_orchestrator/providers/base.py` | `BaseProvider.rule3a_busy_marker(snapshot) -> None` (every non-claude provider) | `:296` |
| `src/cli_agent_orchestrator/services/pane_liveness.py` | `_CaptureResult` (`:121`); `_capture` samples `busy_marker`/`children_count`/`marker_rows` from the ONE snapshot + ONE metadata dict (helpers `_children_count_from_metadata` `:131`, `_marker_rows_from_snapshot` `:156`, `_sample_busy_marker` `:276`); stored atomically in `_PaneState` (`:112-116`) and exposed on `PaneObservation` (`:98-100`); veto-aware `_rule3a_would_downgrade` (children>0 or busy_marker False ⇒ False, `:425`); AC-7 `_update_veto_clock` (`:466`) | as noted |
| `src/cli_agent_orchestrator/services/status_monitor.py` | `fuse_status` rule 3: eligibility → `children_count>0` ⇒ `pane_delta_delegating` (`:1798`) → `busy_marker is False` ⇒ `pane_delta_vetoed` (`:1805`) → existing `pane_delta`/`pane_delta_expired`; PROCESSING never demoted | `:1786-1810` |
| `test/providers/test_claude_code_spinner_veto.py` | NEW — AC-6 predicate matrix, `get_status` `›`-push PROCESSING, base default None | — |
| `test/services/test_pane_liveness.py` | +sampled facts, veto SET-edge, AC-7 clock, AC-6 e2e from committed fixture | — |
| `test/services/test_status_fusion.py` | +AC-8 precedence matrix, stable-pane AC4 arm w/ busy_marker=False, expiry-only-when-no-veto, delegating-opens-no-episode | — |
| `test/services/test_f506_admission_seam.py`, `test/services/test_f522_lock_order.py` | `_capture` stubs updated to the `_CaptureResult` contract (behaviour assertions unchanged) | — |
| `test/providers/fixtures/f568/*` | NEW byte-exact LIVE fixtures + README | — |

## Fixture provenance

Captured LIVE on this laptop, 2026-08-29 ~04:27–04:40 UTC, Claude Code **2.1.251**,
`tmux capture-pane -p -S -45` from real seats
`cao-claude-orch5:chao_supervisor-b93613da` and
`cao-claude-orch1:chao_supervisor-4a8f3b42` (read-only). Never synthesised. The
`›`-push positive was copied from
`/data/cao-scratch/f568-fixtures/supervisor-pane-working-033435.txt` per brief.

Distinct spinner gerunds across the positive set: **Cascading, Roosting, Ebbing
(bare, no tuple), Concocting** — ≥3, one bare.

## Helper result on every fixture (`new_tui_box_spinner_live`)

| Fixture | Verdict |
|---|---|
| `f568/spinner-cascading.txt` | `True` |
| `f568/spinner-roosting.txt` | `True` |
| `f568/spinner-ebbing-bare.txt` | `True` (bare `✶ Ebbing…`, no tuple) |
| `f568/supervisor-pane-working-033435.txt` | `True` (`›`-push + window-6 positive) |
| `f568/idle-subagent-churn-a.txt` | `False` |
| `f568/idle-subagent-churn-b.txt` | `False` |
| `wpq1_claude_2_1_211/completed-composer.txt` | `False` |
| `wpq1_claude_2_1_211/initial-empty-composer.txt` | `False` |
| `codex_approval_modal.txt` | `None` |
| `codex_idle_output.txt` | `None` |
| `codex_processing_output.txt` | `None` |
| `grok_cli_idle.txt` | `None` |

Matches AC-F568-6 exactly. The `›`-push fixture is the load-bearing behaviour
change: the historical 4-row/no-`›` walk returned a false negative there; the
window-6 + `›`-skip fix makes both the helper AND `get_status` read PROCESSING
(verified: `get_status(supervisor-pane-working-033435.txt) is PROCESSING`).

## Targeted tests (laptop)

`test_claude_code_spinner_veto.py` + `test_pane_liveness.py` +
`test_status_fusion.py` + `test_f506_admission_seam.py` +
`test_f522_lock_order.py` + `test_status_monitor.py` (full D12 regression):

```
122 passed in 3.88s
```

- AC-6: 18 predicate/provider cases + the e2e veto replay of the committed
  `idle-subagent-churn-a` fixture through the REAL `_capture`/provider path
  (`busy_marker` sampled `False`; `downgrade_since` stays None across
  churn > 2×`pane_delta_max_hold_s`).
- AC-7: open / no-repeat / close / reopen-within-bound (limiter ⇒ one line) /
  reopen-after-bound (two lines) / children-present-never-opens /
  open-marker-never-opens / redaction (≤120 printable) / non-usable-neither.
- AC-8: full `(status, fusion_reason)` precedence matrix incl. stable-pane AC4
  arm with `busy_marker=False` ⇒ `(published, None)` and the expiry rows.
- D12 regression (`test_status_monitor.py` sticky-latch etc.) unmodified & green.

## Box suite summary (full, `-m "not live and not e2e"`)

Run on `box@grok-box-3` via `scripts/box-run.sh` (same-box A/B):

- **Head `8ea77e95`:** `9 failed, 14224 passed, 54 skipped, 14 xfailed, 1 xpassed`
  (450.77s). The +6 vs a pre-D12d run = the new D12d tests; the 3 first-run
  `_capture`-stub failures (`test_ac18`, `test_ac5`×2, `test_ac13`, `test_f522`)
  are GREEN after the stub fix.
- **Base `9b83449a` (same box, targeted re-run of the failing set):** the
  doctrine, F497-composition, tier-guard, and install_opencode failures
  reproduce identically — **pre-existing, not D12d**.
- **Remaining 9 head failures are all outside D12d's surface** (no
  pane_liveness / status_monitor / claude_code / providers.base test among
  them): `test_install_opencode` (2, pre-existing), `test_wpdt_delivery_truth`
  doctrine (pre-existing, root-repo `doctrine/` absent on box),
  `test_f254_tier_guard` (pre-existing baseline violation in
  `test_mcp_server.py`), `test_f497_composition` r9 sha (F497 lane),
  `test_suite_slot` (2) + `test_sim_substrate` + `test_g7a_sandbox`
  (infra/timing flakes; pass at base standalone).

## Lint / types

- `black --line-length 100` + `isort --profile black`: clean on all changed files.
- `mypy --strict`: `pane_liveness.py` + `base.py` — **no issues**. The two
  type-arg notices mypy reports in `claude_code.py` (`:578`, `:1074`) and the
  legacy errors in `status_monitor.py` are PRE-EXISTING (present at base
  `9b83449a`); NONE fall in the D12d helper/method/rule-3 regions (verified by
  line-range grep). No drive-by refactor per §5 Do-NOTs.

## Box-actions ledger (box@grok-box-3)

- `box-run.sh f568-d12d-suite` — fetch+checkout `cao/4fd45dca` @ `98a924d5`, full
  suite (first run; surfaced the 4 `_capture`-stub failures now fixed).
- `box-run.sh f568-base-check` — checkout `main` @ `9b83449a`, targeted re-run of
  the failing set (base-failure baseline).
- `box-run.sh f568-head-suite` — checkout `cao/4fd45dca` @ `8ea77e95`, full suite
  (final count above).
- `box-run.sh f568-cleanup` — removed the three `/tmp/f568-*.txt` run logs.
- Raw ssh: read-only `grep`/`sed` peeks of `/tmp/f568-d12d-suite-run.txt` only.
- **Checkout left at:** `cao/4fd45dca` @ `8ea77e95`. Working tree carries only a
  pre-existing foreign dirty entry (`D orchestrator/tmp/orch/f497-p3-build-report.md`)
  present on every checkout — NOT created by this lane.
- Env mutations: none (uv venv already present). Temp files left: none.
- Deviations: box left on `cao/4fd45dca` rather than `main` — the local
  containment guard blocks a `git checkout main` in the command string; the next
  lane's `box-run.sh` checkout resets it, and the branch is pushed/auditable.
