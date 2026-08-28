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
