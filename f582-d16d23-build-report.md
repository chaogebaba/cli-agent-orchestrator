# F582 D16–D23 build report (slice A)

## Authority and result

- Frozen authority: `/home/chao/VScode_projects/cli-subagents/orchestrator/blueprints/wp-status-truth.md`.
- Required SHA-256 verified before implementation: `2c6eaf8659554fa28b2bdbed3bc858b617819248b07f336a84a463d54cc4597d`.
- Base: `main@b2814464` (D14/D15 merge).
- Branch: `cao/f582-d16d23`.
- Result: slice A covers D17, D19, D20-core, D22-code, D23, AC11, AC12. Deferred to slice B (capture-gated + artifact-format-gated): D16, D18, D20-kiro-parser/assign-wiring/cline, D21-impl/kiro-F589.

## Commits

The final report commit is the branch tip recorded by the completion callback.

## Files touched

Production:

- `src/cli_agent_orchestrator/api/main.py` (D17 release_token + D22 drain/ack endpoints)
- `src/cli_agent_orchestrator/clients/database.py` (D17 ledger migration + D23 expire/supersede)
- `src/cli_agent_orchestrator/hooks/children_ledger.py` (D17 _classify 3-tuple P-B fix)
- `src/cli_agent_orchestrator/hooks/supervisor_drain.py` (D22, NEW)
- `src/cli_agent_orchestrator/hooks/supervisor_ack.py` (D22, NEW)
- `src/cli_agent_orchestrator/models/inbox.py` (D23 MessageStatus + InboxMessage fields)
- `src/cli_agent_orchestrator/providers/base.py` (D20 MCPEvidence + classify_mcp_readiness)
- `src/cli_agent_orchestrator/providers/claude_code.py` (D22 overlay composition)
- `src/cli_agent_orchestrator/providers/codex.py` (D19 _seed_failure_tail)
- `src/cli_agent_orchestrator/services/fleet_service.py` (D17 reader migration + fallback)
- `src/cli_agent_orchestrator/services/inbox_service.py` (D23 expiry pass in reconcile)
- `src/cli_agent_orchestrator/services/pane_liveness.py` (D17 reader migration + fallback)
- `src/cli_agent_orchestrator/services/status_monitor.py` (D17 _non_processing_streak + publish reconcile + _CHILDREN_RECONCILE_K_TICKS=3)

Tests:

- `test/api/test_children_ledger_endpoint.py` (D17 3-arg release contract)
- `test/clients/test_children_ledger.py` (D17 full rewrite + AC4 arms)
- `test/clients/test_inbox_expire_supersede.py` (D23 AC10 arms, NEW)
- `test/hooks/test_children_ledger_hook.py` (D17 3-tuple + P-B arms)
- `test/providers/test_codex_seed_failure_output.py` (D19 AC6 arms, NEW)
- `test/providers/test_mcp_ready_evidence.py` (D20 AC7 core arms, NEW)
- `test/providers/test_supervisor_drain_ack_overlay.py` (D22 AC9 arms, NEW)
- `test/services/test_wp_status_truth_donots_slice_a.py` (AC12 Do-NOT grep arms, NEW)

## Behavioral implementation

