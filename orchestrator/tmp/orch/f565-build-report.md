# F565 (#421) build report — `cao session start --yolo` for phoenix seat parity

**Priority:** P0
**Branch:** `cao/c6c0bf2e`
**Code commit:** `fe2cd98e5e66b2eebbc148c4516f46bb229af36f`

## Root cause (2 lines)

The phoenix self-redeploy verb resolves to `cao session start` (F540), whose `session_start`
branch builds `launch_cmd` **without `--yolo`** → `allowed_tools=None` → terminal_service resolves the
supervisor-role default `ROLE_TOOL_DEFAULTS['supervisor']=['@cao-mcp-server','fs_read','fs_list']` →
`claude_code.py` emits `--disallowedTools Bash/Edit/Write/Agent/...`, stripping the newborn supervisor's
shell/edit access. `cao launch --yolo` never hit this because it sets `allowed_tools=["*"]`, which
suppresses the `--disallowedTools` branch (`claude_code.py`: `if self._allowed_tools and "*" not in self._allowed_tools`).

## Proven trace (verified read-only in this worktree)

- `src/.../providers/claude_code.py` (~L807-812): emits `--disallowedTools` iff resolved `_allowed_tools`
  is non-empty AND lacks `"*"`. Verified empirically: `get_disallowed_tools("claude_code", ["@cao-mcp-server","fs_read","fs_list"])`
  → `['Agent','Bash','BashOutput','Edit','KillShell','Monitor','NotebookEdit','Task','WebFetch','WebSearch','Write']`;
  `get_disallowed_tools("claude_code", ["*"])` → `[]`.
- `cao launch --yolo`: `cli/commands/launch.py` → `if yolo: resolved_allowed_tools = ["*"]` → forwards `allowed_tools="*"`.
- `cao session start`: `cli/commands/session.py` → `allowed_tools` forwarded only if `--tools` given; else `None`.
- `terminal_service.py` (~L1996-2003, non-Kiro path): `if allowed_tools is None and profile is not None: resolve_allowed_tools(profile.allowedTools, profile.role, ...)` → supervisor role default.
- Evidence baseline: `/data/cao-scratch/phoenix-box-e2e-report.md`.

## Fix (smallest change; ROLE_TOOL_DEFAULTS NOT widened — #125 semantics preserved)

`cao session start` gains a `--yolo` flag mirroring `cao launch --yolo`:
- `--yolo` → `allowed_tools = "*"` (forwarded to `/sessions/start` as the `allowed_tools` query param).
- Mutually exclusive with `--tools` (raises a `ClickException`).
- No change to `ROLE_TOOL_DEFAULTS`; a supervisor-role session **without** `--yolo` still restricts.

This makes the phoenix seat identical to `cao launch --agents chao_supervisor --provider claude_code --headless --yolo`:
`allowed_tools=["*"]` → provider keeps `--dangerously-skip-permissions` AND emits no `--disallowedTools`.

### Files changed (diff --stat)
```
 src/cli_agent_orchestrator/cli/commands/session.py | 29 +++++++++--
 test/cli/commands/test_session.py                  | 59 ++++++++++++++++++++++
 test/providers/test_claude_code_unit.py            | 54 ++++++++++++++++++++
 3 files changed, 137 insertions(+), 5 deletions(-)
```

## Tests (targeted; all green)

`test/providers/test_claude_code_unit.py::TestF565PhoenixSeatContract` (argv-level newborn-seat contract — no live server):
1. `test_yolo_seat_bypasses_and_never_disallows_bash` — `allowed_tools=["*"]` → argv has `--dangerously-skip-permissions` and NO `--disallowedTools`. **(Test 1)**
2. `test_supervisor_role_without_yolo_still_disallows_bash` — supervisor-role set (no yolo) → argv HAS `--disallowedTools Bash`. **(Test 2 / #125 regression guard)**

`test/cli/commands/test_session.py::TestStart` (CLI wiring):
- `test_yolo_forwards_wildcard_allowed_tools` — `--yolo` → `allowed_tools="*"`.
- `test_without_yolo_omits_allowed_tools` — no yolo → param absent.
- `test_tools_flag_still_forwarded_verbatim` — `--tools` unaffected.
- `test_yolo_and_tools_are_mutually_exclusive` — conflict rejected.

Counts:
- Targeted new: **6 passed** (`TestStart` 4 + `TestF565PhoenixSeatContract` 2).
- Regression sweep of touched test files: **250 passed** (`test_session.py` + `test_claude_code_unit.py`), no failures.
- `black` (my new code clean), `isort`, `mypy src/.../session.py` → all clean.

### Test 3 mutation note (why the argv contract, not a script e2e)

The brief's Test 3 ("phoenix box e2e assertion: newborn seat argv has no `--disallowedTools Bash`") is
realized as the **argv-construction seat contract** in `TestF565PhoenixSeatContract`, because the F540
phoenix launch verb — and its only e2e/probe — lives in the **root repo** script
`/home/chao/VScode_projects/cli-subagents/scripts/self-redeploy.sh` (`launch_session()`), which is out of
scope to edit. There is NO phoenix/self-redeploy test or probe inside the fork
(`rg 'phoenix|self-redeploy' test/ scripts/` → none). The newborn-seat property the box e2e would assert
(`allowed_tools=["*"]` ⇒ no `--disallowedTools`) is exactly what Test 1 asserts, decoupled from the root
script and runnable in CI without a live server. If/when the root e2e is updated, its assertion should be
"resolved newborn argv contains no `--disallowedTools`".

## Root-side change (REPORT ONLY — root repo NOT edited)

The phoenix verb resolution lives in the **root** script, not the fork:

- File: `/home/chao/VScode_projects/cli-subagents/scripts/self-redeploy.sh`
- Function: `launch_session()` (~L1313), `session_start` branch (~L1340-1342).

Current (strips Bash):
```bash
    if [[ "$launch_verb" == "session_start" ]]; then
        launch_cmd=( cao session start "$old_session"
            --agents chao_supervisor --provider "$launch_provider" --cwd "$REPO_ROOT" )
```

Required one-line change — append `--yolo` to the `session_start` branch's `launch_cmd`:
```bash
    if [[ "$launch_verb" == "session_start" ]]; then
        launch_cmd=( cao session start "$old_session"
            --agents chao_supervisor --provider "$launch_provider" --cwd "$REPO_ROOT" --yolo )
```
(The deprecated `launch` fallback branch already passes `--headless --yolo`, so it is unaffected.)

This fork commit provides the `--yolo` flag the root change depends on; the root edit is the operator's
to apply since it is outside the fork worktree.
