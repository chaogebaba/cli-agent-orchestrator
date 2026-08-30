# F582 D16–D23 build report (slice A)

## r2 status (post EMPIRICAL-GATE-NO r1 — B1–B4 + N1 all addressed)

- **Gate r1 verdict:** EMPIRICAL-GATE-NO, B=4 S=0 N=1
  (`/data/cao-scratch/9064394e/f582-d16-empirical-gate-r1.md`).
- **r2 branch tip:** `cao/f582-d16d23` @ **`320609c987e326de40a93c05ab03dae986cf3729`**
  (r2 commits: `c7450c50` = B1+B2 fixes, `320609c9` = mypy-parity follow-up).
- **Base for parity:** `4bf27b9261ae81989b47c4fa18a957f271adfaad` (the r1 gate tip).
- **Every r1 finding fixed** — per-finding detail in "## r2 — gate-fix work" below.
  B1 PII (redaction + extended guard), B2 D23 send_message end-to-end wiring,
  B3 AC4 live evidence, B4 AC9 live evidence, N1 mypy reproducibility.
- **r2 verification (box grok-box-004, SHA `c7450c50` full + `320609c9` re-verify):**
  full scope **263 passed**; **all 10 mutants killed** (8 original + `D16-kiro-never-busy`
  + the new `B2-drop-d23-passthrough`); black/isort L100 clean; **mypy --strict parity
  head=176 == base=176, delta 0** on the three r2-touched source files.
- **Live-leg deviation (approved):** AC4/AC9 ran on the LAPTOP in a fully isolated
  `cao sandbox` instance (not an offload box) — the box fleet ships no `claude` CLI
  and no `bwrap`, so claude_code subagent/overlay legs cannot run there. Supervisor
  ruling (A). Detail in "## r2 — gate-fix work" and the Deviations section.

---

# F582 D16–D23 build report (slice A) — r1 body (unchanged below)

## Authority and result

- Frozen authority: `/home/chao/VScode_projects/cli-subagents/orchestrator/blueprints/wp-status-truth.md`.
- Required SHA-256 verified before implementation: `2c6eaf8659554fa28b2bdbed3bc858b617819248b07f336a84a463d54cc4597d`.
- Base: `main@b2814464` (D14/D15 merge).
- Branch: `cao/f582-d16d23`.
- Result: slice A covers D17, D19, D20-core, D22-code, D23, AC11, AC12, **D16-kiro leg** (folded in after captures landed). Deferred to slice B (capture-gated + artifact-format-gated): D16-codex, D18, D20-kiro-parser/assign-wiring/cline, D21-impl/kiro-F589.

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
- **D16 (F581/AC3):** kiro leg DELIVERED (folded into slice A after live captures landed). `kiro_busy_marker_live` helper + `KiroCliProvider.rule3a_busy_marker` override; True on the byte-exact live busy panes (`Kiro is working` / `Thinking...` / `◐ N tasks remaining` spinner), False on idle prompt without marker, None on unidentifiable pane; grok/cline keep the BaseProvider default None. Fixtures verbatim under `test/providers/fixtures/busy_marker/kiro_cli/` (spinner-1/-2 .txt+.json, LIVE captures). Test `test/providers/test_kiro_busy_marker.py`. Mutant `D16-kiro-never-busy` (helper returns None instead of True) killed. **codex leg still deferred** — codex spinner captures were in progress on a live codex seat at slice-A close.
- **PII redaction (fold):** the post-merge full suite's `test/test_fixtures_no_personal_pii.py` flagged an authenticated-account gmail address in `status_truth/cline_cli/abort-2.txt` (ours, D14) and `idle-1.txt` (pre-existing). Both were redacted to the same-width placeholder `usr@example.invalid` (19 chars — pane geometry unchanged; the D14 load-bearing bytes, line count 129 and `ABORT_LINE`, are untouched). A `pii_redaction` note was added to each `.json` (source otherwise verbatim; the note does NOT quote the raw address, which would re-introduce the PII). New hashes: `abort-2.txt` `67c4b39533ffa0065ca182def0680d8ccb23d9c2e78c61db7fe438f290e953da`; `abort-2.json` `5c7b6a0865b4f0115c214816ba6a3e6aeb8b14dcf64ca1466dac8589e34f57bf`; `idle-1.txt` `984c8b142d535b9e9ee34fe7e316b21b009d51e7ccf58ce7643bf6c47a68f52c`; `idle-1.json` `8a0abfe8154583982abbcd795865c0a1bef6fd99fc1f97e8c1b1f80303488369`. Verified on box-004: D14 abort suite still green, PII test passes.
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


---

## r2 — gate-fix work (all r1 findings B1–B4 + N1)

