# F516 #371 — auto-responder robustness hardening — BUILD REPORT

Blueprint (frozen, pinned authority): `orchestrator/blueprints/f516-responder-hardening.md`
(Status FROZEN r10; DESIGN r9 GATE-YES, EMPIRICAL 0/0/3, user-authorized 00:20Z).

- Branch: `cao/3ff35106` (isolated worktree `.cao/worktrees/3ff35106`)
- Base: `c155f0e0` — current fork main (post-F522 lock-order fix 6cdf2d86 present)
- HEAD (this build, pre-report-commit): `01b38113`
- Rebase (commit 8): NO-OP — `origin/main` is still `c155f0e0`; branch is 0 behind / 8 ahead, linear.
- Staging: **exact blueprint 8-commit staging (supervisor RULING: option B).** No
  early fire-path HOLD; the D6 no-history HOLD lands in its blueprint-staged
  commit (6). The empirical gate certified this exact staging.

## Commits (one per build-order step, §4)

| # | SHA | One-line |
|---|-----|----------|
| 1 | `2b40504f` | test(c1): frozen dialog fixtures (2 resume-cwd choosers + content-policy banner) + DialogReplay harness + injectable `_clock` seam |
| 2 | `fa61f9f3` | feat(c2): D3 consult at 3 draft_guard entries + `DialogOpenError` + D3p `match_verdict` + wait-site consult + `.matches` lint 5→6 + AC7 lint file |
| 3 | `fdf3267c` | feat(c3): D2 trust-order (classifier fast-path only; `_busy_veto` retained) + settle-in-barrier + `DialogRegion` digests + `_fire` takes region + codex resume-cwd→WAITING classifier. **No HOLD** (test_m3 red-window opens) |
| 4 | `8e7ae7c6` | feat(c4): D4 `schedule_detection_retry` + geometric backoff (1/2/4/8, cap 6) + `delay` on shared timer + retry wiring + barrier-False enumeration |
| 5 | `700e8fb0` | feat(c5): D5 `Rule.body_hash` + `_consumed_digests` + consume gate in barrier + per-rule reload reset via body-hash keying |
| 6 | `bce94b84` | feat(c6): D6 region-history (last-2) + `_scroll_excluded` (difflib) + cached pre-filter verdict + `match_verdict` banner path + no-history HOLD (**test_m3 GREEN from here**) + `_VetoStreakState` |
| 7 | `317ee8ba` | docs(c7): D1 suppress-tier audit (codex-only adoption) + codex `--yolo` documented as the adopted lever |
| 8 | `01b38113` | chore(c8): rebase (no-op) + `trace_manifest.txt` regen (line-shift only) + pinned-line break-table updates (probe_04, probe_08, stage0b) |

## Per-commit test counts — required suites

Required suites: `test/services/test_f55_auto_responder_hardening.py` +
`test/services/test_auto_responder.py`. Verified at each commit:

| Commit | Result | Note |
|--------|--------|------|
| c1 `2b40504f` | 82 passed | baseline |
| c2 `fa61f9f3` | 82 passed | |
| c3 `fdf3267c` | 81 passed, **1 failed (test_m3)** | AC7 red-window opens |
| c4 `8e7ae7c6` | 81 passed, **1 failed (test_m3)** | red-window |
| c5 `700e8fb0` | 81 passed, **1 failed (test_m3)** | red-window |
| c6 `bce94b84` | **82 passed, 0 failed** | test_m3 GREEN (D6 no-history HOLD) |
| c7 `317ee8ba` | 82 passed | |
| c8 `01b38113` | 82 passed | |

**`test_m3` red-window (c3–c5) is EXPECTED and blueprint-mandated.** AC7:
`test_m3_matching_options_content_without_corroboration_does_not_fire`
(test_f55:190-204) is *BEHAVIOR-CHANGED-BUT-GREEN — under D2 it would fire; it
stays green ONLY via D6's no-history HOLD*. D2 lands in c3 and D6's HOLD lands in
c6, so the test necessarily fires (red) for c3–c5 and is green from c6 onward.
No other test in the required suites is red at any commit.

## New F516 tests (ACs 1–12 coverage): 40

- `test_f516_fixtures.py` — 4 (fixtures + `_clock` seam determinism)
- `test_f516_consult.py` — 8 (D3 consult, D3p, DialogOpenError, AC9)
- `test_f516_lint.py` — 2 (AC7 lint)
- `test_f516_d2.py` — 3 (D2 fast-path fire on WAITING, AC11 settle, digest-domain)
- `test_f516_d4.py` — 6 (AC3/AC5 backoff via delay-path seam)
- `test_f516_d4_wiring.py` — 3 (retry-request wiring)
- `test_auto_responder_f516_d5.py` — 5 (AC4/AC5 consume + reload)
- `test_auto_responder_f516_d6.py` — 6 (AC2(a)/AC2(b)+delivery-arm, banner path, veto-streak)
- `test_auto_responder_f516_d1.py` — 3 (D1 codex lever + audit doc)

Break-table pinned updates (commit 8): `test_probe_04`, `test_probe_08`,
`test_stage0b_receiver_evidence::test_d6_…` — all green at HEAD. Broad touched-set
sweep at HEAD: **243 passed** (1 skipped, 2 xfailed), 0 failed.

## Acceptance-criteria status (at HEAD)

