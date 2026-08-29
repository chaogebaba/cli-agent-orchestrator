# F566 build report — kiro fresh spawn must not pass `--resume-id`

Branch `cao/f566-hotfix` (worktree `/data/cao-scratch/wt-f566`, base `main` = 22473fd3).
Commit **d5e4a32c8de490d4f6c86a2a94983e6ccd156680**, pushed to `origin/cao/f566-hotfix`. Not merged.

## Files + line ranges changed

`src/cli_agent_orchestrator/providers/kiro_cli.py` (+64/-41)
- L25 (old): dropped `import uuid` — no longer used anywhere in the file.
- L182-207 (class comment, was L184-198): replaced the "mint at spawn" doctrine with
  the empirical rule (uncreated `--resume-id` → `session.load.create_uncreated` →
  `--agent` ignored, 0 MCP servers), citing kiro-cli 2.20.1 and
  `/data/cao-scratch/kiro-mcp-probe/report.md`. Names #416 pt2 as the harvest follow-up.
- L263-273 (ctor, was L253-264): `allocated_session_uuid` is the prior id on
  `fork_context.mode == "resume"`, else **None** (was `f"sess_{uuid.uuid4()}"`).
- L294-300 (`resume_session_uuid` docstring): states that a fresh spawn persists
  `provider_session_id = NULL`.
- L303-317 (`_resume_session_id`): unchanged body (`return self.allocated_session_uuid`),
  rewritten docstring — it now returns None on a fresh spawn, which is the whole fix.
- L319-330 (`capture_session_uuid` docstring): the list-sessions fallback is now the
  live path for an unknown id, not a legacy shim; notes the method is unreachable on
  the create path today (kiro is not `supports_reauth_rebind`).
- L518-524 (launch comment): `--resume-id` only for a real prior id.
- L594-603 (`E-KIRO-SESSION-LOCKED` branch): gate changed from
  `self._fork_context is not None` to `if resume_id`, and the message interpolates
  `resume_id` instead of `self.allocated_session_uuid`.

`test/providers/test_kiro_cli_unit.py` (+98/-46), `test/providers/test_kiro_capabilities.py` (+6/-1),
`test/e2e/test_kiro_session_resume.py` (+35/-29) — see Tests below.

## Representation chosen: minted-vs-real

Two options were on the table: keep minting and carry a `_session_id_is_real` flag, or
stop minting so the id is simply absent until known. I took the second.

The deciding fact is that `allocated_session_uuid` is not private to the provider —
`services/terminal_service.py:2746` reads it and writes
`provider_session_id = resume_uuid or allocated_uuid` into the terminals row
(`terminal_service.py:2880`). With a flag, that row keeps recording a fake id, so a later
F444 wake resumes an id kiro never created and reproduces this exact bug from the DB.
Absence is the honest representation: `provider_session_id` is `Optional[str]`, the write
site already coerces a non-`str` to `None` (`terminal_service.py:2746-2748`), and NULL
correctly reads as "unknown until harvested". The accessors follow the same rule —
`_resume_session_id()` and `resume_session_uuid()` return None on a fresh spawn, and
`capture_session_uuid()` never invents an id.

Coherence check on every other reader of these names (`grep` over `src/`):
- `terminal_service.py:2746/2880` — handles None, as above. No crash.
- `terminal_service.py:608-620` (`_prepare_provider_runtime_identity`) — would reach
  `capture_session_uuid` with allocated None, but it returns early for kiro because
  `supports_reauth_rebind` is False, so it is not on the create path.
- `provider_rebind_service.py:328` — same seam, same gate; not reached for kiro.
- `terminal_service.py:1193-1238, 2420-2463, 2989-3078` — all keyed on `resume_uuid`
  (the fork-context id), untouched by this change.
- `kiro_cli.py:594` lock branch — fixed as above; previously a fork (non-resume) context
  could have printed `'None'` into the error.
- `providers/manager.py`, `claude_code.py`, `grok_cli.py` — separate providers, untouched.

`providers/kiro_capabilities.py` needed no change: `build_kiro_command` already omits the
flag when `resume_session_id is None` (L423-424).

## Tests

New / rewritten in `test/providers/test_kiro_cli_unit.py::TestKiroCliSessionResumeMint`:
- `test_fresh_spawn_session_id_is_unknown_not_minted` — accessor returns None (replaces
  `test_fresh_spawn_mints_sess_prefixed_uuid` and `test_two_providers_mint_distinct_ids`,
  both of which asserted the defect).