r2 commits on `cao/f582-d16d23`:
- **`c7450c50`** — B1 (PII redaction + extended guard) + B2 (D23 send_message wiring).
- **`320609c9`** — mypy --strict parity follow-up (`send_kwargs: dict[str, Any]`).

### B1 — Kiro fixtures leaked `/home/<user>` paths + email/token prose (ACCEPTED)

Fix (commit `c7450c50`):
- Redacted every `/home/chao` → `/home/user` (same-width, 4→4 chars, pane geometry
  and line counts unchanged) across **49 fixture files** carrying the builder home
  path — not just the two kiro captures. `/home/user` is the synthetic placeholder
  already used in the codex statusline corpus.
- Neutralized the transcript **prose references** to a real email/token/secret in
  `test/providers/fixtures/busy_marker/kiro_cli/spinner-1.txt` / `spinner-2.txt`
  (no literal email or token was ever present — the prose merely *mentioned* one in
  seat scrollback): `email/secret material.`→`sensitive-log material.`,
  `embedded email/tokens`→`embedded sensitive data`, `incl. a real email/token`→
  `(redacted-out here)`. Busy-marker rows (`Kiro is working`, `◐ N tasks remaining`)
  and the footer/status geometry rows were NOT touched; line count stays 125.
- Recorded a `pii_redaction` block in `spinner-1.json` / `spinner-2.json`.
- **Extended `test/test_fixtures_no_personal_pii.py`** across ALL fixtures with two
  new guards: `test_no_real_home_paths_in_fixtures` (rejects `/home/<name>` /
  `/Users/<name>`; allowlists synthetic names user/test/example/runner/ubuntu/…;
  excludes `__pycache__`/`*.pyc` build artifacts) and `test_no_secret_tokens_in_fixtures`
  (sk-/rk_/gh[pousr]_/github_pat_/xox[baprs]-/AKIA/AIza/ya29/JWT/Bearer shapes).
  The personal-email guard is retained. All 3 guards pass.
- **Collateral the redaction + extended guard caught (fixed):**
  - `test/fixtures/codex_dialogs/SHA256SUMS` regenerated (11 of 24 `.ansi.txt`
    hashes changed by the path redaction).
  - `test/services/test_wpd1_decontam.py` — the two pinned decontam-artifact hashes
    updated: POSITIVE `03d8b6c89974e20f40d7c74072f319a49d95d74cf5783df3ac9a8fa24e9ae217`,
    CONTROL `70132dc917c1a22d298049f883b619f8fcaab626528265a7d43a27c847d79611`.
- **Pre-existing, unrelated:** `test/services/test_f597_auto_answers_corpus.py::
  test_every_enabled_rule_has_a_sample` fails on clean base `4bf27b92` too (missing
  `codex-resume-workdir-card.txt` sample) — NOT introduced by this lane; left as-is.

### B2 — D23 not exposed through send_message (AC10 not met) (ACCEPTED)

Fix (commit `c7450c50`): plumbed `expire_after_s` / `supersede_key` end to end.
Opt-in throughout — the two fields are only forwarded when set, so an unset send is
byte-identical to today (Do-NOT 21).
1. **MCP tool** `mcp_server.server.send_message` — two `Field` params →
   `_send_message_impl` (two new params) → `send_kwargs`.
2. `_send_to_inbox` — adds the two keys to the POST query params only when set.
3. **API** `api.main.create_inbox_message_endpoint` — two new query params, threaded
   into `raw_kwargs` (direct-terminal branch) AND `logical_kwargs` (`mb_` branch).
4. **Logical path** `mailbox_service.create_logical_inbox_message` +
   `_create_logical_inbox_message_inner` → `_insert_routed_inbox_row`.
   (The direct-terminal DB path `create_inbox_message` → `_create_inbox_message_unfenced`
   → `_insert_routed_inbox_row` already threaded them; the DB row-state seam was
   already complete/tested at r1.)
Tests: `test/mcp_server/test_send_message_d23_wiring.py` (7 — MCP→_send_to_inbox
kwargs, query-param forwarding, unset omission, **+ a drop-pass-through mutant guard**),
`test/api/test_inbox_expire_supersede_endpoint.py` (3 — both endpoint branches;
receiver ids must match `^[a-f0-9]{8}|mb_[a-f0-9]{8}$`). AC10's
`send_message(..., expire_after_s=5)` now works through the MCP tool.

### B3 — D17 AC4 ★ live-server acceptance (ACCEPTED — LAPTOP, isolated instance)

Full evidence + method: `/data/cao-scratch/9064394e/f582-ac4-live/AC4-EVIDENCE.md`.
- Isolated `cao sandbox up` instance `e5c365dd` built from THIS worktree's venv,
  `CAO_HOME_DIR=/data/cao-scratch/9064394e/f582-live-home`, endpoint `:9899`,
  tmux socket `cao-sbx-e5c365dd`. Production `:9889` verified HEALTHY + UNTOUCHED
  throughout. claude_code = shared-auth-read-only; model pinned **haiku**.