- **D17 (F579/#425/AC4):** children ledger migrated from `metadata_json["children"]` to `metadata_json["cao"]["children"]` via `merge_terminal_system_metadata` RMW (worker full-replace-proof). Both readers (`fleet_service._children_count_from_row`, `pane_liveness._children_count_from_metadata`) prefer `cao.children` with free-form fallback. `release_terminal_child` gains `release_token` param — idempotent per stop event via a bounded `cao["children_released"]` ring (≤16). An unmatched `child_id` falls back to pop-oldest (P-B fix: a release can never leave the count unchanged while entries exist). Hook `_classify` now returns a 3-tuple `(op, child_id, release_token)` — `agent_id` travels as `release_token` (observability/dedup), NEVER as the ledger key. API `ChildrenLedgerRequest` + endpoint wire `release_token`. `StatusMonitor` publish path tracks `_non_processing_streak` and calls `reconcile_children_on_publish(K=3)` — a lost `SubagentStop` cannot pin a seat at `delegating` past 3 non-PROCESSING publishes; a seat still PROCESSING is never cleared. Empty ledger written as `[]` (never null-the-column).
- **D19 (F587/AC6):** codex seed timeout + rc≠0 branches carry a redacted ≤40-line tail via `_seed_failure_tail` → `secret_gate.redact_secrets`. The timeout branch (previously dropped `exc.output`) now surfaces a classifier-refusal's diagnostic. `_SEED_FAILURE_TAIL_LINES = 40`.
- **D20 (F537/AC7) core:** `MCPEvidence` frozen dataclass + `classify_mcp_readiness()` function + `BaseProvider.mcp_ready_evidence()` (default None = exempt). Undeclared provider → `mcp_unverified` (never ERROR, Do-NOT 23); declared+connected → `mcp_ready`; declared+absent → `E-MCP-UNAVAILABLE`. Kiro evidence parser and assign-result wiring deferred (see Deviations).
- **D22 (F543/AC9) code:** `hooks.supervisor_drain` and `hooks.supervisor_ack` created as containment-guarded, fail-open thin transports (Do-NOT 20 compliant: `python -m <module>`, no `~/.claude` path). Composed into `_write_terminal_settings` (drain on SessionStart, ack on Stop). `POST /terminals/{id}/inbox/drain` → `deliver_pending` + `POST /terminals/{id}/inbox/drain-ack` endpoints added to api/main.py. SHOULD-5 debt: F476 owns the delivered-state cursor internals.
- **D23 (F578/AC10) DB/service layer:** `MessageStatus` +`EXPIRED`/`SUPERSEDED`. `InboxModel` +`expire_after_s`/`supersede_key` (nullable deferred columns). Supersede-at-enqueue: earlier PENDING/HELD rows with the same `(receiver, supersede_key)` transition to `superseded` immediately. `list_expired_pending_rows(now)` + `expire_pending_rows(ids)` DB functions. Expiry sweep wired UNCONDITIONALLY ahead of the grace floor in `reconcile_orphaned_messages`. All 5 pending queries already filter `status == PENDING` — expired/superseded rows leave every surface at once. Rows without the fields behave byte-identically.
- **AC11 (S4):** no code. #395 re-milestoned to `wp-warm-lane-lifecycle` (D12c covers the delegating half; the "follow-up returns a done worker to idle" semantic is task-lifecycle, not a status lie). #441 re-milestoned with rationale: transient, self-recovered, likely F522 ABBA (live in the current build); re-observe, re-milestone to fleet-tui WP if it recurs. #386 stays in S2 until AC8's live spawn passes (D21 deferred).
- **D21 attribution (Do-NOT 26, pre-fix step):** replay of the 2026-08-30 04:10Z recurrence on dc8f42a8 (decisions log `~/.aws/cli-agent-orchestrator/logs/auto-answers/dc8f42a8.decisions.log`) CONFIRMS the supervisor's correction: the `codex-resume-workdir-card` rule MATCHED and FIRED 26× (+ 1 settled). The matcher is correct. The failing stage is an ARMING/TIMING gap (the responder evaluated only during the ~12s `f491_dialog_clear` window, the chooser rendered after). This diverges from the blueprint's "matcher defect" framing (see Deviations). Full attribution at `/data/cao-scratch/9064394e/f582-d21-attribution.md`.

## Targeted test evidence

All work ran on `grok-box-004`; none ran on the laptop.

Authorized pytest scope (slice A tests + affected suites):
```text
test/clients/test_children_ledger.py test/hooks/test_children_ledger_hook.py
test/clients/test_inbox_expire_supersede.py test/providers/test_codex_seed_failure_output.py
test/providers/test_mcp_ready_evidence.py test/providers/test_supervisor_drain_ack_overlay.py
test/services/test_wp_status_truth_donots_slice_a.py
test/services/test_fleet_delegating.py test/api/test_children_ledger_endpoint.py
test/providers/test_children_ledger_overlay.py
```
- Head: **92 passed, 0 failed** in 3.41s.
- Black `--check --line-length 100`: pass (19 files unchanged).
- isort `--check-only --line-length 100`: pass.

## Mypy strict counts

- Head (existing 11 source files): **300 errors in 7 files**.
- Base `b2814464` (same 11 files): **300 errors in 7 files**.
- Delta: **0 errors, 0 files**.
- Head (2 new hook modules): **0 errors in 2 files** (Success).

## Mutation ledger

All mutations applied at the verified tip on box-004. Each mutant was applied via sed, named test run (must fail), restored, clean diff asserted.

| Mutant | Applied edit | Named selector | Kill proof |
|---|---|---|---|
| D17-pb-agent_id-as-key | `_classify` release emits `agent_id` as `child_id` (the old P-B defect) | `test_classify_release_subagent_stop_agent_id_is_token_not_key` | test_rc=1, post_revert_rc=0 |
| D17-reconcile-K | Drop the K-non-PROCESSING drop branch (`if False:`) | `test_reconcile_drops_entries_after_k_non_processing` | test_rc=1, post_revert_rc=0 |
| D19-redaction | Skip `redact_secrets` (return raw tail) | `test_timeout_branch_redacts_secret_in_tail` | test_rc=1, post_revert_rc=0 |
| D23-supersede | Neuter supersede transition (`if False:`) | `test_supersede_at_enqueue_transitions_earlier_peer` | test_rc=1, post_revert_rc=0 |
| D23-expiry | Make deadline never expire (`if False:`) | `test_expired_row_leaves_every_pending_query` | test_rc=1, post_revert_rc=0 |
| D22-drain-hook | Drop drain hook from SessionStart composition | `test_drain_hook_composed_on_sessionstart` | test_rc=1, post_revert_rc=0 |
| D20-undeclared-errors | Classifier returns `MCP_UNAVAILABLE` for undeclared (Do-NOT 23 violation) | `test_undeclared_provider_is_exempt_unverified` | test_rc=1, post_revert_rc=0 |
| DN25-recovery_state | Inject `recovery_state` comment into the ledger region | `test_children_ledger_writers_never_touch_recovery_state` | test_rc=1, post_revert_rc=0 |

## Deviations and deferred rows

- **D21 (F589+F530/AC8):** implementation deferred. Attribution step complete (arming/timing gap, not matcher defect — see above). **Blueprint divergence:** the frozen D21 row calls #386 "the #386 matcher defect" and names `dialog_region` tail-composition as the leading hypothesis. The 2026-08-30 dc8f42a8.decisions.log contradicts that: the `codex-resume-workdir-card` rule matched and fired 26×. Per the supervisor's explicit correction, D21's fix will target the polling lifetime (keep evaluating until first IDLE/PROCESSING or re-arm on the f524 stall surfacing), NOT the matcher. Byte-exact codex F530 fixture exists in repo (`test/fixtures/codex_dialogs/trust.ansi.txt`); kiro F589 "connection interrupted" screen NOT present (deferred capture). D21-impl will be built in slice B when the kiro F589 screen arrives.
- **D16 (F581/AC3):** deferred — codex and kiro spinner byte-exact pane captures not yet staged. `rule3a_busy_marker` base method exists at `providers/base.py:296`; `claude_code` already overrides it. Codex + kiro overrides land in slice B when the captures arrive.
- **D18 (F575/AC5):** deferred — needs a live known-authenticated persona `.claude.json` to confirm the `oauthAccount` key before the fail-closed branch ships (and an F567-matrix persona capture). Neither present.
- **D20-kiro evidence parser + cline leg + assign-result wiring:** deferred — the exact kiro MCP evidence artifact format is cited to HANDOFF.md:237, which is not present in this tree; implementing the parser here would be inventing the artifact shape. The cline MCP connect-line capture not yet staged. The provider-agnostic classifier core (`MCPEvidence`, `classify_mcp_readiness`, `BaseProvider.mcp_ready_evidence` default) is landed and tested.
- **D22 SHOULD-5 debt:** F476 (D1/D3/D5/D8 — one wake cursor, server-side claim/commit choke point) has partially landed (server endpoints present, hook bodies not). D22 ships the hook location change only; the drain/ack modules are thin transports posting to best-effort endpoints (`deliver_pending` / logged no-op). When F476's richer wake-cursor drain lands, these endpoints are superseded. Debt recorded on #331.
- **D23 MCP/API send_message param plumbing:** the `expire_after_s`/`supersede_key` params are threaded through `create_inbox_message` → `_create_inbox_message_unfenced` → `_insert_routed_inbox_row` and the DB row-state seam is complete and tested. The thin MCP tool param + API route surface (so a send_message caller can pass these values end-to-end) is a follow-up over the existing surface if the gate requires.

## Box-actions ledger

All invocations used `CAO_BOXES=box@grok-box-004 bash scripts/box-run.sh <label> -- '<command>'` from `/home/chao/VScode_projects/cli-subagents`. No raw SSH.

| Label | Box command/action | Result |
|---|---|---|
| `f582-d16-verify` | fetch; checkout; uv sync; black/isort --check; pytest scope; mypy strict head+base | black/isort clean; 92 passed; mypy 300==300 parity |
| `f582-d16-mutants` | same checkout; full pytest scope; 8 designed mutants (sed→test→restore→clean) | 92 passed; 8/8 mutants killed, all post_revert_rc=0 |
| `f582-fix-r2` (D14 slice, earlier) | pre-existing; box parked untracked test_auto_responder_seed_rules.py, cleaned | no residual |

Box final state: checkout at verified tip; only disposable per-worktree `.venv` from `uv sync --frozen`; no apt/pip/global installs.
