# f-seedok — codex seed prompt back to SEED_OK marker

User order 2026-08-30 — supersedes F587 #445 (`"Say hello."`). Isolated worktree
off `main e64684f9`; committed on branch `cao/8f70c4f1`, not pushed.

## What changed

`CodexProvider.seed_resume_identity` in
`src/cli_agent_orchestrator/providers/codex.py` (~line 1509):

1. **Seed prompt** reverted to require the SEED_OK literal.
   - old (F587): `Say hello.`
   - new: `Reply with exactly the text SEED_OK and nothing else.`
2. **SEED_OK marker verification added** (new behaviour). After the rc-check, the
   seed now fails unless `SEED_OK` appears in the codex stdout. rc==0 with a
   parseable `session id:` is no longer sufficient — a refusal/empty answer that
   still exits 0 is rejected. Missing marker → `RuntimeError("seed_marker_missing: <tail>")`,
   surfacing the same bounded, secret-scrubbed stdout tail F587 introduced
   (`_seed_failure_tail` / `_SEED_FAILURE_TAIL_LINES`), logged at ERROR.
3. **F587 stdout-tail surfacing kept intact** — the rc!=0 branch, the
   TimeoutExpired branch (F587 D19 tail carry), and the OSError branch are all
   unchanged.

## Important clarification vs the task brief

The task described steps 1–3 as "restoring" old behaviour. The history does not
match that framing, and I implemented the user's explicit intent rather than a
literal git revert:

- `git log -p e3341b77` shows the pre-F587 seed prompt was
  `"Reply exactly: SEED_OK then stop."` — **but the reply text was never parsed**;
  the seed only ever consumed the `session id:` line. There was **no** SEED_OK
  marker/pane verification in the old code to "restore". Task step (3) is
  therefore a NEW verification, which I added as ordered.
- F588 (`orchestrator/build-reports/f587-build-report.md`, lines 1–60) records
  that the Codex content classifier refused the **specific phrasing**
  `"Reply exactly: SEED_OK then stop."` ("flagged for possible cybersecurity
  risk"), not any SEED_OK literal. The user-specified phrasing
  `Reply with exactly the text SEED_OK and nothing else.` avoids the flagged
  `exactly:` colon form and the `then stop` imperative.
- LIVE smoke against real codex 0.151.0 confirms the new phrasing is **accepted**
  (not refused) and the model returns the `SEED_OK` literal. See smoke below.

## Test ids (test/providers/test_codex_provider_unit.py, TestCodexSeedResumeIdentity)

- `test_seed_argv_requires_seed_ok_not_say_hello` — argv's prompt must contain
  `SEED_OK`, must not be `Say hello.`, and must not contain the F587/F588-refused
  `exactly:` / `then stop` phrasing. (rewritten from
  `test_seed_argv_uses_say_hello_not_seed_ok`; its stdout mock now carries SEED_OK)
- `test_seed_missing_marker_raises_even_when_rc_zero` — NEW. rc==0 + valid
  `session id:` but no `SEED_OK` in stdout must raise `seed_marker_missing`, must
  log "SEED_OK marker missing" at ERROR, and must never reach
  `validate_session_artifact`.
- `test_seed_rc_nonzero_raises_with_tail_and_logs_error` — unchanged; F587's
  rc!=0 flagged-content tail-surfacing still asserted.

Result: 3 passed.

## Mutant ledger (empirically verified red)

| Mutant | Change | Test that caught it | Result |
|---|---|---|---|
| M1 | seed prompt reverted to `Say hello.` | `test_seed_argv_requires_seed_ok_not_say_hello` | RED (`assert 'SEED_OK' in 'Say hello.'`) |
| M2 | marker check disabled (`if False and ...`) | `test_seed_missing_marker_raises_even_when_rc_zero` | RED (`DID NOT RAISE RuntimeError`) |

Both mutants applied in-place, run, confirmed red, then reverted. Final tree = clean 3-pass.

## Checks

- `black --line-length 100 --check` — both files unchanged (pass).
- `isort --profile black --line-length 100 --check-only` — pass.
- `mypy --strict src/.../codex.py` — 1 error, **pre-existing and out of scope**:
  `_extract_rollout_user_texts` `dict` missing type params. Verified against the
  stashed base (base line 2100 → shifted to 2120 only by my added lines). My
  change introduces **zero** new mypy errors (parity held). Not touched — outside
  stated scope.

## LIVE smoke — NOT capped

`codex exec --skip-git-repo-check -C <scratch> "Reply with exactly the text SEED_OK and nothing else." < /dev/null`
against real codex-cli 0.151.0 (model gpt-5.6-sol). Prompt accepted (not
classifier-refused); `session id:` and `SEED_OK` both present. Last 5 stdout lines:

```
codex
SEED_OK
tokens used
13,092
SEED_OK
```

## Scope

Only the two files above. No push, no drive-by refactor, no fix of the
pre-existing mypy error.
