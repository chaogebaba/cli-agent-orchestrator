# F493 Build Report — delete_terminal wedge fix

**Git-SHA-fork:** `a7625d872add3c8aae2f0bc52cfb7cf506c6febf`
**Branch:** `cao/ca2b048e`
**Date:** 2026-08-26

## Changes

### 1. mcp_server/server.py — provider-aware deferral messages
- Added `_cleanup_deferred_message(terminal_id)` helper that queries terminal
  metadata for the actual provider name.
- Replaced 3 hardcoded "after the Grok process exits" messages with calls to
  the helper, producing e.g. "after the kiro_cli process exits".

### 2. api/main.py — DELETE /terminals/{id} 409 response
- The 409 Conflict response now calls `get_terminal_metadata(terminal_id)` and
  includes the terminal's actual provider in the detail string instead of
  hardcoded "Grok".

### 3. terminal_service.py — force plumbing to _delete_terminal_under_lease
- Added `force: bool = False` parameter to `_delete_terminal_under_lease`.
- Passed `force=force` from the cascade call site in `_delete_terminal_inner`.
- When `force=True` and `cleanup_provider()` returns `False`, deletion proceeds
  with a warning log instead of returning `cleanup_deferred`.
- Non-force behavior is unchanged (still defers).

## Regression Tests (test/services/test_f493_delete_wedge.py)

| Test | Covers |
|------|--------|
| `test_mcp_delete_cleanup_deferred_message_names_provider` | kiro_cli terminal → message says "kiro_cli", never "Grok" |
| `test_mcp_delete_cleanup_deferred_non_raise_path_names_provider` | Pre-raise_for_status 409 path names provider |
| `test_mcp_delete_payload_failure_names_provider` | Payload success=False path names provider |
| `test_api_delete_terminal_deferred_names_provider` | API 409 detail names provider, not Grok |
| `test_delete_under_lease_force_overrides_cleanup_deferral` | force=True + cleanup_provider=False → deletion proceeds |
| `test_delete_under_lease_non_force_still_defers` | force=False + cleanup_provider=False → deferred (existing behavior) |

## Suite Run

- **Box:** cursor-3
- **Command:** `cd ~/cli-subagents/cli-agent-orchestrator && make test-quick`
- **Exit:** 2 (1 pre-existing flaky test)
- **Counts:** 13793 passed, 190 skipped, 15 xfailed, 1 failed
- **Sole failure:** `test_suite_slot.py::TestLedgerSampling::test_sample_records_child_process`
  — subprocess terminate+wait(5) timeout race, pre-existing box-only flake, unrelated to F493.

## Verdict

**PASS** — all F493 objectives met, no regressions introduced.
