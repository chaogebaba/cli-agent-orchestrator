# F571 build report — issue #428

**Problem.** The `CAO_MCP_APPS_ENABLED is set but no IdP is configured` warning
(`McpAppsPlugin.on_mcp_server`, `plugins/builtin/mcp_apps.py`) fired on **every**
`cao` CLI invocation. Root cause: `cli/main.py` imports `cli/commands/mcp_server.py`
at module top to build the command tree, and that module did a **top-level**
`from cli_agent_orchestrator.mcp_server.server import main`. Importing
`mcp_server/server.py` runs `register_mcp_server_surfaces(mcp)` at module scope
(server.py:4577), which dispatches `on_mcp_server` → emits the warning. So any CLI
path (`cao --version`, `cao install`, ...) paid MCP startup and printed the warning.
`install.sh` calls `cao install` ~25× → ~25 warnings.

## Fix (minimal, no plugin-registry refactor)

Lazy-import the server module inside the `mcp-server` command body so building the
CLI command tree no longer imports `mcp_server.server`. Only actually starting the
server (the `cao-mcp-server` entrypoint = `mcp_server.server:main`, or `cao mcp-server`)
now mounts the surface and emits the warning — exactly once per process (Python
caches the module import, so `register_mcp_server_surfaces` runs once).

The warning check itself was left untouched: it already lives on the mount path
(`on_mcp_server`, gated on `_surface_enabled() and not is_auth_enabled()`), and the
mount runs once per process. The only defect was the CLI dragging that mount in at
import time — fixed by the lazy import.

### Files + lines

- `src/cli_agent_orchestrator/cli/commands/mcp_server.py` (12 → 22 lines)
  - Removed top-level `from ...mcp_server.server import main`.
  - Moved the import inside `mcp_server()`; added the issue-#428 rationale comment.
  - Added `-> None` return annotation and a local `Callable[[], None]` binding so the
    file passes `mypy --strict` (the source module `main()` is untyped).
- `test/cli/commands/test_mcp_server.py` (13 → 78 lines)
  - Updated existing `test_mcp_server_command` to patch the server main at its source
    (`cli_agent_orchestrator.mcp_server.server.main`) since it is now lazily imported.
  - Added `test_cli_version_emits_no_apps_warning_when_enabled`: subprocess runs
    `python -m cli_agent_orchestrator.cli.main --version` with `CAO_MCP_APPS_ENABLED=true`,
    no IdP → asserts **0** warnings on stderr.
  - Added `test_server_surface_mount_emits_exactly_one_apps_warning`: subprocess imports
    `mcp_server.server` with `CAO_MCP_APPS_ENABLED=true`, no IdP → asserts **exactly 1**.
  - Subprocesses are used deliberately: the surface registers at module import, so once
    any in-process test imports `mcp_server.server` the module is cached and the
    import-time warning cannot re-fire; warning counts are only observable in a clean
    interpreter.

`git diff --stat`: 2 files changed, 76 insertions(+), 7 deletions(-).

## Before / after warning counts

All commands run from the worktree venv
(`/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/bdaa13d1/.venv`,
editable install of the worktree source). `CAO_MCP_APPS_ENABLED=true`, `AUTH0_DOMAIN` /
`CAO_AUTH_JWKS_URI` unset. Count = occurrences of the warning string on stderr.

| Command (exact) | Before | After | exit |
|---|---|---|---|
| `CAO_MCP_APPS_ENABLED=true .venv/bin/cao --version` | 1 | **0** | 0 |
| `CAO_MCP_APPS_ENABLED=true HOME=<isolated> .venv/bin/cao install developer --provider mock_cli` | 1 | **0** | 0 |
| `CAO_MCP_APPS_ENABLED=true .venv/bin/python -c "import cli_agent_orchestrator.mcp_server.server"` (server surface mount) | 1 | **1** | 0 |

`cao install` was run under an isolated `$HOME` under `/data/cao-scratch/f571/` so it
never touched the user's real `~/.aws/cli-agent-orchestrator` config.

## Tests / lint / types (targeted, worktree venv)

- `uv run pytest test/cli/commands/test_mcp_server.py test/plugins/builtin/test_mcp_apps.py -q`
  → **10 passed** (3 in test_mcp_server.py incl. 2 new subprocess tests; 7 in
  test_mcp_apps.py, unchanged & still green), exit 0.
- `uv run black --line-length 100 --check` (both touched files) → all done, exit 0.
- `uv run isort --line-length 100 --profile black --check-only` (both) → exit 0.
- `uv run mypy --strict src/.../cli/commands/mcp_server.py test/.../test_mcp_server.py`
  → `Success: no issues found in 2 source files`, exit 0.

## Scope / containment

- No plugin-registry refactor; the warning emitter and its guard are unchanged.
- Diff limited to the CLI command module and its test.
- Scratch under `/data/cao-scratch/f571/` (never `/tmp`); no recursive grep of `~/`.
- Worktree branch only; **not merged**.
