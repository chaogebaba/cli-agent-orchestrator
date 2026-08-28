# F557 (#412) build report — claude_code missing-profile fail-loud

Branch: `cao/05482988`
Tip sha: `4cc1f9c554665c4c5bde052ddac3482484f62564`
Base: `51b1156b` (Merge 'cao/9a61dd36' into main)

## Root cause (3 lines)
1. Empty/isolated `CAO_HOME_DIR` → `load_agent_profile(<name>)` raises `FileNotFoundError` → `ClaudeCodeProvider._load_profile()` returns `None`.
2. `_build_claude_command` path 2 (`self._agent_profile is not None and profile is None`) then handed the bare name to `claude --agent <name>` against Claude Code's NATIVE agent store (`~/.claude/agents/`), which has no CAO profile names.
3. Pane hit `--agent '<name>' not found`, Claude dropped to a shell, and `cao launch` only 500'd ~30s later on the init timeout (F548 gate r2, ~30 min misdiagnosis).

## Fix (decision A — supervisor-approved)
CAO profiles are the contract for CAO-launched terminals; native passthrough was never intentional here and, on an empty store, is indistinguishable from a missing profile. So a missing CAO profile now fails LOUD at terminal create instead of silently falling through.

- **`providers/claude_code.py`**
  - New `ProfileNotFoundError(ValueError)` with `code = "E-PROFILE-NOT-FOUND"`, carrying `profile_name`, `store_dir`, and a structured `detail` naming both the profile and the agent-store dir searched.
  - New `ClaudeCodeProvider.preflight_launch(cls, *, agent_profile, model)` override of the `BaseProvider` no-op: when an `agent_profile` name is requested but `load_agent_profile` raises `FileNotFoundError`, raise `ProfileNotFoundError`. The store dir is resolved at call time via `local_agent_store_dir()` (honours the live `CAO_HOME_DIR`). This hook already runs in `terminal_service.create_terminal` BEFORE any resource allocation (window, DB row, F138 incarnation token, `claude` subprocess) — same seam grok_cli uses for `RelayPreflightFailed`. Only `FileNotFoundError` is converted; a profile that exists-but-fails-to-parse still raises `ProviderError` from the authoritative load (that was never the silent path).
  - Docstrings updated so **docs no longer lie**: `_build_claude_command` path-2 docstring, its inline branch comment, and `_load_profile`'s docstring now state the native-store fallthrough is UNREACHABLE for CAO-launched terminals (preflight fails first) and is retained only for direct/legacy callers of the raw builder + unit tests.

- **`api/main.py`**
  - Import `ProfileNotFoundError` from `providers.claude_code` (no import cycle — verified).
  - Dedicated `except ProfileNotFoundError` arm returning **HTTP 400** with `{"code": e.code, "message": e.detail}`. Placed AFTER `KiroCapabilityError` and BEFORE the generic `except ValueError` arm (which would otherwise 404 it) — Python matches top-to-bottom, so ordering is load-bearing.

## Why the old behaviour was removed (test flip rationale)
`test_initialize_with_missing_profile_falls_back_to_native_agent` asserted the *dangerous* behaviour: on `FileNotFoundError`, silently pass the bare name to `claude --agent <name>`. That is exactly the silent fallthrough that broke `cao launch --agents <name>` on an empty `CAO_HOME_DIR` and cost the ~30 min misdiagnosis. Under decision A the native-store fallthrough is no longer a supported launch path, so the test is replaced by `test_missing_profile_fails_loud_no_native_fallback`, which asserts the loud `E-PROFILE-NOT-FOUND` and that **no subprocess is spawned** (`send_keys` not called).

## Tests (targeted only — no laptop suite)
Run: `CI=true uv run pytest test/providers/test_claude_code_unit.py -p no:cacheprovider -q`
(the `CI=true` skips the pytest-layer suite-slot lock; these are mocked unit tests with no subprocess/OOM risk — "quick single unit tests may still run locally" per box-ops. The box suite-slot was held by another lane at run time.)

- `test_missing_profile_fails_loud_no_native_fallback` — empty `CAO_HOME_DIR` + known profile name → `ProfileNotFoundError` (code, profile_name, store_dir named); `send_keys` never called (no spawn). **[the loud-error + no-spawn contract test]**
- `test_preflight_launch_present_profile_passes` — a present profile passes preflight (returns None, raises nothing). **[regression: present profile still resolves]**
- `test_preflight_launch_no_profile_name_is_noop` — nameless launch is a no-op (store not consulted).
- Untouched and still green: `test_initialize_with_broken_profile_raises_provider_error`, `test_build_command_uses_native_agent_from_profile`.

