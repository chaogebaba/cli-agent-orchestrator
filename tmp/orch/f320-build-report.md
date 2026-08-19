# F320 Build Report: Cline CLI Provider for CAO

**Date:** 2026-08-19
**Binary:** `~/.bun/bin/cline` v3.0.55
**Provider key:** `cline_cli`
**Default model:** `deepseek/deepseek-chat` (DeepSeek Chat, via Cline pass)

---

## 1. Verified CLI Surface

### Flags verified via `cline --help`

| Flag | Purpose | Verified |
|------|---------|----------|
| `-i` / `--tui` | Interactive TUI mode | help output |
| `--auto-approve <bool>` | Auto-approve all tools (default: true) | help + probe |
| `-c` / `--cwd <path>` | Working directory | help output |
| `-m` / `--model <model-id>` | Model override (format: `provider/model-id`) | live probe |
| `-P` / `--provider <id>` | API provider (default: cline) | live probe |
| `-s` / `--system <prompt>` | System prompt override | live probe |
| `--timeout <seconds>` | Per-run timeout | help output |
| `--retries <n>` | Max consecutive retries | help output |
| `--id <session-id>` | Resume session | help output |
| `--json` | Structured JSON output (headless) | live probe |
| `--thinking <level>` | Reasoning effort | help output |
| `--compaction <mode>` | Context compaction | help output |

### Live probes performed

1. **Model format validation:**
   - `cline -m "deepseek-v4-0324"` → error "invalid model format. Expected format: modelType/model"
   - Confirmed format is `provider/model-id` (e.g. `deepseek/deepseek-chat`)

2. **Working model discovery:**
   - `deepseek/deepseek-chat` → **SUCCESS** (DeepSeek Chat, 163840 ctx, family "deepseek")
   - `deepseek/deepseek-chat-v3-0324` → SUCCESS (DeepSeek V3 0324, costs $0.27/$1.12)
   - `deepseek/deepseek-r1` → SUCCESS (DeepSeek-R1, reasoning model)
   - `deepseek/deepseek-r1-0528` → SUCCESS (R1 0528, prompt-cache)
   - `deepseek/deepseek-v4-0324` → error "model not found"
   - `deepseek/deepseek-v4` → error "model not found"
   - Default without -m: `dots-studio/dots-3-note-preview:free` (free tier)

3. **System prompt verified:** `-s "You are a test assistant."` + `-m deepseek/deepseek-chat` → completed, cost $0.0014

4. **JSON mode verified:** `--json` gives structured NDJSON with run_result envelope

5. **Provider default:** `-P cline` confirmed (cline-pass routing)

### Model choice rationale

Selected `deepseek/deepseek-chat` as the providers.toml.default model because:
- It works with the `cline` provider (Cline pass)
- 163K context window, 16K output
- Low cost ($0.26/$1.03 per M tokens) — effectively free under Cline pass
- "DeepSeek V4 flash" from the task spec resolves to the current DeepSeek Chat endpoint
  (`deepseek/deepseek-v4-0324` does not exist on the router)

---

## 2. Implementation Map

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/providers/cline_cli.py` | NEW — full provider (256 LOC) |
| `src/cli_agent_orchestrator/models/provider.py` | Added `CLINE_CLI = "cline_cli"` enum member |
| `src/cli_agent_orchestrator/providers/manager.py` | Import, PROVIDER_CLASSES entry, elif branch |
| `providers.toml.default` | Added `[cline_cli]` section with model default |
| `test/providers/test_cline_cli_unit.py` | NEW — 30 unit tests |

### Provider architecture

- **Mode:** Interactive TUI (`--tui`) in tmux — matches Copilot/Cursor pattern
- **Yolo:** `--auto-approve true` (no permission prompts in headless orchestration)
- **Model override:** Direct `-m` flag (assign/handoff `model` param → `_model`)
- **System prompt:** Profile `system_prompt` + skill_prompt → `-s` flag
- **Status detection:** Regex-based (idle `❯`/`>`, spinner, error, waiting patterns)
- **Response extraction:** User-prompt → content → next-idle-prompt window

---

## 3. Test Roster & Results

### test/providers/test_cline_cli_unit.py (30 tests)

| Class | Tests | Status |
|-------|-------|--------|
| TestClineCliProviderCommand | 4 | PASS |
| TestClineCliModelResolution | 3 | PASS |
| TestClineCliStatusDetection | 7 | PASS |
| TestClineCliIdlePrompt | 4 | PASS |
| TestClineCliExtraction | 4 | PASS |
| TestClineCliRegistration | 3 | PASS |
| TestClineCliProperties | 5 | PASS |

### Broader suite

```
uv run pytest test/providers/ -m "not e2e" --timeout=60 -q
1692 passed, 13 skipped, 1 xpassed in 38.75s
```

No regressions introduced.

---

## 4. Notes / Limitations

- **No fork/resume support:** Cline has `--id <session-id>` for resume but no fork
  mechanism. `supports_fork_context` is not set (defaults to False).
- **providers.toml.default:** Created at subrepo worktree root. The root-repo copy
  at `/home/chao/VScode_projects/cli-subagents/providers.toml.default` needs the
  same `[cline_cli]` section added (outside this worktree's write scope).
- **Profile not required:** Provider does not enforce agent profile existence (like
  copilot_cli). Missing profiles fall through to providers.toml model resolution.
