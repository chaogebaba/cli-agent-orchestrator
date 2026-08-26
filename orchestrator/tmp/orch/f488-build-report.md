# F488 Build Report — Memory Subsystem Defaults OFF

## Summary
Flipped memory subsystem default from ON (opt-out) to OFF (opt-in) per issue #343. Fresh installs no longer spend Claude quota on CAO memory unless explicitly enabled.

## Changes

### Production code
- `src/cli_agent_orchestrator/services/settings_service.py`:
  - `is_memory_enabled()`: both `.get("enabled", True)` → False, exception fallback → False
  - `get_memory_settings()`: defaults dict `"enabled": True` → False
  - `is_learning_enabled()`: internal `.get("enabled", True)` → False
  - Docstrings updated
- `src/cli_agent_orchestrator/services/config_service.py`:
  - `CAO_MEMORY_ENABLED` env mapping default: True → False
  - `MemoryConfig` builder: `_get_value("memory.enabled", default=True)` → False

### Test code
- `test/conftest.py`: Added autouse fixture `_enable_memory_in_tests` that sets
  `CAO_MEMORY_ENABLED=true` env var — preserves old test behavior (tests assume memory on)
- `test/services/test_memory_enabled_flag.py`: Updated default-absent test, added
  `monkeypatch.delenv` for tests that verify disabled behavior
- `test/services/test_learning_enabled_flag.py`: Fixed tests that now need explicit
  `enabled=True` in settings or env for the full chain to work
- `test/services/test_audit_log.py`: Fixture enables memory for write tests

## How to enable memory
```bash
# Environment variable (takes precedence)
export CAO_MEMORY_ENABLED=true

# Or in settings.json
{"memory": {"enabled": true}}

# Or via CLI
cao config set memory.enabled true
```

## Test Results

### Local (targeted: 303 tests)
303 passed, 6 skipped

### Offload box (full suite, cursor-3)
- **13620 passed, 15 failed, 204 skipped, 1 error**
- All failures are pre-existing (cc_inbox_path stale fakes, tmux flakes,
  manifest stale, lifecycle pointer, etc.)
- Zero F488-related failures

## Branch / SHA
- Branch: `cao/f488-build`
- HEAD: `41ecfc19`