- A claude_code (haiku) `code_supervisor` (terminal `e990f4ad`) spawned TWO
  SEQUENTIAL subagents via its native Task tool (ACK-ONE, then ACK-TWO → BOTH-DONE).
- **Observed** (`fleet-samples.jsonl`): `delegating=true, children_count=1` DURING
  each subagent (seat status COMPLETED with a child in flight — D12c projection);
  `delegating=false, children_count=0` within one poll tick (~2 s) after the second
  subagent returned; final settle 0 children. Ledger lives under DB `metadata`
  `cao.children`; the `cao.children_released` idempotency ring advanced 0→1→2 across
  the two register/release pairs and is never counted as a child. Server log:
  **7× `POST /terminals/e990f4ad/children-ledger 200 OK`**.
- Captures (email-scrubbed): `AC4-EVIDENCE.md`, `fleet-samples.jsonl`,
  `supervisor-pane.txt`, `server-log-excerpt.txt`.
- Note: an INFORMATIONAL weekly-usage banner appeared; it is NOT a login/usage
  prompt requiring an answer and the run completed — no auth prompt was answered.

### B4 — D22 AC9 ★ foreign-repo acceptance (ACCEPTED — LAPTOP, isolated instance)

Full evidence + method: `/data/cao-scratch/9064394e/f582-ac9-live/AC9-EVIDENCE.md`.
- Foreign scratch repo `/data/cao-scratch/9064394e/f582-foreign-repo` (own `git init`,
  HEAD `a0d8d31f`, **no `.claude/` dir** → the overlay-composed hook is the ONLY
  drain hook that can fire).
- A claude_code (haiku) supervisor (terminal `45da8ee2`) launched with
  `--working-directory` = the foreign repo; reached READY.
- **(b) overlay drain hook RAN (★):** the seat's composed overlay `settings.json`
  wires SessionStart → `env CAO_API_BASE_URL=http://127.0.0.1:9899 <venv python> -m
  cli_agent_orchestrator.hooks.supervisor_drain` (`overlay-drain-hook.txt`), and the
  server log shows **`POST /terminals/45da8ee2/inbox/drain 200 OK`** on SessionStart
  (+ paired `inbox/drain-ack`). In a repo with no `.claude/hooks`, that POST is
  unambiguously attributable to `hooks.supervisor_drain`.
- **(a) exactly-once callback (★):** a callback (sentinel `AC9-CB-SENTINEL-7f3a91`)
  delivered via `cao session send` was delivered EXACTLY ONCE (sentinel prompt appears
  1× in the pane; one `POST /input`; one `GOT-CALLBACK` reply). A hand-rolled raw
  inbox POST with a bogus sender was correctly refused `403 E-SENDER-UNKNOWN`.
- **consumed_through_id (honest scope):** that cursor is a *mailbox* field advanced by
  F476's wake-cursor machinery (D22 SHOULD-5 debt #331). This idle direct-terminal
  seat is not a mailbox-pull supervisor, so the D22 observable is the drain-endpoint
  POST above, not a mailbox cursor advance — stated in `AC9-EVIDENCE.md`.
- Captures (email-scrubbed): `AC9-EVIDENCE.md`, `overlay-drain-hook.txt`,
  `server-log-excerpt.txt`, `supervisor-pane.txt`.
- Sandbox torn down: `cao sandbox down --purge` → `{"status":"down","purged":true}`;
  root + tmux socket gone; production `:9889` still 200.

### N1 — mypy count not reproducible from the report (ACCEPTED)

The r1 report stated "300 errors" without a specific command. r2 fix:
- **Exact command specified:** `uv run mypy --strict
  src/cli_agent_orchestrator/mcp_server/server.py src/cli_agent_orchestrator/api/main.py
  src/cli_agent_orchestrator/services/mailbox_service.py` (the three r2-touched source
  files).
- Running B2's wiring first surfaced **+3 new strict errors** (`send_kwargs` was
  inferred `dict[str, bool]` from its bool-only literal, then carried int/str D23
  values → 2 assignment + 1 `**`-call arg-type). Fixed in commit `320609c9` with
  `send_kwargs: dict[str, Any]`.
- **Reproduced on box grok-box-004:** HEAD `320609c9` = **176 errors**, base
  `4bf27b92` = **176 errors**, **delta 0** on the three files. No new type errors.

### r2 mutation ledger (10 mutants: 8 original + kiro-never-busy + B2)

Run on grok-box-004 at `c7450c50` (full scope 263 passed); every mutant sed→named
test (must fail)→restore→clean-diff.

