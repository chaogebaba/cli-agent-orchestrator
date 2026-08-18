# F297 Build Report

## Summary
Inject `GROK_CLAUDE_HOOKS_ENABLED=0` into every grok worker's spawn environment to prevent the hooks-wedge issue (grok-build v1.0.5, issue #140/#151).

## Branch & Commit
- **Branch:** `cao/f297`
- **Tip SHA:** `05d1609d7ab9137b67bd6fe0a82fe50ae7856062`
- **Fork repo:** `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator`

## Change
- **File:** `src/cli_agent_orchestrator/providers/grok_cli.py`
- **Location:** `_build_grok_command()` return statement (the single point that assembles the `env ...` command string for all spawn paths including fork and resume)
- **Diff:** Added `GROK_CLAUDE_HOOKS_ENABLED=0` to the `env` prefix alongside `GROK_HOME`

## Tests
- **New test file:** `test/providers/test_f297_grok_hooks_env.py` (7 test cases covering basic spawn, model override, fork, resume, value correctness, build_fork_command, build_resume_command)
- **Test run:** `uv run pytest test/providers/ -n 2` — **1650 passed**, 13 skipped, 1 xpassed in 49.19s
- **No failures in providers scope**

## AC Verification
| AC | Status |
|----|--------|
| AC1: env var present for every grok terminal spawn path | PASS — single return point in `_build_grok_command` covers all paths |
| AC2: unit test asserting spawn env contains `GROK_CLAUDE_HOOKS_ENABLED=0` | PASS — 7 tests in `test_f297_grok_hooks_env.py` |
| AC3: `uv run pytest test/providers/ -n 2` green | PASS — 1650 passed |
| AC4: build report at `orchestrator/tmp/orch/f297-build-report.md` | This file |
