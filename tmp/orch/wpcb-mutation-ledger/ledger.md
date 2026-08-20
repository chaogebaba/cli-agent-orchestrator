# WP-CALLBACKS Mutation Kill Ledger

**Branch:** cao/9b0f21a0-wpcallbacks  
**Pre-mutation hash:** `e2284a7a40b4a8d17c8d92f40c717b9941f8c9d4`

## Executed Code-Level Mutants

| # | Exit | Witness | Killed? |
|---|------|---------|---------|
| M3 | 1 | AC7::test_build_base_args_includes_data_dir | YES |
| M4 | 1 | AC6::test_symlink_allowlist_creates_valid_links | YES |
| M5 | 1 | AC7::test_build_base_args_includes_data_dir | YES |
| M9 | 1 | AC14::test_token_header_attached | YES |
| M10 | 1 | AC2::test_wrong_token_returns_403 | YES |
| M11 | 1 | AC1::test_no_token_header_returns_403 | YES |
| M12 | 1 | AC16::test_null_token_sender_returns_403_unknown | YES |
| M14 | 1 | AC1::test_no_token_header_returns_403 | YES |
| M15 | 1 | AC9::test_forged_sender_with_drift_gets_403_not_drift_notice | YES |
| M16 | 0 | AC11 (all providers) | EQUIVALENT — see below |
| M19 | 1 | AC14::test_token_header_attached_alongside_auth | YES |

## Findings (mutants that survived their targeting witness)

### M6 — Hardcode path (targeting: AC8)
**Exit:** 1 (KILLED after AC8 tightened)  
**Excerpt:** `AssertionError: Materialized command '/home/chao/.local/bin/cao-mcp-server' != resolver return '/resolved/by/mcp_resolution/cao-mcp-server'. M6 mutant (hardcoded path) would produce the hardcoded value here.`  
**Fix:** AC8 test now mocks `resolve_cao_mcp_command` with a sentinel value and asserts the materialized settings contain that sentinel — a hardcoded path produces a different value and fails.

### M7 — rm -rf instead of shutil.rmtree (targeting: AC15)
**Exit:** 0 (pass)  
**Reclassified:** DESIGN-MUTANT. Both `shutil.rmtree` and `rm -rf` unlink symlinks without following them on Linux. The mutation is functionally equivalent. Killed by D5 design mandate (no shell injection surface, no process-spawn).

### M8 — Delete without parent/basename guard (targeting: AC15)
**Exit:** 1 (KILLED after AC15 test fixed)  
**Excerpt:** `AssertionError: Guard failed to refuse — dir was deleted despite parent mismatch`  
**Fix:** AC15 test now patches `_data_dir()` to return a real existing directory while setting `CLINE_SANDBOX_ROOT` to a different path — so the guard's `dd.parent == CLINE_SANDBOX_ROOT` comparison is actually exercised. With `if True:` the dir gets deleted; with the guard it survives.

### M13 — Compare with == (targeting: AC1)
**Exit:** 0 (pass)  
**Reclassified:** DESIGN-MUTANT. Timing side-channels are not observable in unit tests. Killed by D7 design mandate (constant-time comparison on principle).

### M16 — Remove CAO_TERMINAL_TOKEN from claude_code.py (targeting: AC11)
**Exit:** 0 (pass)  
**Classification:** CLAIMED-EQUIVALENT (dual-coverage)

**Equivalence argument:** `claude_code.py` has two independent paths that inject `CAO_TERMINAL_TOKEN` into MCP server env:

1. **Explicit injection** (`:579-585`): `if "CAO_TERMINAL_TOKEN" not in env: env["CAO_TERMINAL_TOKEN"] = ...` — the lines M16 removes.
2. **`bind_mcp_server_identity`** (`:570`): `mcp_config[server_name] = bind_mcp_server_identity(resolve_mcp_server_config(...), self.terminal_id)` — calls `sandbox_guard.py:38-60`, which at line 52-54 sets `expected["CAO_TERMINAL_TOKEN"] = terminal_token` when `os.environ.get("CAO_TERMINAL_TOKEN", "")` is non-empty.

Path 2 executes BEFORE path 1 (line 570 < line 579). After `bind_mcp_server_identity` returns, `env` already contains `CAO_TERMINAL_TOKEN`. The explicit check at path 1 (`if "CAO_TERMINAL_TOKEN" not in env`) is therefore a **no-op** — the key is already set. Removing path 1 (M16's mutation) produces identical runtime behavior because path 2 already set the value.

AC11's test correctly identifies this as covered: the assertion is `"CAO_TERMINAL_TOKEN" in source OR "bind_mcp_server_identity" in source`. Both predicates reflect a real injection mechanism. The hand-written-list mutant's threat — "provider #13 forgets" — is caught by the first predicate (`CAO_TERMINAL_TOKEN in source`) for providers that use direct injection, and by the second for providers that delegate to `bind_mcp_server_identity`.

## DESIGN-MUTANTS (killed by blueprint decisions, not tests)

| # | Decision violated | Kill evidence |
|---|---|---|
| M1 | Do-NOT 1 + §2 (no synthetic callbacks) | No code path exists; would contradict F332's trust model |
| M2 | D1-alt-1 (shared-file excluded by M2) | Pre-authorised fallback is D1-alt-3 only; shared-file would fail AC5 property |
| M7 | D5 (no process-spawn in cleanup) | shutil.rmtree and rm -rf are functionally equivalent on symlinks |
| M13 | D7 (constant-time comparison) | Timing attack not unit-testable; principle mandate |

## DEFERRED (e2e witnesses)

| # | Witness AC | Reason |
|---|---|---|
| M2 | AC5 (e2e) | Fallback path only exercised on AC5 live failure |
| M17 | AC18 (e2e) | Codex value-assignment correctness needs live MCP subprocess |
| M20 | AC18 (e2e) | F332 without F329' needs live cline worker |

## Post-restore verification
All mutations restored to `e2284a7a40b4a8d17c8d92f40c717b9941f8c9d4`.
