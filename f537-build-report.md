# F537 (#393) build report — cline workers never attach cao-mcp-server

## Branch / commit
- Branch: `cao/8fc54014`
- Commit: `b8e56eeb3a25633dc36f3a6582ae275462626feb`
- Worktree: `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/8fc54014`

## Root cause
cline hard-times-out the MCP `initialize` handshake at its built-in 3 s default
and SKIPS any server that hasn't finished initializing, so cline workers never
attached cao-mcp-server. cao-mcp-server startup (Python import + HTTP round-trip
to the CAO API) exceeds 3 s under concurrent worker load. cline's log says to
set the per-server `"timeout"` field (units: **seconds**) in
`cline_mcp_settings.json`. The materializer `_materialize_mcp_settings` wrote no
`timeout` key.

## Files touched
- `src/cli_agent_orchestrator/providers/cline_cli.py`
- `test/providers/test_cline_sandbox.py`

## Diff summary
- **cline_cli.py**
  - Added module constant `CLINE_MCP_INIT_TIMEOUT_S = 60` (seconds) and floor
    constant `_CLINE_MCP_INIT_TIMEOUT_FLOOR_S = 30`.
  - Added `_resolve_cline_mcp_init_timeout_s() -> int`: reads
    `[cline_cli] mcp_init_timeout_s` from providers.toml via the existing
    `get_provider_defaults("cline_cli")` knob (the same one the provider already
    uses for `model`/`thinking`/`api_provider`), accepts int or digit-string,
    rejects bool/float/garbage (falls back to the constant), and floors the
    result at 30 s so an operator can't re-open the F537 race with a tiny value.
  - `_materialize_mcp_settings` now writes `"timeout": _resolve_cline_mcp_init_timeout_s()`
    on the `cao-mcp-server` entry in `cline_mcp_settings.json`.
  - Docstrings updated (function + top-of-file MCP note).
  - F345's `MCP_CONNECT_TIMEOUT_MS` env export left exactly as-is.
- **test_cline_sandbox.py**
  - Extended `TestAC8_MaterializedMCPSettings` with
    `test_mcp_settings_includes_init_timeout`: asserts the `timeout` key is
    present, is an `int` (not bool), and is `>= 30` (seconds).
  - Added `TestF537_MCPInitTimeoutResolution`: default is the module constant
    (60), providers.toml int override, providers.toml digit-string override,
    below-floor override is clamped to 30, and invalid overrides
    (`"abc"`, `True`, `1.5`, `None`) fall back to 60.

Stat: `2 files changed, 189 insertions(+), 47 deletions(-)`.

## Tests
- Command: `uv run pytest test/providers/test_cline_cli_unit.py test/providers/test_cline_f343_busy_gate.py test/providers/test_cline_sandbox.py test/providers/test_cline_sandbox_isolation.py -q`
- Result: **88 passed** (all cline provider tests). The targeted materializer
  file (`test_cline_sandbox.py`) alone: **12 passed** post-format.
- No full suite run on the laptop (per instructions).

## mypy --strict (cline_cli.py)
- Before change: **1 error** — `cline_cli.py:180: Missing type parameters for
  generic type "list"` (`allowed_tools: Optional[list]`), pre-existing.
- After change: **1 error** — same error, now at line 220 (shifted by added
  lines). No new errors introduced by this change. Verified by stashing the
  change and re-running mypy on the clean base.

## Formatting
- `black --line-length 100` and `isort --profile black --line-length 100` run on
  both touched files. `test_cline_sandbox.py` reformatted; `cline_cli.py`
  already clean. Tests re-run green after formatting.

## Token decision (part 2)
**No change needed — CAO_TERMINAL_TOKEN handling was already correct.**
- cline already sources the token identically to every other provider:
  `os.environ.get("CAO_TERMINAL_TOKEN", "")`, added to the MCP env block only
  when present (`cline_cli.py:423-425`). This is the exact same conditional
  pattern used by claude_code.py, cursor_cli.py, kimi_cli.py, and omp.py — none
  of them force the token; they all omit it when it's absent.
- The token is baked into the pane env at spawn (`clients/tmux.py:756-758`,
  guarded by `if terminal_token:`), so cline's `os.environ.get` picks it up in
  the same env context as the other providers.
- The token is genuinely optional at the MCP server: `mcp_server/server.py`
  has `_refresh_terminal_token_from_pane()` (F352) which re-reads it from
  `/proc/<ppid>/environ` if it wasn't in the MCP server's env, and the API
  returns a remediation message on a 403 rather than hard-failing. cline is not
  "the one provider that can only omit it" — it behaves exactly like the rest.
- Therefore the token was intentionally left as-is; F537 is purely the missing
  `timeout` key.

## How to live-verify
1. Redeploy (supervisor does this — NOT done here): `./install.sh` etc.
2. Spawn a cline worker (e.g. `assign`/`handoff` to a `cline_*` profile).
3. Check the sandbox settings file has the timeout:
   `cat /data/cao-scratch/cline-home/<terminal_id>/settings/cline_mcp_settings.json`
   → `mcpServers.cao-mcp-server.timeout == 60` (int, seconds).
4. Check the worker's cline log has NO "timed out" / MCP init timeout line:
   `grep -i "timed out" <cline log>` (e.g. under the worker's data-dir/logs) →
   no match for the cao-mcp-server initialize.
5. Confirm the worker lists the MCP tool: it should have
   `mcp__cao-mcp-server__send_message` (and siblings) available, i.e. it can
   call `send_message` back to its supervisor.
