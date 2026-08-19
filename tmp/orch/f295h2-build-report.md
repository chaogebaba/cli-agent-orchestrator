**Artifact-Path:** /data/cao-scratch/f295h2-worktree/tmp/orch/f295h2-build-report.md
**Artifact-Repo-Path:** tmp/orch/f295h2-build-report.md
**Git-SHA-fork:** b0bcfb05d164e6d647e0eda91109e666b4d2c01a

## F295 Half 2 — Build Report

### Per-AC Evidence

| AC | Status | Evidence |
|----|--------|----------|
| AC1 | GREEN | `TestConnectionRefused::test_connection_refused_raises` — dead relay raises `RelayPreflightFailed`; Step 1a placed before F138 incarnation block (S1). No window/row/worktree on failure by construction. |
| AC2 | GREEN | `TestOfficialRouteNoProbe::test_no_base_url_no_probe` / `test_no_model_table_no_probe` — patched HTTP client raises on any call; launch succeeds. |
| AC3 | GREEN | `TestExactlyOneProbe::test_one_request_on_responses_backend` — asserts `mock_post.call_count == 1`. |
| AC4 | GREEN | `TestKeyRedaction::test_key_in_502_response` — detail contains endpoint URL, model, `http_502`; key value (`sk-live-secret123`) count in detail string = 0. |
| AC5 | GREEN | `TestEscapes::test_disabled_via_providers_toml` / `test_sandbox_skips` — both bypass confirmed. |
| AC6 | GREEN | `TestProbeShapes` — responses→POST /responses w/ max_output_tokens; chat→POST /chat/completions w/ max_tokens; unknown→GET (404 passes, ConnRefused fails). |
| AC7 | GREEN | `TestWedgeArmFiringAndDedup::test_fires_once_on_grok_cli_after_age_threshold` — exactly one notice, dedup on second tick; F228-b arm confirmed silent. |
| AC8 | GREEN | `TestLivenessExcludePatterns::test_grok_provider_has_processing_pattern` — `GrokCliProvider.liveness_exclude_patterns == [PROCESSING_PATTERN]`. |
| AC9 | GREEN | `TestFlagAndNotifyOnly::test_no_send_keys_no_status_write_no_reap` — backend.send_keys never called, status unchanged (PROCESSING). Source grep: no `send_key`, `Escape`, `\x1b`, `status.*ERROR`, or `respawn` in the wedge arm code. |
| AC10 | GREEN | `TestWedgeFlagProjection::test_wedge_suspect_in_fleet` / `test_clears_on_status_transition` — projects True, clears on IDLE transition. |
| AC11 | GREEN | `TestReapedTerminalNotFlagged::test_reaped_before_recheck_no_notice` — no notice, no metadata write, no exception. |
| AC12 | GREEN | `TestSystemMetadataProtection::test_worker_replace_preserves_cao_namespace` / `test_worker_cannot_inject_cao_key` — worker full-replace preserves `cao`; worker's `cao` key stripped. Pre-existing `TestWorktreeInfoImmutability` (2 tests) and `test_update_terminal_metadata` (1 test) pass unmodified. |
| AC13 | GREEN | `TestLegacyFallback::test_is_config_stale_legacy_row` / `test_is_config_stale_new_namespace` / `test_count_stale_legacy_and_new` — legacy top-level and new `cao` namespace both classify correctly. |
| AC14 | GREEN | Pre-existing test suites all green (below). |
| AC15 | PARKED | Root-side patch parked at `tmp/orch/f295h2-root-side.patch`. |

### Pre-existing Test Suites (AC14)

| File | Tests | Result |
|------|-------|--------|
| `test/services/test_f228b_no_progress_watchdog.py` | part of 109 | PASS |
| `test/services/test_stalled_callback_watchdog.py` | part of 109 | PASS |
| `test/services/test_fx181_quiescence_watchdog.py` | 68 | PASS |
| `test/providers/test_f295_grok_config_lifecycle.py` | 14 | PASS |
| `test/providers/test_grok_cli_unit.py` | part of 62 | PASS |
| `test/providers/test_grok_cli_private_home.py` | part of 62 | PASS |
| `test/services/test_worktree_branch_integrity.py::TestWorktreeInfoImmutability` | 2 | PASS |
| `test/clients/test_database.py::TestGroupAndMetadata::test_update_terminal_metadata` | 1 | PASS |

### New Tests

| File | Tests | Result |
|------|-------|--------|
| `test/utils/test_f295_relay_preflight.py` | 16 | PASS |
| `test/services/test_f295_wedge_watchdog.py` | 10 | PASS |
| `test/clients/test_f295_system_metadata.py` | 7 | PASS |

Total: 33 new tests green.

### CI Status

- **Run ID:** 32200843394
- **URL:** https://github.com/chaogebaba/cli-agent-orchestrator/actions/runs/32200843394
- **headSha:** c633326f7e3d8c06abae45f677f23db12bd55269 (pre-rebase equivalent of b0bcfb05; same tree content, verified)
- **Conclusion:** 4 failed, 11575 passed, 55 skipped, 9 xfailed (215s)
- **Adjudication:** 4-known / 0-owned
  1. `test/clients/test_database.py::TestMessageTraceTransactions::test_list_terminals_by_session` — F303: MagicMock for `metadata_json` is truthy on Py3.14.7; `list_terminals_by_session` is byte-identical to base `dc70fd76`
  2. `test/clients/test_f264_database_hardening.py::test_list_terminals_by_session_skips_stale_rows` — same root cause
  3. `test/test_f254_quarantine.py::test_no_expired_quarantine_entries` — pre-existing quarantine entries with invalid `expires=''`
  4. `test/test_f254_quarantine.py::test_expiry_guard_fires_for_non_serial_only` — same quarantine entry issue

### Modified Files

**Source (8 files modified, 1 new):**
- `src/cli_agent_orchestrator/utils/grok_preflight.py` — NEW (relay probe + exception)
- `src/cli_agent_orchestrator/providers/base.py` — `preflight_launch` classmethod
- `src/cli_agent_orchestrator/providers/grok_cli.py` — override + liveness_exclude_patterns + D12 stamp repoint
- `src/cli_agent_orchestrator/services/terminal_service.py` — Step 1a await
- `src/cli_agent_orchestrator/api/main.py` — RelayPreflightFailed in exception arm
- `src/cli_agent_orchestrator/clients/database.py` — D12 system metadata functions + preserved key
- `src/cli_agent_orchestrator/services/stalled_callback_watchdog.py` — wedge arm
- `src/cli_agent_orchestrator/services/fleet_service.py` — cao namespace + wedge_suspect
- `src/cli_agent_orchestrator/services/grok_config_watcher.py` — cao namespace fallback

**Tests (3 new, 3 modified):**
- `test/utils/test_f295_relay_preflight.py` — NEW
- `test/services/test_f295_wedge_watchdog.py` — NEW
- `test/clients/test_f295_system_metadata.py` — NEW
- `test/providers/test_f295_grok_config_lifecycle.py` — updated mock target for D12 repoint
- `test/services/test_stage0_flip_machinery.py` — trace manifest hit count 37→38
- `test/cli/commands/test_cli_verify.py` — trace manifest hit count assertions 37→38

### Root-Side Patch (AC15)

Parked at `tmp/orch/f295h2-root-side.patch` — adds commented knobs to `providers.toml.default` and one paragraph to `USAGE.md`.
