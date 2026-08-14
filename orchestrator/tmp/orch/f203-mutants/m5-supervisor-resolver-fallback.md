# Mutant M5: Remove role-based lookup in _find_supervisor (revert D19)

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/auto_responder.py
+++ b/src/cli_agent_orchestrator/services/auto_responder.py
@@ -978,7 +978,0 @@
-        # Primary: role-based lookup
-        supervisor_profiles = {"supervisor", "code_supervisor", "chao_supervisor"}
-        for terminal in terminals:
-            profile = terminal.get("agent_profile", "")
-            if profile in supervisor_profiles:
-                return terminal["id"]
-
```

## Command
```
uv run pytest test/services/test_auto_responder.py -x --tb=short -q
```

## Result
Exit code: 0 (SURVIVED — existing tests use mock data that doesn't exercise two-terminal ordering)

Kill path: AC16 test (two claude_code terminals where supervisor is not first in query order)
requires a dedicated test case.

## Post-restore hash
dd50dccd
