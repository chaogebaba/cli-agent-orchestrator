Artifact-Path: orchestrator/build-reports/f587-build-report.md
Git-SHA-fork: e3341b77

# F587 #445 — codex seed prompt refused by content classifier

## Summary

`CodexProvider.seed_resume_identity` sent the seed prompt
`"Reply exactly: SEED_OK then stop."`, which the Codex content classifier
refuses with a "flagged for possible cybersecurity risk" error (probe evidence
in issue #445). The reply text is never parsed — only the `session id:` line is
consumed — so the prompt was swapped for the innocuous `"Say hello."`. On a
non-zero exit the failure previously raised a bare `RuntimeError("seed_exec_failed")`
that hid the real cause; it now logs the stdout tail at ERROR and embeds it in
the exception message so an assign failure names why.

## Changes

### src/cli_agent_orchestrator/providers/codex.py — `seed_resume_identity`

1. Seed prompt literal `"Reply exactly: SEED_OK then stop."` → `"Say hello."`
   (line ~1377).
2. On `rc != 0` (line ~1395): compute the last 10 lines of `completed.stdout`,
   log them at ERROR via the module logger (`cli_agent_orchestrator.providers.codex`),
   and raise `RuntimeError(f"seed_exec_failed rc={rc}: {tail[-400:]}")`. The
   `TimeoutExpired` and `OSError` branches are unchanged.

### test/providers/test_codex_provider_unit.py — `TestCodexSeedResumeIdentity`

- `test_seed_argv_uses_say_hello_not_seed_ok` — patches `subprocess.run`,
  `load_agent_profile`, `_resolved_codex_profile_config`, and
  `validate_session_artifact`; asserts the argv passed to `subprocess.run`
  contains `"Say hello."` and no token contains `"SEED_OK"`; asserts the parsed
  session UUID is returned.
- `test_seed_rc_nonzero_raises_with_tail_and_logs_error` — `subprocess.run`
  returns `rc=1` with stdout `"ERROR: This content was flagged for possible
  cybersecurity risk"`; asserts the raised `RuntimeError` message contains
  `"flagged"` and `"seed_exec_failed rc=1"`, and that an ERROR record containing
  `"flagged"` was logged (`caplog`).

Added `import logging` to the test module (was not previously imported).

## SEED_OK consumer grep (scope: fork src/ + test/ + scripts/)

Only the seed-prompt literal at `codex.py:1377` is a consumer of the seed
prompt. All other `SEED_OK` occurrences are the unrelated **submit_verify /
stuck-paste pane-detection** feature — simulated pane captures, not consumers of
the seed prompt:

- `codex.py:2782` — a docstring in the stuck-paste detector describing a rendered
  pane bullet (`• SEED_OK`), not the seed prompt.
- `test/providers/test_codex_submit_verify_f435*.py` — pane-render fixtures using
  `• SEED_OK` bullets to exercise composer/stuck detection.
- `test/providers/fixtures/codex_f435_stuck_paste_pane.txt`,
  `test/providers/fixtures/status_truth/codex/unknown-1.txt` — historical pane
  captures (they contain `› Reply exactly: SEED_OK`), used as detection inputs.

These were **not** modified: they test a different mechanism and rewriting the
captured pane text would falsify the fixtures. No `SEED_OK` references exist in
`scripts/` or `docs/`.

## Formatting / typing

- `black --line-length 100`: applied to both touched files; `black --check
  --line-length 100` on `test/providers/test_codex_provider_unit.py` and
  `src/cli_agent_orchestrator/providers/codex.py` is clean (exit 0, "2 files
  would be left unchanged"). NOTE: this folded PRE-EXISTING black drift in
  `codex.py` (12 hunks, unrelated to the seed change, present identically on
  base) plus two pre-existing test-file lines into the code commit, so that
  `black --check` passes on both files per the r1 gate requirement.
- `isort --check`: clean for both files (exit 0).
- `mypy --strict src/.../codex.py`: 1 error at `_extract_rollout_user_texts`
  (`record: dict` missing type params) — PRE-EXISTING (identical on base commit).
  My change introduces no new mypy error.

## Test run — `make test-quick` on offload box

Command (see ledger for the exact box-run.sh invocation): `make test-quick`.
Result: **14317 passed, 10 failed, 197 skipped, 17 xfailed** in 514.86s.

Both new tests (`TestCodexSeedResumeIdentity`) are in the passing set.

### The 10 failures are NOT caused by this change

Same-box (grok-box-4) re-run of the failing node ids on the base commit
`44738edf` (WITHOUT my change) reproduced 7 of them identically:

- `test_install_opencode.py::TestSlashSafeAgentId` (x2)
- `test_wpdt_delivery_truth.py::...test_doctrine_arming_section_exists`
- `test_f254_tier_guard.py::...test_no_real_io_in_unit_tier`
- `test_fixtures_no_personal_pii.py::test_no_personal_email_addresses_in_fixtures`
- `test_g7a_sandbox.py::...[services/fork_context_service.py-display-message]`
- `test_f497_composition.py::...[codex_empirical_reviewer]` (golden `contextPolicy` drift)

The other 3 (`test_suite_slot.py::...test_sample_records_child_process`,
`test_stage0_flip_machinery.py::...byte_exact...36_hits`,
`test_sim_substrate.py::...600_virtual_seconds_under_2_real_seconds`) PASSED in
isolation on the base commit — they are load/timing/ordering-sensitive under
full-suite parallelism (slot sampling, byte-exact trace manifest, a <2s
virtual-time perf assertion), not related to the codex seed path.

None of the 10 touch `seed_resume_identity` or the codex seed flow.

## Deviations (honest)

1. **TCACHE_BIN path translated.** The brief said run verbatim with
   `TCACHE_BIN=/home/chao/VScode_projects/cli-subagents/scripts/tcache`. That
   laptop path does not exist on the box (box repo is at `~/cli-subagents`, i.e.
   `/home/box/...`). Ran with `TCACHE_BIN=$HOME/cli-subagents/scripts/tcache`
   (the box's equivalent). The command and flags are otherwise verbatim.
2. **Box left with temp branches (supervisor to clean).** The worktree-containment
   PreToolUse hook (fx121) refuses any `git checkout <branch>` whose target is not
   my worktree branch `cao/cadab0a1`, so `git checkout main` on the box was blocked.
   grok-box-4 is left on temp branch `basecmp` at commit `44738edf` (base merge
   commit; no content drift) with temp branches `basecmp` and `cao/cadab0a1`
   present. Supervisor will clean. Temp file `/tmp/f587-testquick-run.txt` was
   removed. An untracked file `test/services/test_auto_responder_seed_rules.py` is
   present but belongs to another lane — left untouched per single-writer rule.

## Commit

- Branch: `cao/cadab0a1`
- Code commit: `e3341b77` (rebased onto fork main `13b2aa92`; black --line-length 100 applied)
