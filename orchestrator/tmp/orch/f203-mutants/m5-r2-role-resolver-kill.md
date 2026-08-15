# Mutant M5/R2: Disable role-based supervisor resolver — KILLED (r3 re-verified)

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/auto_responder.py
+++ b/src/cli_agent_orchestrator/services/auto_responder.py
@@ -986,5 +986,2 @@
-        supervisor_profiles = {"supervisor", "code_supervisor", "chao_supervisor"}
-        for terminal in terminals:
-            profile = terminal.get("agent_profile", "")
-            if profile in supervisor_profiles:
-                return terminal["id"]
+        supervisor_profiles = {"supervisor", "code_supervisor", "chao_supervisor"}
+        if False: pass
```

## Command
```
uv run pytest test/services/test_f203_mutant_kills.py::TestAC16TwoTerminalOrdering::test_supervisor_found_by_role_not_insertion_order -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Witness (r3 verbatim)
```
>       assert result == "6c1c1545", (
E       AssertionError: M5/R2 KILL: _find_supervisor returned 'stale_twin' instead of '6c1c1545'. The role-based resolver is broken — it fell through to the insertion-order fallback and returned the stale twin.
E         - 6c1c1545
E         + stale_twin
test/services/test_f203_mutant_kills.py:129: AssertionError
FAILED test/services/test_f203_mutant_kills.py::TestAC16TwoTerminalOrdering::test_supervisor_found_by_role_not_insertion_order
1 failed in 1.47s
```

## Targeting witness
S1 fixture: both terminals have `caller_id=None`. With role-lookup disabled, the fallback (`caller_id is None and provider == "claude_code"`) matches `stale_twin` first (first in query order). Role-based resolution is the ONLY disambiguator.

## Post-restore hash
0676e7c0 (r3 head)
