Artifact-Path: tmp/orch/f277-build-r1.md
Artifact-SHA256: (final)
Artifact-Repo-Path: tmp/orch/f277-build-r1.md
Git-SHA: (final — see commit)
Branch: cao/f277
Base: 07ed30de
Bug: F277
Author: kiro_dev-c158af9f

---

# F277 Build Report: Pin Background Wiki LLM Calls to Cheap Tier

## Summary

Pinned all three CLI backends in `wiki_compiler.py` to `--model claude-sonnet-5` so that
background wiki operations (compile, find_related, contradiction) hit the free tier
instead of the subscription's default `claude-opus-5`.

**Deployment note**: memory.enabled=false is now active (live mitigation), stopping
phantom traffic at the source. This pin is defense-in-depth for re-enablement.

**Routing correction**: the tier reroute (sonnet→DeepSeek free) happens in the **CPA
layer** (Claude-model route: Myflow→dario→CPA→CC-switch), not dario itself. Dario is
passthrough (`model: null`, no aliases) — it forwards whatever model the client requests
unchanged. The CPA layer downstream performs the actual model-tier switch.

## Changes

### `src/cli_agent_orchestrator/services/wiki_compiler.py`

- Added module-level constant `_F277_BACKGROUND_MODEL = "claude-sonnet-5"` with F277 comment.
- **claude_code** backend: `build_argv` now includes `"--model", _F277_BACKGROUND_MODEL`.
- **codex** backend: `build_argv` now includes `"--model", _F277_BACKGROUND_MODEL`.
- **kiro_cli** backend: `build_argv` now includes `"--model", _F277_BACKGROUND_MODEL`.

### `test/services/test_wiki_compiler.py`

- Added `TestF277BackgroundModelPin` class with 4 assertions:
  - `test_claude_code_argv_contains_model_flag`
  - `test_codex_argv_contains_model_flag`
  - `test_kiro_cli_argv_contains_model_flag`
  - `test_background_model_constant_is_sonnet`

## Backend Model-Flag Support

| Backend | Flag | Verified |
|---------|------|----------|
| claude_code (`claude`) | `--model <MODEL>` | Yes — `claude --help` shows it |
| codex | `--model <MODEL>` / `-m <MODEL>` | Yes — `codex --help` shows it |
| kiro_cli (`kiro-cli`) | `--model <MODEL>` | Yes — `kiro-cli chat --help` shows it |

All three backends pinned.

## Triplet "OK" Source Identification (Task 3 — definitive)

**Caller**: `wiki_compiler.compile()` at `src/cli_agent_orchestrator/services/wiki_compiler.py:590`

**Full call path** (file:line):
```
memory_service.MemoryService.store()
  → is_update=True, get_compile_mode()=="llm"
  → _schedule_background_compile()           [memory_service.py:888]
      → _run_background_compile()            [memory_service.py:1064]
          → wiki_compiler.compile()          [wiki_compiler.py:514]
              → _llm_call(client, system, user, timeout_s=...)  [wiki_compiler.py:590]
                  → _default_llm_call()      [wiki_compiler.py:432]
                      → client.build_argv()  [wiki_compiler.py:453]
                      → asyncio.create_subprocess_exec(*argv)   [wiki_compiler.py:458]
```

**Why "OK" (4 tokens)**: The compile prompt passes an existing article + a new observation
and instructs "Return the updated article only." When the model judges the article
already incorporates the new fact (or the observation is trivial), it responds with just
"OK" instead of the full article. Output validation rule 3 (`wiki_compiler.py:232` —
first line must match expected header) catches this and triggers the append fallback.
The subscription tokens are consumed for zero benefit.

**Why 3 per cluster**: Each supervisor turn stores 2-3 memories (updates to existing
topics). Each update with `is_update=True` fires one `_schedule_background_compile()`.
Per-topic debounce (`_compile_inflight`) prevents duplicates on the SAME key, but
different keys run concurrently → 3 parallel compile invocations → 3 opus-5 hits.

**Why find_related doesn't fire**: When compile's output validation rejects "OK",
`result.used_llm` is effectively False (the fallback path), so the `find_related()`
call at `memory_service.py:1129` is never reached for these instances.

**NOT the contradiction checker**: The singleton (in=26, out=18) is `wiki_lint._check_pair()`
at `wiki_lint.py:394`, which runs on a separate schedule (lint pass after article writes).
It has a different token profile because its prompt is tiny (just two short filler articles).

**All callers flow through** `wiki_compiler._CLI_BACKENDS[*].build_argv` → `_default_llm_call`
→ `asyncio.create_subprocess_exec(*argv, ...)`, so the `--model` pin in `build_argv`
covers every background LLM invocation.

## Test Results

```
44 passed in 0.81s (test/services/test_wiki_compiler.py, -n 0)
```

No full-suite run requested (queue: F262-held, F261, F273 ahead).
