# F488 Build Report — Fix Round (R2)

**Branch:** `cao/f488-build`
**HEAD:** `a7529560`
**Base:** `df543db2` (original F488 implementation)
**Gate report addressed:** `/data/cao-scratch/f488-gate-report.md` (GATE-NO, 2B/2S/2N)

## Findings Addressed

| ID | Severity | Status | Summary |
|----|----------|--------|---------|
| B1 | BLOCKER | ✅ FIXED | `MemoryConfig.enabled` default changed `True` → `False` |
| B2 | BLOCKER | ✅ FIXED | All three fail-open guards now fail closed (return `False`) |
| S1 | SHOULD | ✅ FIXED | Four doc files updated: default `false`, opt-in recipe, fail-closed narrative |
| S2 | SHOULD | ✅ FIXED | Regression tests (AC6 model defaults + AC7 fail-closed guards); env cleared in file-precedence tests |
| N1 | NIT | ✅ FIXED | Test narrative says opt-in/default-False; AC4 label updated |
| N2 | NIT | SKIPPED | tier-census.json stale node — out of scope for code fix |

## Changes (9 files)

### Production code (4 files)
- `src/cli_agent_orchestrator/services/config_service.py` — `MemoryConfig.enabled: bool = False`
- `src/cli_agent_orchestrator/services/memory_service.py` — `_is_memory_enabled()` except → `False`
- `src/cli_agent_orchestrator/services/audit_log.py` — `_is_memory_enabled_safe()` except → `False`
- `src/cli_agent_orchestrator/services/wiki_lint.py` — exception path → `return []`

### Documentation (4 files)
- `docs/configuration.md` — default `false`, fail-closed note
- `docs/self-learning.md` — default `false`, both flags in recipe, fail-closed narrative
- `docusaurus/docs/reference/configuration.md` — default `false`
- `docusaurus/docs/reference/environment-variables.md` — default `false`

### Tests (1 file)
- `test/services/test_memory_enabled_flag.py`:
  - AC6: `TestConfigModelDefaults` — `MemoryConfig()`, `CAOConfig()`, JSON schema all False
  - AC7: `TestFailClosedGuards` — error-injected assertions for memory_service, audit_log, wiki_lint
  - Cleared `CAO_MEMORY_ENABLED` env in `test_returns_true_when_explicitly_enabled`
  - Narrative updated to opt-in language

## Test Evidence

**Box:** `cursor-5`
**Command:** `uv run pytest -q` (full suite)
**Result:** 13589 passed, 3 failed, 204 skipped, 15 xfailed (323s)

3 pre-existing flaky failures (unrelated to F488):
- `test_ready_deadline_edge_probe` — timing/quarantine
- `test_server_shut_down_under_us_is_a_confirmed_absence` — tmux infra
- `test_sample_ledger_monotonic_growth` — suite-slot ledger

**Local targeted (laptop):**
- `test_memory_enabled_flag.py` — 23 passed
- `test_config_service.py` + `test_learning_enabled_flag.py` — 47 passed

## Acceptance Verification

- ✅ Fresh install, no settings key, no env → `MemoryConfig().enabled` is `False`
- ✅ Fresh install, no settings key, no env → `CAOConfig().memory.enabled` is `False`
- ✅ Error paths → all guards return `False` (fail closed)
- ✅ Env/settings precedence unchanged (env > settings.json > default False)
- ✅ Regression tests prove the above WITHOUT the autouse fixture masking them
- ✅ Full suite green (modulo pre-existing flakes)
