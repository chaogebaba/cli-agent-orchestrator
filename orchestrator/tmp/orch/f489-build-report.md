# F489 Build Report — Fleet TUI Auto-Start on Dispatch

**Date:** 2026-08-26  
**Issue:** #344  
**Feature:** Idempotent fleet TUI ensure-on-dispatch

## Summary

When a CAO subagent is dispatched (terminal created via assign/handoff), the
fleet TUI is now automatically ensured to be running. Idempotent and
concurrency-safe — does not block or fail terminal creation.

## Changes

### Root repo (branch `cao/f489-root`, commit `a07282b`)

| File | Change |
|------|--------|
| `scripts/fleet-tui-ensure.sh` | **NEW** — idempotent launcher with flock, fast-path alive check, absolute paths, no inherited PATH/cwd dependency |
| `scripts/fleet-tui.py` | Brought from quirks-merge-train (dependency of ensure script) |
| `scripts/launch-fleet-tui.sh` | Brought from quirks-merge-train (called by ensure script) |
| `scripts/fleet-events-sync.sh` | Brought from quirks-merge-train (dependency of fleet-tui.py) |

### Fork repo (branch `cao/f489-build`, commit `c73a780a`)

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/settings_service.py` | Added `get_tui_settings()`, `is_tui_autostart_enabled()`, `get_tui_ensure_script()` with `CAO_TUI_AUTOSTART` / `CAO_TUI_ENSURE_SCRIPT` env overlay |
| `src/cli_agent_orchestrator/services/terminal_service.py` | Added `_maybe_ensure_fleet_tui()` — fire-and-forget Popen after PostCreateTerminalEvent, gated by settings, once-per-process |
| `test/services/test_fleet_tui_ensure.py` | **NEW** — 12 unit tests |

## Design Decisions

1. **Once-per-process** — the ensure script is idempotent (flock + alive check),
   but spawning a subprocess on every single terminal creation is wasteful. A
   module-level `_fleet_tui_ensure_attempted` flag fires it at most once per
   cao-server lifetime.

2. **Settings gating** — `tui.autostart` (default True) allows disabling without
   code changes. `tui.ensure_script` allows overriding the script path.

3. **Never blocks** — `subprocess.Popen` with `start_new_session=True`, all I/O
   to DEVNULL. Wrapped in try/except → log-only warning.

4. **Root script concurrency** — `flock -n` on `/data/cao-scratch/.fleet-tui-ensure.lock`
   prevents parallel spawns. Non-blocking: if another instance holds the lock,
   exit 0 (they'll handle it).

5. **Fast-path** — checks tmux `list-panes -t SESSION:fleet` + `kill -0` before
   acquiring the lock. Expected <50ms when TUI is already alive.

## Test Results

### Unit tests (12/12 pass)

```
test/services/test_fleet_tui_ensure.py::TestMaybeEnsureFleetTui::test_fires_popen_when_enabled_and_script_exists PASSED
test/services/test_fleet_tui_ensure.py::TestMaybeEnsureFleetTui::test_skipped_when_autostart_disabled PASSED
test/services/test_fleet_tui_ensure.py::TestMaybeEnsureFleetTui::test_no_exception_when_script_missing PASSED
test/services/test_fleet_tui_ensure.py::TestMaybeEnsureFleetTui::test_fires_at_most_once_per_process PASSED
test/services/test_fleet_tui_ensure.py::TestMaybeEnsureFleetTui::test_popen_exception_does_not_propagate PASSED
test/services/test_fleet_tui_ensure.py::TestMaybeEnsureFleetTui::test_session_name_none_omits_arg PASSED
test/services/test_fleet_tui_ensure.py::TestTuiSettingsService::test_is_tui_autostart_enabled_default_true PASSED
test/services/test_fleet_tui_ensure.py::TestTuiSettingsService::test_is_tui_autostart_enabled_false_from_file PASSED
test/services/test_fleet_tui_ensure.py::TestTuiSettingsService::test_is_tui_autostart_env_override PASSED
test/services/test_fleet_tui_ensure.py::TestTuiSettingsService::test_get_tui_ensure_script_default PASSED
test/services/test_fleet_tui_ensure.py::TestTuiSettingsService::test_get_tui_ensure_script_from_settings PASSED
test/services/test_fleet_tui_ensure.py::TestTuiSettingsService::test_get_tui_ensure_script_env_override PASSED
```

### Full suite on box@cursor-3

```
13619 passed, 13 failed (pre-existing), 204 skipped, 15 xfailed
Duration: 320.32s (5m 20s)
```

Pre-existing failures (all unrelated to F489):
- `test_f424_f426_inbox_mutation_kills.py` (5 tests) — barrier generation stamping
- `test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects`
- `test_fx168_hotfix.py::TestFix2StalePathSelfHeal` (2 tests)
- `test_stage0_flip_machinery.py::test_trace_manifest_is_byte_exact_and_has_36_hits`
- `test_f77_lifecycle_pointers.py::TestFAM3TerminalizeAwaitingMembers` (2 tests)

**Zero new failures introduced by F489.**

## Configuration

To disable TUI auto-start:
```json
// ~/.aws/cli-agent-orchestrator/settings.json
{
  "tui": {
    "autostart": false
  }
}
```

Or via environment: `CAO_TUI_AUTOSTART=0`

To override the ensure script path:
```json
{
  "tui": {
    "ensure_script": "/custom/path/fleet-tui-ensure.sh"
  }
}
```

Or via environment: `CAO_TUI_ENSURE_SCRIPT=/custom/path/fleet-tui-ensure.sh`