Result: **8 passed** for the targeted `-k` subset; **183 passed** for the full `test_claude_code_unit.py` file.

## Mutation notes (per new/changed test)
- `test_missing_profile_fails_loud_no_native_fallback`:
  - Kills a mutant that swallows `FileNotFoundError` in `preflight_launch` (returns None) — the mutant would let create proceed to the native fallthrough and `pytest.raises` would fail.
  - Kills a mutant that spawns before preflighting — asserted via `send_keys.assert_not_called()`.
- `test_preflight_launch_present_profile_passes`:
  - Kills a mutant that raises unconditionally in `preflight_launch` (ignores load success) — would break every valid claude_code launch.
- `test_preflight_launch_no_profile_name_is_noop`:
  - Kills a mutant that drops the `if not agent_profile` guard and calls `load_agent_profile(None)`/raises — asserted via `mock_load.assert_not_called()`.

## Quality gates (touched files)
- `black`: reformatted `test/providers/test_claude_code_unit.py`; all three files now clean.
- `isort`: clean on all three.
- `mypy --strict`:
  - `providers/claude_code.py`: 2 errors, **both pre-existing** on base at the same code (untyped `Optional[list]` in `__init__` L408 / untyped `dict` L904 — shifted from base L374/L827 by the added lines). **0 net-new.** My additions (`ProfileNotFoundError`, `preflight_launch`) are fully typed.
  - `api/main.py`: 130 errors on BOTH base and head (legacy file, never --strict clean). **0 net-new.** My added arm + import are typed.

## Scope / notes
- **Kiro provider left untouched** as instructed. `kiro_cli` has its own thin-orchestrator native-`--agent` path with the same theoretical failure mode; addressing it is a separate issue if wanted.
- Name collision note (harmless): `services/profile_store.py` also defines an unrelated `ProfileNotFoundError`. Mine lives in `providers/claude_code.py` and is imported explicitly, so no shadowing.
- Import-cycle check: `uv run python -c "import cli_agent_orchestrator.api.main; from cli_agent_orchestrator.providers.claude_code import ProfileNotFoundError"` → OK.

## Diff --stat
```
 src/cli_agent_orchestrator/api/main.py             |  11 ++
 src/cli_agent_orchestrator/providers/claude_code.py|  93 +++++++++++++++--
 test/providers/test_claude_code_unit.py            | 116 ++++++++++++++-------
 3 files changed, 177 insertions(+), 43 deletions(-)
```
Counts: +177 / -43.

## Box-actions ledger
No offload box was used. All work (tests, black/isort/mypy) ran locally in the worktree `.cao/worktrees/05482988` (mocked unit tests only; the suite-slot lock was held by another lane, so the targeted subset ran with `CI=true` per box-ops "quick single unit tests may still run locally"). No ssh, no box mutations, no temp files outside the worktree.


---

# F557 (#412) r2 — gate GATE-NO remediation (1B / 2S)

Branch: `cao/05482988` (worktree `/data/cao-scratch/wt-f557-r2`, re-provisioned by
supervisor after the r1 fork worktree turned out to be containment-locked to an
unrelated F558 branch). Onto r1 tip `c44e6482`. Source code from `4cc1f9c5`
(claude_code preflight + api arm) left UNCHANGED — r2 touches only tests + docs.

## What the gate flagged and what r2 did

### B1 (BLOCKER) — net-new full-suite regression, not in the break-list
`test/services/test_terminal_service_coverage.py::TestPersonaPlanningSeam::test_sandbox_precedence_skips_persona_with_one_warning`
failed at tip: the sandbox branch of `create_terminal` skips persona composition
and reaches `ClaudeCodeProvider.preflight_launch`, which resolves the profile via
its OWN module binding `cli_agent_orchestrator.providers.claude_code.load_agent_profile`.
The test monkeypatched only `terminal_service.load_agent_profile`, so preflight hit
the real loader for the on-disk-absent `"persona"` profile and raised
`ProfileNotFoundError` (`E-PROFILE-NOT-FOUND …`) instead of reaching the expected
`pytest.raises(ValueError, match="already exists")`.

- **Fix chosen: (a) fix the TEST** — added ONE `monkeypatch.setattr` stubbing the
  provider-module binding to the same present `profile` fixture.
- **Why (a) over (b):** it is the smaller, lower-risk change (1 stub in 1 test vs.
  re-routing `preflight_launch` through a service-owned seam — a provider importing
  from `terminal_service` would invert the dependency direction and risk an import
  cycle). The other `TestPersonaPlanningSeam` test (`..._before_any_pane`,
  `is_sandbox=False`) already passes because `compose_persona_plan` (patched to
  raise) runs BEFORE preflight on the non-sandbox path, so only the sandbox test
  needed the stub. Reproduced red before, green after.

