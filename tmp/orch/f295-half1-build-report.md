# F295 Half 1 — Build Report: Grok Config Lifecycle

**Artifact-Path:** tmp/orch/f295-half1-build-report.md
**Git-SHA-fork:** d66435c4a5776d1ee2fac29fa2cbc6658a26a29a
**Branch:** cao/f295-half1
**Worktree:** /data/cao-scratch/f295-build
**Issue:** #149 (scope-split comment — Half 1)

---

## Pre-Implementation Investigation

### (a) Does provider __init__/_prepare_grok_home re-run for adopted live terminals?

**Finding:** YES — `ProviderManager.get_provider()` (manager.py:231-266) recreates the
provider on-demand from DB metadata when no in-memory mapping exists (post-server-restart
adoption path). This calls `GrokCliProvider.__init__()` → `_prepare_grok_home()`.

**Impact on design:** The rebuild MUST be anchored in `_build_grok_command()` (the LAUNCH
path only called during `initialize()`, `build_fork_command()`, `build_resume_command()`),
NOT in `__init__`. The on-demand restoration path restores a provider for an already-running
terminal — its config was built at launch time. ✓ Design is correct.

### (b) Other writers into private GROK_HOME config.toml

**Sweep result:** Only ONE writer: `ensure_grok_mcp_servers()` (utils/grok_config.py) —
called from `_build_grok_command()` AFTER the rebuild point. It upserts MCP server sections
on top of whatever content is present. The rebuild writes the canonical base, then MCP upsert
adds terminal-specific sections on top. No clobber risk. ✓

---

## AC0 — Rebuild-Per-Launch

**Implementation:** `_rebuild_private_config()` added to `grok_cli.py`, called at the top
of `_build_grok_command()` before `ensure_grok_mcp_servers`.

**Logic:**
1. Read canonical `~/.grok/config.toml` (via `provider_home("grok_cli").home / "config.toml"`)
2. Sanity-parse with `tomllib.loads()`
3. On success: atomic-write over private config (reuses `_atomic_write_private`)
4. On canonical missing OR parse failure: keep existing private config + log warning

**`_prepare_grok_home` unchanged:** retains seed-once as first-creation bootstrap.

### Test Evidence

```
$ uv run pytest test/providers/test_f295_grok_config_lifecycle.py::TestAC0RebuildPerLaunch -v
PASSED test_rebuild_updates_private_config_from_canonical
PASSED test_malformed_canonical_keeps_prior_private_config
PASSED test_missing_canonical_keeps_prior_private_config
PASSED test_mcp_sections_survive_rebuild
```

---

## AC2 — Staleness Stamp

**Implementation:**
- At rebuild time, stamps `hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()` into
  terminal metadata via `update_terminal_metadata(terminal_id, {"config_sha256": hash})`.
- `fleet_service.py`: added `_current_grok_canonical_hash()` (computes current hash once per
  fleet call) and `_is_config_stale(row, canonical_hash)` → `config_stale` boolean per terminal.
- `list_terminals_by_session` now surfaces `metadata` field for fleet consumption.

### Test Evidence

```
$ uv run pytest test/providers/test_f295_grok_config_lifecycle.py::TestAC2StalenessStamp -v
PASSED test_stamp_written_on_rebuild
PASSED test_fleet_config_stale_true_when_hash_differs
PASSED test_fleet_config_stale_false_when_hash_matches
PASSED test_fleet_config_stale_none_for_non_grok
PASSED test_fleet_config_stale_none_when_no_metadata
PASSED test_fleet_config_stale_none_when_no_canonical
```

---

## AC4 — Change Notice

**Implementation:** New module `services/grok_config_watcher.py`:
- `GrokConfigWatcher` async background task — polls canonical mtime every 10s.
- Debounce: tracks mtime + hash; mtime-change with same-hash = no notice (touch safety).
- On real content change: counts stale terminals, pushes ONE inbox message to supervisor via
  `create_routed_inbox_message`.
- Wired into `api/main.py` lifespan (skipped in sandbox mode). Cancelled on shutdown.

### Test Evidence

```
$ uv run pytest test/providers/test_f295_grok_config_lifecycle.py::TestAC4ChangeNotice -v
PASSED test_no_notice_on_same_hash
PASSED test_notice_on_content_change
PASSED test_debounce_no_duplicate_notice_same_mtime
PASSED test_deleted_canonical_no_crash
```

---

## Style & Type Checks

```
$ uv run black --check --line-length 100 <touched files>
All done! 5 files would be left unchanged.

$ uv run isort --check --profile black --line-length 100 <touched files>
(no errors)

$ uv run mypy --strict src/cli_agent_orchestrator/services/grok_config_watcher.py \
              src/cli_agent_orchestrator/services/fleet_service.py
Success: no issues found in 2 source files

$ uv run mypy --strict src/cli_agent_orchestrator/providers/grok_cli.py
Found 10 errors (all pre-existing — lines 117, 163, 178, 206, 242, 246, 336, 370;
none in new _rebuild_private_config code)
```

---

## Existing Test Regression

```
$ uv run pytest test/providers/test_grok_cli_unit.py test/providers/test_grok_cli_private_home.py -n 2
======================== 61 passed, 1 xfailed in 3.55s =========================
```

No regressions.

---

## Files Modified

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/providers/grok_cli.py` | +`import tomllib`, +`_rebuild_private_config()`, call in `_build_grok_command` |
| `src/cli_agent_orchestrator/services/fleet_service.py` | +`_current_grok_canonical_hash`, +`_is_config_stale`, `config_stale` in projection |
| `src/cli_agent_orchestrator/services/grok_config_watcher.py` | NEW — background mtime watcher |
| `src/cli_agent_orchestrator/clients/database.py` | `list_terminals_by_session` +metadata field |
| `src/cli_agent_orchestrator/api/main.py` | Wire grok_config_watcher into lifespan |
| `test/providers/test_f295_grok_config_lifecycle.py` | NEW — 14 unit tests |
