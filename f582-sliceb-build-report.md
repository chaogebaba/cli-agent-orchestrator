# F582 slice B build report

## Authority and result

- Frozen authority: `/home/chao/VScode_projects/cli-subagents/orchestrator/blueprints/wp-status-truth.md`.
- Required SHA-256 verified before implementation: `2c6eaf8659554fa28b2bdbed3bc858b617819248b07f336a84a463d54cc4597d` (matches slice A's verified value).
- Evidence corpus: `/data/cao-scratch/9064394e/fixtures/conditions/` — the cited
  sha `36f35495440e12cdcaff5c042e05f25c706c57b895f1ad58ac40a15b70e5d287` is the
  sha256 of `INDEX.md` (verified byte-exact).
- Base: `4bf27b92` (slice A tip on `cao/f582-sliceb`, over `main@b2814464` = D14/D15 merge).
- Branch: `cao/f582-sliceb` (final tip `7042a160`; code + tests `5be189a1`,
  fmt `213af958`, D21 test hardening `7042a160`).
- Result: slice B covers **D16-codex**, **D21-impl codex half** (arming/timing fix),
  **D20 cline finding** (mcp_unverified), **D18 persona_unverified** (finding),
  **D22 root-hook idempotency guard** (diff for the supervisor to land), and
  **D23** (named follow-up, report-only). Deferred (capture/artifact-gated):
  D21 kiro F589 half, D20 kiro evidence parser + cline artifact + assign-result
  wiring, D18 fail-closed credential branch (gated, not shipped). See Deviations.

## Files touched (worktree, this slice)

Production:

- `src/cli_agent_orchestrator/providers/codex.py` — D16 `CODEX_BUSY_MARKER_PATTERN`
  + `codex_busy_marker_live()` helper + `CodexProvider.rule3a_busy_marker` override.
- `src/cli_agent_orchestrator/providers/claude_code.py` — D18 persona-credential
  vocabulary (`PERSONA_UNVERIFIED` / `PERSONA_UNAUTHENTICATED`),
  `classify_persona_credential()`, `_persona_credential_present()` keys-only probe;
  `_PERSONA_CREDENTIAL_KEY_CONFIRMED = False` (the finding).
- `src/cli_agent_orchestrator/services/terminal_service.py` — D21 polling-lifetime
  fix in `_wait_for_auto_responder_dialog_clear` + `_F491_DIALOG_CLEAR_MAX_LIFETIME`.

Tests:

- `test/providers/test_codex_busy_marker.py` (D16 AC3, NEW).
- `test/providers/fixtures/busy_marker/codex/busy-1.{txt,json}` / `busy-2.{txt,json}`
  (D16 corpus, `cp`'d byte-exact from the condition corpus, NEW).
- `test/services/test_f582_d21_dialog_clear_lifetime.py` (D21 AC8 arming, NEW).
- `test/auto_answers/fixtures/f530/05-trust-dir-startup-card-15a6fa21.{txt,yaml}`
  (D21 AC8 matcher PASS regression, NEW — exercised by the existing
  `test/auto_answers/test_f530_corpus.py`).
- `test/providers/test_mcp_ready_evidence.py` (D20 cline-exempt + no-override guard, +2 tests).
- `test/providers/test_persona_credential_unverified.py` (D18 AC5, NEW).

Out-of-tree artifact (supervisor lands):

- `/data/cao-scratch/9064394e/d22-root-hooks.diff` (D22 root-repo hook guard).

## Behavioral implementation

- **D16 (F581/AC3) codex leg.** `codex_busy_marker_live(text)` returns `True` on the
  byte-exact Working/Thinking spinner (`• Working (28s • esc to interrupt)`,
  anchored on the stable "esc to interrupt" hint via the existing
  `TUI_PROGRESS_PATTERN`), `False` when only the idle composer placeholder
  (`Ask Codex to do anything`) is present, `None` on an unidentifiable pane —
  identical contract to claude_code (D12d) and the slice-A kiro leg
  (`kiro_busy_marker_live`). `CodexProvider.rule3a_busy_marker` delegates to it.
  grok/cline keep the `BaseProvider` default `None` (non-goal this WP, Do-NOT 17).
  Fixtures `busy-1`/`busy-2` were `cp`'d byte-exact from the condition corpus
  (`codex-busy-1`/`-2`); `.txt` sha256 match the INDEX values `08c21768…` /
  `c631bce2…`. Redaction re-checked: no personal-provider email/token in the panes
  (the PII guard `test_fixtures_no_personal_pii.py` only flags personal-provider
  emails — none present).

- **D21 (F589 + F530 / AC8) codex half — arming/timing fix.** Attribution
  (`/data/cao-scratch/9064394e/f582-d21-attribution.md`, supervisor correction
  mid-2629): the `codex-resume-workdir-card` rule MATCHED and FIRED on the 04:10Z
  recurrence — the matcher is correct; the failing stage is an ARMING/TIMING gap.
  `_wait_for_auto_responder_dialog_clear` (terminal_service.py) evaluated only
  during a fixed ~12s window; a dialog that renders AFTER that window closes is
  never re-evaluated (a static pane produces no quiescence tick, and there is no
  periodic sweep), so `send_input` races into an unanswered dialog — the #386
  stall. **Fix:** the poll loop keeps its LIFETIME open past the base `timeout`
  for as long as a whitelisted dialog is PROVABLY on screen
  (`_responder_dialog_pending()` True), bounded by a hard cap
  `_F491_DIALOG_CLEAR_MAX_LIFETIME = 45.0`. The base `timeout` still governs the
  no-dialog common case (the loop never sleeps past it when nothing is pending),
  so the change is scoped to exactly the late-render class.

  **Empirical matcher ruling (settles the AC8 "must fail on pre-fix matcher"
  tension).** Per the supervisor's ruling I ran the real matcher+classifier
  against the byte-exact F530 corpus fixture `codex-dialog-blocked-1` (the 18:20Z
  trust-dir stall pane, seat 15a6fa21):
  - the fixture is **18 source rows**, which fit entirely inside
    `DIALOG_REGION_LINES = 20` — the OpenAI Codex startup card below the trust
    prompt does **NOT** push the dialog out of the match tail;
  - `codex-trust-dir` (contains), `codex-trust-dir-subdir` (contains) and
    `codex-trust-dir-card` (regex, compiled verbatim + IGNORECASE) all match the
    (chrome-filtered) region;
  - the classifier verdict on the pane is `WAITING_USER_ANSWER` (no PROCESSING
    busy-veto), so the full fire decision is **WOULD_FIRE = True**.

  Therefore the matcher+classifier **fire once** on this byte-exact fixture:
  there is NO second (tail-composition) matcher defect on this pane, and AC8's
  "currently NOT matched — the test must fail on the pre-fix matcher" wording is
  superseded by the attribution. AC8 is tested as the ARMING gap only, plus a
  PASS regression pin of the fire decision on the fixture
  (`test/auto_answers/fixtures/f530/05-trust-dir-startup-card-15a6fa21`,
  `expected_rule: codex-trust-dir`, `xfail: false`). The tail-composition
  hypothesis is recorded in Deviations as empirically checked and NOT reproduced
  on the byte-exact pane.

- **D20 (F537 / AC7) cline finding.** cline inherits the `BaseProvider`
  `mcp_ready_evidence()` default `None` → classified `mcp_unverified` (exempt,
  never ERROR — Do-NOT 23). **#393 "no durable artifact" finding:**
  `ClineCliProvider._materialize_mcp_settings` (cline_cli.py:449-503) MATERIALIZES
  `cline_mcp_settings.json` at spawn (with the F537 per-server `timeout` so cline
  does not skip cao-mcp-server on its 3 s default), but cline emits NO durable
  connect-ok line / readiness artifact on a SUCCESSFUL attach — the handshake is
  internal to cline's process with no log CAO can read. Inventing a connect-log
  parser here would be fabricating an artifact cline does not write, so cline
  stays `mcp_unverified`. Tests pin the cline exemption and guard that NO provider
  declares an artifact yet (if one later does, the assign-result wiring must land
  with it).

- **D18 (F575 / AC5) persona_unverified.** **Finding (keys-only probe,
  2026-08-30):** the live production plane `~/.claude/.claude.json` has NO
  `oauthAccount` key (8 top-level keys; only `userID` is auth-like). So the
  blueprint's cited D18 credential marker (`oauthAccount` absent ⇒ unauthenticated,
  SHOULD-2) is UNVERIFIED on this build and cannot be trusted as the
  discriminator. Per D18's own rule ("until confirmed, a missing key yields
  `persona_unverified`, never `E-PERSONA-UNAUTHENTICATED`"), the shipped
  `classify_persona_credential()` returns `persona_unverified` whenever the key is
  unconfirmed (`_PERSONA_CREDENTIAL_KEY_CONFIRMED = False`), and only fails a seat
  closed once a build CONFIRMS the key name AND the credential is absent. The
  fail-closed branch is BUILT but GATED (not dead — the confirmed-key test arms
  prove it fires correctly once the flag flips); no other markers are guessed
  (Do-NOT 19). The `_persona_credential_present()` probe reads ONLY key presence,
  never the credential value (PII/secret discipline). The seeder guard
  (`_ensure_sandbox_onboarding_state`, claude_code.py) was NOT widened — there is
  no persona `PlaneClass` value and no F567-matrix persona capture, so widening
  the guard would be guessing (deferred with the fail-closed branch).

- **D22 (F543 / AC9) root-hook idempotency guard.** Delivered as a validated diff
  at `/data/cao-scratch/9064394e/d22-root-hooks.diff` (the root repo is read-only
  to this lane; the supervisor lands it). It adds a per-incarnation idempotency
  guard to BOTH root hooks — `.claude/hooks/supervisor-inbox-drain.sh` (Gate 2.5,
  an idempotent-ack no-op) and `.claude/hooks/f213-callback-rewake.sh` (an
  idempotent-arm no-op) — keyed on `CAO_TERMINAL_ID` + `CAO_PROCESS_INCARNATION`
  under `$CAO_HOME_DIR`, engaged only when `CAO_OVERLAY_HOOKS_ACTIVE=1` AND the
  sentinel is present. Both edited hooks pass `bash -n`; the patch body is
  `git diff` output and `git apply --check`-clean against the committed root hooks
  AND the live root worktree. Landing the diff ALONE is behaviour-preserving (the
  env test is false until the overlay opts in); the companion wiring (overlay
  exports the env + stamps the sentinel) is the D22 SHOULD-5 follow-up (below).

- **D23 (F578 / AC10) MCP/API param wire.** Report-only, per brief. The DB/service
  row-state seam for `expire_after_s` / `supersede_key` landed in slice A. The thin
  MCP `send_message` tool param + API route surface (so a caller can pass these
  values end-to-end) is the **named follow-up**: `F578-D23-send-message-param-wire`
  — thread `expire_after_s`/`supersede_key` through the `send_message` MCP tool and
  the `POST /terminals/{id}/inbox` (or equivalent send) route into the existing
  `create_inbox_message` params. No code this slice.

## Verification (box)

All verification ran on `box@grok-box-004` (pinned; never grok-box-1 which is
frozen). None on the laptop. Verified SHA `7042a160b9de8bd99dc4d6b5301df90086f143fb`.

Slice-B AC scope + affected regression suites:
```text
test/providers/test_codex_busy_marker.py
test/services/test_f582_d21_dialog_clear_lifetime.py
test/auto_answers/test_f530_corpus.py
test/providers/test_mcp_ready_evidence.py
test/providers/test_persona_credential_unverified.py
test/providers/test_kiro_busy_marker.py
```
- pytest: **43 passed, 2 xfailed** in ~1.9s. (The 2 xfailed are pre-existing
  F530-corpus `xfail(strict)` cases; the slice-B fixture `05-…` is a PASS pin.)
- black `--check --line-length 100`: **7 files unchanged** (pass).
- isort `--check-only --line-length 100`: **pass** (clean).
- mypy `--strict` parity (touched source files
  `providers/codex.py`, `providers/claude_code.py`, `services/terminal_service.py`,
  same box): HEAD `7042a160` **117 errors in 3 files** == BASE `4bf27b92`
  **117 errors in 3 files**. **Delta 0** (the 117 are pre-existing baseline debt in
  these large files; this slice introduces no new strict error).

## Mutation ledger

One mutant per built arm — each applied at the verified tip on box-004
(python-patch → named test → revert → clean). **4/4 KILLED.**

| Mutant | Applied edit | Named selector | Result |
|---|---|---|---|
| D16-codex-never-busy | `codex_busy_marker_live` returns `None` instead of `True` on the marker branch | `test_codex_busy_marker.py::test_helper_true_on_live_codex_busy_fixtures` | pre_rc=0 test_rc=1 post_revert_rc=0 — KILLED |
| D21-fixed-window | Revert the loop to `while time.monotonic() - start < timeout` (drop the lifetime extension) | `test_f582_d21_dialog_clear_lifetime.py::…::test_late_render_after_base_timeout_still_clears` | pre_rc=0 test_rc=1 post_revert_rc=0 — KILLED |
| D20-cline-declares | `ClineCliProvider.mcp_ready_evidence` returns a declared+connected `MCPEvidence` | `test_mcp_ready_evidence.py::test_cline_provider_is_exempt_unverified_no_durable_artifact` | pre_rc=0 test_rc=1 post_revert_rc=0 — KILLED |
| D18-failclosed-when-unconfirmed | `classify_persona_credential` fails closed on absent credential even while `key_confirmed=False` | `test_persona_credential_unverified.py::test_unconfirmed_key_missing_credential_is_unverified_not_failclosed` | pre_rc=0 test_rc=1 post_revert_rc=0 — KILLED |

## Deviations and deferred rows (every deferral with its named gap)

- **D21 blueprint divergence (matcher framing).** The frozen D21 row calls #386
  "the #386 matcher defect … a rule that textually matches and does not fire is
  the bug, not the rule" and names `dialog_region` tail-composition as the leading
  hypothesis. Both the slice-A attribution (04:10Z, `codex-resume-workdir-card`
  fired 26×) and this slice's empirical replay of the byte-exact 18:20Z trust-dir
  pane (`codex-dialog-blocked-1`: WOULD_FIRE=True; 18 rows fit in
  `DIALOG_REGION_LINES=20`) CONTRADICT the matcher-defect framing. Per the
  supervisor's correction, D21's fix targets the polling **lifetime**, NOT the
  matcher. **Tail-composition hypothesis: empirically checked and NOT reproduced**
  on the byte-exact fixture (the startup card does not push the dialog out of the
  20-line tail). Recorded rather than papered over.
- **D21 kiro F589 half — DEFERRED. Named gap:** the corpus has no live kiro
  DIALOG_BLOCKED screen (`INDEX.md` GAPS: "kiro DIALOG_BLOCKED: no live kiro dialog
  screen in scrollbacks — only classifier source lines and supervisor prose"). The
  F589 "Your connection was interrupted" screen was not captured, so the
  `resume-nudge` rule + its AC8 arm cannot be built against a byte-exact screen.
  Lands in the capture-gated follow-up.
- **D20 kiro evidence parser + cline artifact + assign-result wiring — DEFERRED.
  Named gap:** the kiro MCP readiness artifact format is cited to `HANDOFF.md:237`,
  which is not present in this tree, and the corpus `INDEX.md` has no kiro MCP
  readiness artifact — implementing the parser would be inventing the artifact
  shape. cline writes no durable connect-ok artifact (#393 finding above). The
  provider-agnostic classifier core (`MCPEvidence`, `classify_mcp_readiness`,
  `BaseProvider.mcp_ready_evidence` default) landed in slice A; no assign-result
  wiring lands until a provider declares a real artifact.
- **D18 fail-closed credential branch + seeder-guard widening — DEFERRED (gated).
  Named gap:** no live known-authenticated persona `.claude.json` to confirm the
  `oauthAccount` key (keys-only probe found it absent even on the live plane), and
  no F567-matrix persona capture. `E-PERSONA-UNAUTHENTICATED` therefore ships GATED
  behind `_PERSONA_CREDENTIAL_KEY_CONFIRMED = False` (shipped state:
  `persona_unverified`); the seeder guard is not widened (no persona `PlaneClass`).
- **D22 SHOULD-5 companion wiring — FOLLOW-UP.** The landed diff is inert until the
  overlay opts in. Companion (named `F543-D22-overlay-idempotency-wiring`): export
  `CAO_OVERLAY_HOOKS_ACTIVE=1` in the overlay-composed hook env
  (`claude_code.py _write_terminal_settings`) and have `supervisor_drain.py` /
  `supervisor_ack.py` / the overlay rewake `touch` the same per-incarnation
  sentinel on success. F476's richer wake-cursor drain supersedes these thin
  transports when it lands (debt on #331).
- **D23 MCP/API send_message param wire — FOLLOW-UP** (`F578-D23-send-message-param-wire`,
  above). Report-only per brief; the DB/service seam is complete from slice A.
- **AC11 (S4):** unchanged from slice A — #395/#441 re-milestoned; #386 stays in S2
  until AC8's live spawn passes (this slice ships the fixture-level + arming-level
  arms; the live-spawn e2e is the gate's, not the builder's).

## Box-actions ledger

All invocations used `CAO_BOXES="box@grok-box-004" bash scripts/box-run.sh
<label> -- '<cmd>'` from `/home/chao/VScode_projects/cli-subagents` (root repo).
box@grok-box-1 was never used (frozen; auto-refused). No laptop suite/mypy runs.

| Label | Box command / action | Result |
|---|---|---|
| `f582-sliceb-verify` | fetch+checkout `5be189a1`; uv sync --frozen; black/isort --check; pytest AC scope | black 3 files would-reformat, pytest 43 passed/2 xfailed (fmt fixed in `213af958`) |
| `f582-sliceb-fmtdiff` | checkout `5be189a1`; black/isort `--diff` | captured exact fmt diffs (3 test files); applied locally |
| `f582-sliceb-verify2` | fetch+checkout `213af958`; uv sync --frozen; black/isort --check; pytest AC scope | black 7 unchanged; isort clean; pytest 43 passed/2 xfailed |
| `f582-sliceb-mypy` | checkout head `213af958` then base `4bf27b92`; `uv run mypy --strict` on the 3 touched source files (same box A/B) | HEAD 117 == BASE 117 (delta 0); left box at head |
| `f582-sliceb-mutants` | checkout `213af958`; 4 designed mutants (python-patch → named test → `git checkout` revert) via `~/box-scratch/f582-mutants.sh` (removed after) | D16/D20/D18 KILLED; D21 REVIEW (test not discriminating — fixed in `7042a160`) |
| `f582-check-mut` | read-only `sed -n` of the D21 loop region (diagnosing the D21 REVIEW) | confirmed source matched; the test, not the mutation, was the gap |
| `f582-d21-recheck` | fetch+checkout `7042a160`; black/isort d21 test; pytest d21; re-run D21 mutant | black/isort clean; 3 passed; D21 mutant pre_rc=0 test_rc=1 post_revert_rc=0 — KILLED |

- Raw ssh: none (all through box-run.sh).
- Checkout SHA left on box-004: `7042a160` (head), clean working tree after each
  run (every mutant reverted with `git checkout`; mypy A/B restored to head).
- Environment mutations: only the disposable per-worktree `.venv` from
  `uv sync --frozen`. No apt/pip/global installs; no lockfile change.
- Temp files: `/tmp/f582-*.txt` on the box (transient); `~/box-scratch/f582-mutants.sh`
  removed at end of its run. None left outside /tmp.
- Deviations from box-ops rules: none.
