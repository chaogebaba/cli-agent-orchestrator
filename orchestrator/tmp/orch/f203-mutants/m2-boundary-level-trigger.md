# Mutant M2: Revert D4 level-triggered boundary to any-boundary-blocks

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/boundary_pull_service.py
+++ b/src/cli_agent_orchestrator/services/boundary_pull_service.py
@@ -155,10 +155,3 @@
             if state.last_boundary_at is not None:
-                if oldest_obligation_accepted_at is not None:
-                    # Only block if boundary is at-or-after obligation acceptance
-                    if state.last_boundary_at >= oldest_obligation_accepted_at:
-                        return False
-                    # else: stale boundary, does not block
-                else:
-                    # Legacy path: any boundary blocks
-                    return False
+                return False
```

## Command
```
uv run pytest test/services/test_f203_family_sweep.py::TestF203ClassC_ThresholdAliasing::test_interrupt_fires_before_escalation -x --tb=short
```

## Result
Exit code: 0 (SURVIVED — but AC3 test kills it)

```
uv run pytest test/services/test_fx194_boundary_pull.py::TestAC2MaskedInterrupt::test_rearm_then_fresh_window_allows_fire -x --tb=short
```

Exit code: 1 (KILLED by rearm test — boundary after reset still blocks)

## Post-restore hash
dd50dccd
