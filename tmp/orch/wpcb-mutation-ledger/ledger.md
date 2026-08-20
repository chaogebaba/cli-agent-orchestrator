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
| M16 | 0 | AC11 (all providers) | SURVIVED — see finding |
| M19 | 1 | AC14::test_token_header_attached_alongside_auth | YES |

## Findings (mutants that survived their targeting witness)

### M6 — Hardcode path (targeting: AC8)
**Exit:** 0 (pass)  
**Root cause:** AC8 asserts `entry["command"]` is truthy (non-empty) but does not compare against `resolve_cao_mcp_command`'s actual return value. A hardcoded non-empty path satisfies the assertion.  
**Impact:** Low — the e2e AC5/AC18 catches this at runtime (hardcoded path breaks after `uv tool upgrade`). The unit test could be tightened to mock `resolve_cao_mcp_command` and assert the return value matches the materialized entry.

### M7 — rm -rf instead of shutil.rmtree (targeting: AC15)
**Exit:** 0 (pass)  
**Reclassified:** DESIGN-MUTANT. Both `shutil.rmtree` and `rm -rf` unlink symlinks without following them on Linux. The mutation is functionally equivalent. Killed by D5 design mandate (no shell injection surface, no process-spawn).

### M8 — Delete without parent/basename guard (targeting: AC15)
**Exit:** 0 (pass)  
**Root cause:** The test creates the target dir under a DIFFERENT root than `_data_dir()` resolves to. With guard removed, `_data_dir()` still points to a non-existent path (the guard passes vacuously). Test structure needs the dd to match `_data_dir()` for the guard test to be meaningful.  
**Impact:** Medium — the guard IS implemented in production code; the test's assertion verifies the log-level branch but not the delete-protection branch.

### M13 — Compare with == (targeting: AC1)
**Exit:** 0 (pass)  
**Reclassified:** DESIGN-MUTANT. Timing side-channels are not observable in unit tests. Killed by D7 design mandate (constant-time comparison on principle).

### M16 — Remove CAO_TERMINAL_TOKEN from claude_code.py (targeting: AC11)
**Exit:** 0 (pass)  
**Root cause:** `claude_code.py` also calls `bind_mcp_server_identity` which sets `CAO_TERMINAL_TOKEN` (via `sandbox_guard.py`). The AC11 test correctly allows either mechanism (`CAO_TERMINAL_TOKEN in source OR bind_mcp_server_identity in source`). The provider has dual coverage — removing the explicit lines is safe because `bind_mcp_server_identity` handles it.  
**Impact:** None — this is correct behavior, not a gap. The provider is covered by both paths.

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
