Artifact-Path: tmp/orch/f277-build-r1.md
Artifact-SHA256: b9970fd9194fba4ed0c46c38d6dcf2ec17e414de5afd4b04f66572fcaf6a0780
Artifact-Repo-Path: tmp/orch/f277-build-r1.md
Git-SHA: b0f1fb7f0367c2afd439fff83ce11c194aa6809b
Branch: cao/f277
Base: 07ed30de
Bug: F277
Author: kiro_dev-c158af9f

---

# F277 Build Report: Pin Background Wiki LLM Calls to Cheap Tier

## Summary

Pinned all three CLI backends in `wiki_compiler.py` to `--model claude-sonnet-5` so that
background wiki operations (compile, find_related, contradiction) hit dario's free DeepSeek
tier instead of the subscription's default `claude-opus-5`.

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

## Triplet "OK" Source Identification

**Caller**: `wiki_compiler.compile()` and `wiki_compiler.find_related()`, invoked by
`memory_service.MemoryService._run_background_compile()` (file: `src/cli_agent_orchestrator/services/memory_service.py:1064-1145`).

**Call graph**:
```
memory_store()
  → _schedule_background_compile()  [memory_service.py:984]
      → _run_background_compile()   [memory_service.py:1064]
          → wiki_compiler.compile()  [wiki_compiler.py:514, _llm_call at :590]
          → wiki_compiler.find_related()  [wiki_compiler.py:700, _llm_call at :779]
```

Each `memory_store` that updates an existing topic fires ONE background compile task.
If the supervisor stores 3 memories per turn (typical), that's 3 compile calls
(+ up to 3 find_related calls after each compile succeeds), explaining the triplet.

The "OK" output (4 tokens) is the model declining to rewrite an already-clean article —
it replies "OK" instead of the full updated article. The compile output validator
(rule 3: first line must match expected header) catches this and falls back to the
append path. The subscription tokens are still consumed even though the result is discarded.

The singleton `{"contradicts": false, "summary": ""}` comes from
`wiki_lint._detect_contradictions()` → `_check_pair()` (file: `src/cli_agent_orchestrator/services/wiki_lint.py:371-394`), triggered by the lint pass that runs on a separate
schedule after article writes.

**All callers flow through** `wiki_compiler._CLI_BACKENDS[*].build_argv` → `_default_llm_call`
→ `asyncio.create_subprocess_exec(*argv, ...)`, so the `--model` pin in `build_argv`
covers every background LLM invocation.

## Test Results

```
44 passed in 0.81s (test/services/test_wiki_compiler.py, -n 0)
```

No full-suite run requested (queue: F262-held, F261, F273 ahead).