- `test_fresh_spawn_resume_session_id_is_none`
- `test_resume_session_id_is_the_real_prior_id`
- `test_fresh_kas_launch_omits_resume_id` — full argv equality plus an explicit
  `"--resume-id" not in sent`.
- `test_capture_session_uuid_returns_known_id_without_subprocess` (resume path)
- `test_capture_session_uuid_falls_back_to_list_sessions_when_unknown` (fresh path)
- `test_fresh_spawn_timeout_is_not_reported_as_session_locked` — covers the lock-branch
  gate change.
- `test_resume_launch_reuses_prior_id_in_command` and
  `test_resume_session_locked_raises_clear_error` kept as-is (still green).

Eight fresh-spawn argv expectations elsewhere in that file (init success, shell timeout,
yolo legacy-ui, profile model, legacy-ui fallback ×2, etc.) had
`--resume-id {provider.allocated_session_uuid}` removed from the expected command string.

`test/providers/test_kiro_capabilities.py::test_build_kiro_command_omits_resume_id_when_none`
— docstring now names this as the fresh-spawn shape, plus an added tail assertion that
`--agent developer` is still the last pair.

### Red without the fix

With the tests in place and `src/.../kiro_cli.py` restored to `main`:

```
$ uv run pytest test/providers/test_kiro_cli_unit.py -q -k "fresh_spawn or fresh_kas or list_sessions_when_unknown"
FAILED ...::test_fresh_spawn_session_id_is_unknown_not_minted
FAILED ...::test_capture_session_uuid_falls_back_to_list_sessions_when_unknown
FAILED ...::test_fresh_spawn_resume_session_id_is_none
FAILED ...::test_fresh_kas_launch_omits_resume_id
FAILED ...::test_fresh_spawn_timeout_is_not_reported_as_session_locked
5 failed, 1 passed in 2.09s
```

The last one's failure line is the pre-fix lock branch firing on a non-resume context:
`RuntimeError: E-KIRO-SESSION-LOCKED: kiro session 'sess_f6d07267-e751-4ed5-bab8-4ce39849d71f' is still open in another process` — a minted id reported as locked.

(Note: the restore was done with `git show main:<path> >` because the F142 hook blocks
`git checkout --` in this candidate; the fix was re-applied afterwards and re-verified.)

### Green with the fix

```
$ cd /data/cao-scratch/wt-f566 && uv run pytest test/providers/test_kiro_cli_unit.py test/providers/test_kiro_capabilities.py -q
197 passed in 2.60s     exit code 0
```

No full suite, no `-n` override. Log: `/data/cao-scratch/f566-targeted.log`.

## e2e assertion weakened (with why)

`test/e2e/test_kiro_session_resume.py` — renamed
`test_kiro_mint_and_resume_round_trip` → `test_kiro_fresh_spawn_then_resume_round_trip`.

Dropped: `minted = p1.allocated_session_uuid; assert minted.startswith("sess_")` and the
post-init assertion that `--list-sessions` contains the minted id. Both encoded the
defect — CAO can no longer choose the id, so there is nothing to assert adoption of.

Replaced with: `assert p1.allocated_session_uuid is None` and
`assert p1._resume_session_id() is None` before init, then a `--list-sessions` diff taken
before spawn and after the first turn, requiring **exactly one** new `sess_*` id, which
feeds the resume leg. Steps 3-4 (kill the window, resume that id, assert ZQX7 recall) are
unchanged and are the real coverage.

**What this no longer covers, honestly:** the round-trip now harvests the id inside the
test. Production does not harvest it — `terminals.provider_session_id` stays NULL for a
fresh kiro worker, so F444 hibernate/wake cannot yet find an id to resume. That is
**#416 pt2**, called out in the module docstring, the class comment, and this report.
The e2e is unrun here (marked `e2e` + gated on `CAO_F560_E2E`, needs a real authenticated
kiro-cli); it was edited for correctness, not executed.

## Lint / typecheck

- `uv run black --check` on the four touched files — clean (one file, the unit tests, was
  reformatted by black first, then re-checked).
- `uv run isort --check` on the four touched files — clean.
- `uv run mypy --strict src/.../providers/kiro_cli.py src/.../providers/kiro_capabilities.py`
  → 1 error, `kiro_cli.py:228: Missing type parameters for generic type "list"`. **Pre-existing**,
  not from this change: it is the `allowed_tools: Optional[list] = None` ctor parameter,
  present on `main` at line 219 (verified with `git show main:`). Left alone — out of scope
  for a P0 hotfix.