| Mutant | Named selector | Kill |
|---|---|---|
| D17-pb-agent_id-as-key | `test_classify_release_subagent_stop_agent_id_is_token_not_key` | test_rc=1, revert=0 |
| D17-reconcile-drop-K | `test_reconcile_drops_entries_after_k_non_processing` | test_rc=1, revert=0 |
| D19-skip-redaction | `test_timeout_branch_redacts_secret_in_tail` | test_rc=1, revert=0 |
| D23-skip-supersede | `test_supersede_at_enqueue_transitions_earlier_peer` | test_rc=1, revert=0 |
| D23-expiry-never | `test_expired_row_leaves_every_pending_query` | test_rc=1, revert=0 |
| D22-drop-drain-hook | `test_drain_hook_composed_on_sessionstart` | test_rc=1, revert=0 |
| D20-undeclared-errors | `test_undeclared_provider_is_exempt_unverified` | test_rc=1, revert=0 |
| DN25-recovery_state-write | `test_children_ledger_writers_never_touch_recovery_state` | test_rc=1, revert=0 |
| **D16-kiro-never-busy** | `test_helper_true_on_live_kiro_busy_fixtures` | test_rc=1, revert=0 |
| **B2-drop-d23-passthrough** | `test_send_message_d23_wiring.py::test_mutant_dropping_pass_through_is_detectable` | test_rc=1, revert=0 |

### r2 box-actions ledger (grok-box-004)

All via `CAO_BOXES=box@grok-box-004 bash scripts/box-run.sh <label> -- '<cmd>'` from
`/home/chao/VScode_projects/cli-subagents`; script bodies delivered base64-inline
(read-only decode on the box); no code-under-test scp'd (checkout of pushed SHA only).

| Label | Action | Result |
|---|---|---|
| `f582-r2-verify` | fetch; checkout `c7450c50`; uv sync --frozen; full pytest scope; 10 mutants; black/isort; mypy head+base parity | 263 passed; 10/10 killed; fmt clean; **mypy 179 vs 176 → +3 (regression found)** |
| `f582-boxclean` / `f582-boxclean2` | diagnose + recover box repo after the mypy-parity stash collision (see below); `git reset --merge`, `git checkout -f 320609c9` | box repo restored clean at `320609c9` |
| `f582-r2b` | checkout `320609c9`; D23/send_message pytest; black/isort; **stash-free** mypy parity | 36 passed; fmt clean; **mypy 176==176 delta 0**; final box status clean |

**Box incident (self-inflicted, recovered, reported per box-ops):** the first
`f582-r2-verify` mypy base-comparison used `git stash` + `git checkout BASE -- <files>`
+ `git stash pop`. On both this worktree AND the box repo that collided with
PRE-EXISTING foreign stashes (from other lanes: an F401 "no branch" WIP, F348, F351),
leaving an unmerged index + foreign modified files (a `mock_cli.py` F401 reversion,
`sandbox_bootstrap.py`, several test files). Recovered both: worktree via
`git checkout HEAD -- <unmerged>` + `git restore --staged --worktree <foreign>` (kept
only my one-line `server.py` fix); box via `git reset --merge` + `git checkout -f
320609c9`. The 3 pre-existing box stashes were left untouched (not mine). The parity
re-check was rewritten stash-free (`git show BASE:file` into a temp copy, swap+restore
from git). **Lesson:** never `git stash` on a shared checkout for an A/B; use
`git show <sha>:<file>`.

Box final state: clean checkout at `320609c9`; only the disposable per-worktree
`.venv` from `uv sync --frozen`; no apt/pip/global installs; no temp files outside
`~/box-scratch`.

### r2 live-instance ledger (laptop, isolated — AC4/AC9)

- `cao sandbox up --root /data/cao-scratch/9064394e/f582-live-home --port 9899`
  (built from the worktree venv) → instance `e5c365dd`, tmux `-L cao-sbx-e5c365dd`.
- Seeded sandbox `providers.toml` (`[claude_code] model = "haiku"`) + folder trust
  in the sandbox `.claude.json` (scratch + foreign repo).
- Sessions launched + torn down: `cao-f582probe`/`probe2` (auth/trust smoke),
  `cao-f582ac4` (AC4), `cao-f582ac9` (AC9).
- `cao sandbox down --purge` → root + tmux socket removed. Production `:9889` verified
  200 before and after. All captured evidence email-scrubbed; both evidence dirs
  verified free of personal email.

### r2 deviations

- **Live legs on the laptop, not a box** (supervisor ruling A): the grok-box fleet has
  no `claude` CLI and no `bwrap`, so the claude_code-specific AC4 (`SubagentStop`
  children ledger) and AC9 (overlay drain) legs cannot run there. Run instead in a
  fully isolated, production-inert `cao sandbox` instance on the laptop with real
  claude (haiku). Production server/config never touched.
- The B2 API `send_message` surface that r1's Deviations listed as "a follow-up if the
  gate requires" is now **built** (this is exactly what B2 required).
