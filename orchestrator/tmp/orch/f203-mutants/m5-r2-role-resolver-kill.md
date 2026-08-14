# Mutant M5/R2: Remove D19 role-based lookup — NOW KILLED

## Applied diff (R2 per reviewer report)
```diff
--- a/src/cli_agent_orchestrator/services/auto_responder.py
+++ b/src/cli_agent_orchestrator/services/auto_responder.py
@@ -988,7 +988,7 @@
-        supervisor_profiles = {"supervisor", "code_supervisor", "chao_supervisor"}
-        for terminal in terminals:
-            profile = terminal.get("agent_profile", "")
-            if profile in supervisor_profiles:
-                return terminal["id"]
+        if False:  # MUTANT: disabled
+            pass
```

## Command
```
uv run pytest test/services/test_f203_mutant_kills.py::TestAC16TwoTerminalOrdering::test_supervisor_found_by_role_not_insertion_order -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Excerpt
```
AssertionError: M5/R2 KILL: _find_supervisor returned 'stale_twin' instead of '6c1c1545'.
The role-based resolver is broken — it fell through to the insertion-order fallback.
```

## Post-restore hash
dd50dccd
