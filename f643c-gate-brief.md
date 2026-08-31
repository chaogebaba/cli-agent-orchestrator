# F643c (#498) EMPIRICAL gate brief — codex first-delivery submit race fix

Re-dispatch artifact (the prior build report was destroyed by a terminal reap;
the CODE under review is unchanged and lives on the lane branch).

- Lane branch: cao/0436cf96 in /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator
- Lane HEAD: 2dae6273
- A/B base: b48e7b92
- Diff: providers/codex.py +192/-3; test/providers/test_codex_submit_verify_f643c.py (new, 16 tests, 370 lines)

## What the fix does
1. `pre_paste_gate()` — bounded 12s readiness wait using `_codex_tui_is_ready_for_submit()`
   (reuses STARTUP_FOOTER_PATTERN) wired at terminal_service.py:6153 BEFORE
   capture_submission_baseline + send_keys, so the first delivery no longer races
   codex 0.151.0 resume-TUI init (model:loading / Resuming session / trust dialog / MCP startup).
2. Recovery leg — `_composer_holds_own_draft` content-signature match -> re-send Enter ->
   re-verify via rollout, closing the dropped-Enter mode F435's chip-only loop missed.

## Gate scope
- Full fork suite A/B on a box (base b48e7b92 vs lane 2dae6273): no regressions.
- The 16 new tests green; mutation checks on the readiness predicate + recovery leg
  (invert/short-circuit each and show a test fails).
- Verdict per F244 contract.