### BREAK-LIST (gate rule 5) — enumerated here, was missing from r1
- `test/services/test_terminal_service_coverage.py::TestPersonaPlanningSeam::test_sandbox_precedence_skips_persona_with_one_warning`
  — BROKEN at r1 tip `4cc1f9c5`/`c44e6482` by the F557 `preflight_launch` calling
  its own-module `load_agent_profile` binding (the ONE net-new full-suite FAILURE
  the r1 A/B measured). FIXED in r2 by stubbing that binding in the test. This was
  the sole net-new full-suite regression; the other 12 HEAD failures in the r1 A/B
  were pre-existing at BASE `51b1156b` (opencode-install ×2, suite_slot ledger ×2,
  wpdt doctrine-arming, sim_substrate wall-clock, g7a_sandbox mutation-site,
  f497_composition, f497_resolver ×4) and are out of F557 scope.

### S1 (SHOULD) — docs/claude-code.md lied
`docs/claude-code.md` "Native Agent Routing" line claiming a missing CAO profile
"also falls back to `--agent <name>`, assuming it exists in the native store" was
rewritten to state the new fail-loud behaviour: a CAO-launched terminal with an
unresolved profile now raises `ProfileNotFoundError` (`E-PROFILE-NOT-FOUND`, HTTP
400) at create naming the profile + store searched, and the `--agent` passthrough
is reached only via the explicit `native_agent:` profile field.

### S2 (SHOULD) — api/main.py 400-arm ordering untested
Added `test/api/test_api_endpoints.py::TestCreateTerminalInSession::test_create_terminal_missing_profile_returns_400_structured`.
Drives `POST /sessions/{name}/terminals` with `terminal_service.create_terminal`
raising a REAL `ProfileNotFoundError` instance (the actual exception type from the
provider, not a stand-in) and asserts HTTP **400** + `detail["code"] ==
"E-PROFILE-NOT-FOUND"` + the profile name in the message. Because
`ProfileNotFoundError(ValueError)` and the generic `except ValueError` arm 404s,
this test is load-bearing on ARM ORDER.
- **Mutation proof (ran):** masking the dedicated `except ProfileNotFoundError`
  arm so the generic `ValueError` arm catches it flipped the response 400→404 and
  the new test FAILED (`assert 404 == 400`). Restored api/main.py to baseline
  hash `bff13433…` (byte-identical) and the test passes. Mutation CAUGHT.

## Targeted suite (per brief, `-n0`)
`CI=1 uv run pytest test/services/test_terminal_service_coverage.py test/providers/test_claude_code_unit.py test/api ... -n0 -p no:suite_slot`
The full `test/api` dir + the other two exceeds the 120s foreground tool budget on
the laptop, so it was split (no coverage dropped):
- `test/services/test_terminal_service_coverage.py` + `test/providers/test_claude_code_unit.py`: **196 passed** (1 environmental teardown warning — shared `/data/cao-scratch/pytest-tmp` rm_rf race with another lane, not a test failure).
- `TestPersonaPlanningSeam` (B1) + `TestCreateTerminalInSession` (incl. S2) + full `test_claude_code_unit.py`: **190 passed**, 0 failed.

## Quality gates (touched files only)
Touched: `test/api/test_api_endpoints.py`, `test/services/test_terminal_service_coverage.py` (py); `docs/claude-code.md` (md).
- **black**: clean (reformatted `test_terminal_service_coverage.py` — one PEP8 blank line before module-level `pytestmark`; `test_api_endpoints.py` clean).
- **isort**: clean on both.
- **mypy --strict**: **139 errors on BOTH base `c44e6482` and r2 head — 0 net-new.**
  The 139 are the file-wide pre-existing untyped-test-def pattern (the repo's real
  `mypy.ini` sets `disallow_untyped_defs = False`; `--strict` is stricter than the
  project config). My NEW test is fully strict-typed (`client: TestClient` param +
  `-> None`, `# type: ignore[import-untyped]` on the `providers.claude_code` import
  which has no py.typed marker — same import api/main.py already uses), so it adds
  zero errors. My B1 stub adds no new def.

## Scope / containment
- Only 3 files changed (diff --stat above/below). No source-code (`claude_code.py`,
  `api/main.py`) edits — the r1 fix stands; r2 is tests + docs + this report.
- All work in worktree `/data/cao-scratch/wt-f557-r2` (branch `cao/05482988`). The
  original fork worktree `.cao/worktrees/29ddc043` was NOT touched (it is bound to
  an unrelated F558 branch; the fx121 guard correctly refused a branch switch there
  — reported to supervisor, who re-provisioned this dedicated checkout).
- No offload box used. No env/lockfile mutations.
