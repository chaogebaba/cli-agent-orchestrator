# Mutant M3: Change ejection threshold from 3 to 999

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/transport_ejection.py
+++ b/src/cli_agent_orchestrator/services/transport_ejection.py
@@ -38,1 +38,1 @@
-    EJECTION_THRESHOLD = 3
+    EJECTION_THRESHOLD = 999
```

## Command
```
uv run pytest test/services/test_f203_transport_ejection.py::TestAC6CountedEjection -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Excerpt
```
FAILED test_ejection_after_threshold - AssertionError: assert False is True
```
3 refusals no longer trigger ejection with threshold=999.

## Post-restore hash
dd50dccd
