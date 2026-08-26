# F487/F475 Fix — Round 5 (Merge-Conflict Resolution)

**Merge-SHA:** `8adf6dda366b9750fa8724cb5d9896a1a299e6ac`  
**Merged:** `origin/main` (a9af1352, F483+F488+F489) INTO `cao/f487-f475-fix`  
**Code-SHA (r4 fix):** `7787ed344e92df412c515ee9d2a7ae9874da5341`  
**Date:** 2026-08-26

---

## Conflicted paths

| Path | Resolution |
|------|-----------|
| `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt` | Regenerated via `generate_manifest()` (canonical generator from `trace_manifest.py`) |

No other conflicts.

---

## Generator command

```python
from cli_agent_orchestrator.kernel.receiver_state.trace_manifest import generate_manifest
from pathlib import Path
result = generate_manifest(Path('.'))
Path('src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt').write_text(result)
# Output: 39 lines
```

Equivalent CLI: `cao verify manifest --regen`

---

## Test evidence

### Manifest byte-exact test

```
test_stage0_flip_machinery.py::test_trace_manifest_is_byte_exact_and_has_36_hits — 1 passed
```

### F487-touched family tests (box@cursor-3, clean worktree /tmp/f487-r5-wt)

```
test_f475_callback_dedup.py
test_f424_f426_inbox_mutation_kills.py
test_fx168_hotfix.py
test_wpq7_callback_barrier.py
test_f487_park_warm_watchdog.py
test_stage0_flip_machinery.py

144 passed in 12.87s
```

No failures. Merge did not disturb any F487-touched test families.
