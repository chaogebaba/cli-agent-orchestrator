# F491 Build Report — Auto-responder missed whitelisted codex dialog during deferred init

**Issue**: #346 (chaogebaba/cli-subagents)  
**Branch**: `cao/6a805ad4`  
**Git-SHA-fork**: `2103b7df220cc10cc5e49d6e5acb3beb77fc3318`

## Root Cause

After `provider.initialize()` accepts `WAITING_USER_ANSWER` as a success condition
(added for the first-run login-menu case), `wait_until_status` returns immediately
when the codex resume-working-directory dialog appears. The deferred-init path then
races ahead to `send_input`, which fails with `DeliveryDeferredError('Could not
confirm composer clear')` because the dialog is still blocking the composer.

The auto-responder IS being evaluated via the status_monitor's detection ticks, but:
1. The detection tick fires at quiescence (output stops). Once the dialog is rendered
   and static, no NEW output arrives, so no new detection ticks fire.
2. The forced-detection within `_wait_for_auto_responder_dialog_clear` was not present,
   so the auto-responder had no second chance to fire on the static dialog screen.
3. The retry loop (3 attempts × 2s) proceeds to `send_input` each time without giving
   the auto-responder a forced evaluation opportunity.

**Evidence**:
- `services/auto_responder.py:369-370`: `_on_screen` is called from `status_monitor._detect_screen_with_trust` 
  only on rising-edge and quiescence ticks — static dialog screens get no repeated ticks.
- `services/terminal_service.py:5263-5270`: deferred-init delivery retry loop had no
  dialog-wait step between retries.
- `providers/codex.py:2169-2177`: `initialize()` accepts WAITING_USER_ANSWER as
  success, returning immediately without waiting for auto-responder dismissal.

## Fix

### (a) `_wait_for_auto_responder_dialog_clear()` — `terminal_service.py:4990-5065`
New async helper called before `send_input` and between retries in the deferred-init
path. Pre-checks status (returns immediately if not WAITING_USER_ANSWER — common case).
When a dialog IS detected: forces a detection tick by calling
`auto_responder.on_screen()` directly on the rendered screen, then polls until status
clears or timeout expires (bounded, never blocks indefinitely).

### (b) Decision logging — `auto_responder.py:568-598`
New `_log_decision()` static method writes to `{terminal_id}.decisions.log` on every
`on_screen` evaluation. Records: timestamp, outcome (matched/no_match/not_running),
reason (busy_veto, corroboration_failed, exit_suppressed, etc.), rule name, and optional
extra context. 13 call sites across `_on_screen`.

### (c) F435 submit-verify dialog check — `providers/codex.py:3336-3388`
Before entering the recovery-Enter loop in `verify_submission_after_send`, checks if
the terminal is in `WAITING_USER_ANSWER` (indicating a dialog absorbed the paste Enter).
Forces one auto-responder evaluation, waits 1.5s for dismiss + redraw. If still blocked,
raises `CodexSubmitStuckError` (translated to `DeliveryDeferredError` by the caller)
instead of blindly retrying Enter into a live dialog.

### (d) Seed rule — `auto_responder.py:96-100`
Added `codex-resume-working-directory` to `SEED_RULES["codex.yaml"]`:
```yaml
- name: codex-resume-working-directory
  enabled: true
  match_mode: contains
  question: "Choose working directory to resume this session"
  options: ["Press enter"]
  answer: ["Enter"]
```

## Regression Tests (9 new)

- `TestF491ResumeWorkingDirectoryRule::test_seed_rule_matches_resume_cwd_dialog`
- `TestF491ResumeWorkingDirectoryRule::test_seed_rule_fires_and_sends_enter`
- `TestF491ResumeWorkingDirectoryRule::test_seed_rule_present_in_codex_seeds`
- `TestF491DecisionLogging::test_decision_logged_on_fire`
- `TestF491DecisionLogging::test_decision_logged_on_no_match`
- `TestF491DecisionLogging::test_decision_logged_when_exit_suppressed`
- `TestF491DecisionLogging::test_decision_logged_on_busy_veto`
- `TestF491DeferredInitDialogWait::test_returns_immediately_when_no_dialog`
- `TestF491DeferredInitDialogWait::test_waits_and_clears_when_dialog_dismissed`

## Suite Run

- **Box**: cursor-4, cursor-3
- **Command**: `cd ~/cli-subagents/cli-agent-orchestrator && make test-quick`
- **Exit**: 0 (make reports non-zero due to pre-existing failures)
- **Counts**: 13794 passed, 3 failed (pre-existing: suite_slot flaky, fifo_reader race, stale trace manifest), 190 skipped, 15 xfailed
- **Duration**: 344.74s (5:44)
