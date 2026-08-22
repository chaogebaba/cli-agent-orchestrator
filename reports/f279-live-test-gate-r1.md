---
Artifact-Path: /home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/25bd0556/reports/f279-live-test-gate-r1.md
Artifact-SHA256: (self)
Artifact-Repo-Path: reports/f279-live-test-gate-r1.md
Git-SHA: 74147da7
---

# F279: Live Test Gate — R1 Report

## Burn families identified

### Family 1: Claude transcript hook live tests (opus-5 API)

The two `@pytest.mark.live` tests in `test/providers/test_claude_transcript_hook.py`
spawn the real `claude` binary with `-p "Reply with exactly OK."` and no `--model`
flag. Without a model pin, this defaults to opus-5 (the subscription tier). Each
parametrized expansion fires a full API request on every suite run.

**Evidence:** dario usage rows correlated with suite runs show two request bursts
(one per live test function, the parametrized test contributing 2x) with no
`CAO_RUN_LIVE_PROVIDER_TESTS` gate — any developer running `make test-quick` or
bare `pytest` triggers them.

### Family 2: Wiki-lint contradiction detector (unstubbed populated_scope fixture)

`test/graph/providers/test_memory_provider.py:168` —
`TestMemoryProviderHappyPath.test_nodes_edges_from_populated_scope`.

The `populated_scope` fixture seeds 3 topics (BODY = "A reasonably long article
body so contradiction pairing engages." + " filler" * 10) and calls
`_patch_lint_env` but never stubs `_build_llm_client`. The test body calls
`MemoryGraphProvider.project()` → `wiki_lint.run_lint()` → `_build_llm_client()`.
On a machine with `claude` on PATH, `_build_llm_client()` returns a real
`_CliBackend` for claude_code → `_check_pair()` → `_llm_call()` fires a real API
call (in=26/out=18 tokens, output `{"contradicts": false, "summary": ""}`).

**Evidence:** dario usage row with exactly in=26/out=18 tokens, matching the
fixture body length and contradiction detector system prompt. The `@pytest.mark.slow`
marker does NOT exclude this test from default runs (pyproject.toml addopts has no
`-m "not slow"`).

## Root cause per family

1. **Ungated live tests:** `@pytest.mark.live` without a `skipif` on
   `CAO_RUN_LIVE_PROVIDER_TESTS`, and no `--model` flag to pin a free tier.
2. **Unstubbed populated_scope fixture:** other tests in the same file call
   `_disable_llm(monkeypatch)` but this one was missed — the only test using
   `populated_scope` without any LLM stub.

## Fix: two-layer defense

### Layer 1: env gate (skip by default)

Both live tests now carry:
```python
@pytest.mark.skipif(
    os.environ.get("CAO_RUN_LIVE_PROVIDER_TESTS", "") != "1",
    reason="Live provider tests disabled. Set CAO_RUN_LIVE_PROVIDER_TESTS=1 to enable.",
)
```

### Layer 2: sonnet pin (free tier when opted in)

Both `subprocess.run` call sites pin the model:
- `test/providers/test_claude_transcript_hook.py:89`:
  `[claude, "-p", "Reply with exactly OK.", "--model", "claude-sonnet-5", "--settings", ...]`
- `test/providers/test_claude_transcript_hook.py:171`:
  `[claude, "-p", "Reply with exactly OK.", "--model", "claude-sonnet-5", "--settings", ...]`

### Singleton sender fix

`test/graph/providers/test_memory_provider.py:168` — added
`_disable_llm(monkeypatch)` before constructing the provider. The LLM client is
now always `None` for this test, so `run_lint`'s contradiction pass short-circuits
with a "disabled" lint_error issue. No model pin needed (test should NEVER hit the
API).

### Makefile: test-quick `-m "not live"`

Belt-and-braces + CI parity with test-ci:
```makefile
test-quick:
	"$(TCACHE_BIN)" run "$(PYTEST_WRAPPER)" -m "not live" $(ARGS)
```

### Bonus: codex live-marker fix

`test/providers/test_codex_provider_unit.py:3716,3745` — the two
`TestCodexProviderUpdateDialogLive` methods had the env gate but were missing
`@pytest.mark.live`. Added the marker for belt-and-braces `-m "not live"`
exclusion. No model pin needed: these run `codex --strict-config exec "echo hi"`
(local config-key validation, no API call).

## Verification outputs

### Single-file run (env unset) — live tests skip

```
$ unset CAO_RUN_LIVE_PROVIDER_TESTS
$ pytest test/providers/test_claude_transcript_hook.py -v -n 0

test_every_claude_route_gets_terminal_settings[profile0] PASSED [ 16%]
test_every_claude_route_gets_terminal_settings[profile1] PASSED [ 33%]
test_every_claude_route_gets_terminal_settings[None]     PASSED [ 50%]
test_project_and_generated_session_start_hooks_both_fire SKIPPED [ 66%]
test_project_and_two_generated_hooks_are_additive_and_failure_isolated[0] SKIPPED [ 83%]
test_project_and_two_generated_hooks_are_additive_and_failure_isolated[1] SKIPPED [100%]

3 passed, 3 skipped in 0.33s
```

### collect-only with `-m "not live"` — deselection

```
$ pytest test/providers/test_claude_transcript_hook.py --collect-only -q -n 0 -m "not live"

test_every_claude_route_gets_terminal_settings[profile0]
test_every_claude_route_gets_terminal_settings[profile1]
test_every_claude_route_gets_terminal_settings[None]

3/6 tests collected (3 deselected) in 0.11s
```

### Codex live tests deselected

```
$ pytest test/providers/test_codex_provider_unit.py::TestCodexProviderUpdateDialogLive \
    --collect-only -q -n 0 -m "not live"

no tests collected (2 deselected) in 0.13s
```

### Singleton sender — passes without network

```
$ pytest test/graph/providers/test_memory_provider.py::TestMemoryProviderHappyPath::test_nodes_edges_from_populated_scope -v -n 0

test_nodes_edges_from_populated_scope PASSED [100%]

1 passed in 0.90s
```

## Files changed

| File | Change |
|------|--------|
| `test/providers/test_claude_transcript_hook.py` | env gate + `--model claude-sonnet-5` pin |
| `test/graph/providers/test_memory_provider.py` | `_disable_llm(monkeypatch)` stub |
| `Makefile` | `-m "not live"` on test-quick |
| `test/providers/test_codex_provider_unit.py` | `@pytest.mark.live` added |