| AC | Status | Where |
|----|--------|-------|
| AC1 | offline arm green (fast-path fire on WAITING); full path live | `test_f516_d2`/`d6` |
| AC2(a) | GREEN | `test_auto_responder_f516_d6::test_ac2a…` |
| AC2(b) + delivery arm | GREEN | `test_auto_responder_f516_d6::test_ac2b…` |
| AC3 | GREEN (delay-path seam) | `test_f516_d4` |
| AC4 | GREEN | `test_auto_responder_f516_d5::test_ac4…` |
| AC5 | GREEN (body-hash keying) | `test_auto_responder_f516_d5::test_ac5…` |
| AC6 | GREEN | `test_auto_responder_f516_d6::test_veto_streak…` |
| AC7 | GREEN at HEAD (test_m3 red c3-c5 by design) + lint | required suites + `test_f516_lint` |
| AC8 | table GREEN; live-spawn **LIVE-ONLY** | `docs/f516-d1-suppress-tier-audit.md` |
| AC9 | GREEN | `test_f516_consult::test_ac9…` |
| AC10 | GREEN (pinned, unchanged) | `test_f55::test_m1_variant2_bottom`, `test_incident_variant2` |
| AC11 | GREEN | `test_f516_d2::test_ac11a…`, `test_digest_domain…` |
| AC12 | **LIVE-ONLY** | run ledger |

## Deviations

**None from the blueprint's decisions, §6 wall, or 8-commit staging.**

### Restage note (F524 #379 — supervisor-side late-delivery defect)

This branch was built TWICE in good faith due to a delivery defect on the
supervisor side (filed F524 #379, P0): the "take (B)" ruling (inbox msg 1210,
00:54Z) sat in `pending` ~68 min and was delivered only after the option-A build
had already reported COMPLETE at 02:00Z. Acting on the last instruction received,
this worker rewound `cao/3ff35106` from the option-A HEAD `3c2019a6` to `c2` and
restaged as option B. The supervisor preserved the full option-A chain as branch
**`cao/f516-optionA-preserved`** at `3c2019a68fd0456cf98ae10efd9d06c46c415e69`
(do not delete). The supervisor then confirmed: **continue option B** (blueprint-
exact staging removes the DEV-1 equivalence question from the gate by
construction).

### Intended relationship to `cao/f516-optionA-preserved`

`git diff cao/f516-optionA-preserved..HEAD` is **NOT empty**, but every
difference is non-behavioral. Reported honestly per supervisor request:

- **Production code — behaviorally identical.** `codex.py`, `draft_guard.py`,
  `terminal_service.py`, the fixtures, and the replay harness are BYTE-IDENTICAL
  between the two chains. `status_monitor.py` differs only in comment wording
  (zero code change). `auto_responder.py` differs only in (a) comment wording,
  (b) helper placement, (c) one `_log_decision("firing")` line moved two lines
  earlier, and (d) **option-A retains a now-DEAD `_no_history_hold` helper method
  that option-B removed** — option-A introduced it in its c3 early-HOLD arm and
  never deleted it once the inline `had_history` logic superseded it; option-B
  never created it. The live fire-path decision block is byte-identical between
  the chains, so runtime behavior is the same. The dead-method removal is the
  single substantive tree difference and is an improvement (no dead code).
- **Tests — coverage-equivalent, different authoring.** `test_f516_d4`,
  `test_auto_responder_f516_d1/d5/d6` have identical test sets. `test_f516_d2`
  drops option-A's `test_d2_no_history_hold_uncorroborated_match_does_not_fire`
  (which asserted a c3-transient behavior that cannot be green across the
  option-B c3-c5 red-window) in favor of three staging-stable assertions.
  `test_f516_d4_wiring` swaps option-A's `test_no_history_hold_requests_a_retry`
  for `test_unknown_dialog_episode_requests_a_retry`. Both suites are green at
  their respective HEADs.
- **Regenerated/doc artifacts.** `trace_manifest.txt` (regenerated on both
  chains — line-shift only), `docs/f516-d1-…` (one comment line), and this build
  report (rewritten for option B) differ as expected.

Conclusion: the non-empty diff does NOT indicate a bug in either chain. It is the
expected residue of two independent authoring passes plus the intended
dead-helper removal. Both chains implement the same runtime behavior; option B is
the one whose COMMIT STAGING matches the frozen blueprint (test_m3 red c3-c5,
green from c6), which is the property the gate will read.

The D1 codex lever is the existing `--yolo` launch flag; no new/conflicting `-c`
override was added.

F522 lock-order Do-NOT honored: `schedule_detection_retry` arms its timer under
the monitor lock only, as a LEAF, with no responder lock held across it and no
status_monitor→responder→status_monitor re-entry.

## Not F516-caused (out of scope, left untouched)

Verified failing IDENTICALLY on clean base `c155f0e0`:
- `test/providers/test_omp_unit.py::test_extension_root_merges_mcp_without_overriding_explicit_terminal_id` — a `CAO_TERMINAL_TOKEN` MCP-env addition unrelated to F516.
- `test/services/test_ready_deadline_edge_probe.py::…one_lawful_owner@quarantine-serial` — a flaky async-cancel probe.

## Files touched (within the §6 wall)

- `src/cli_agent_orchestrator/services/auto_responder.py` (D2/D3p/D4-req/D5/D6)
- `src/cli_agent_orchestrator/services/draft_guard.py` (D3 consult + DialogOpenError)
- `src/cli_agent_orchestrator/services/status_monitor.py` (D4 seam only + `_clock`)
- `src/cli_agent_orchestrator/services/terminal_service.py` (wait-site consult)
- `src/cli_agent_orchestrator/providers/codex.py` (D2 resume-cwd classifier signal)
- `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt` (F492 regen)
- `docs/f516-d1-suppress-tier-audit.md` (D1 table)
- `test/…` fixtures, harness, and test files (new + break-table updates).
