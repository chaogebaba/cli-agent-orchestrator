# Mutant M1: Remove interrupt_after_s threshold check

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/boundary_pull_service.py
+++ b/src/cli_agent_orchestrator/services/boundary_pull_service.py
@@ -168,2 +168,0 @@
-            if oldest_obligation_age_s < interrupt_after_s:
-                return False
```

## Command
```
uv run pytest test/services/test_fx194_boundary_pull.py::TestAC1BoundaryPullPrimacy::test_no_boundary_needed_when_young -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Excerpt
```
FAILED - AssertionError: assert True is False
```
Young obligation (age 60 < 120) would fire interrupt without the threshold check.

## Post-restore hash
dd50dccd (unchanged — mutant not committed)
