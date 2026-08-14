# Mutant R1: Disable D15 self-notify path — NOW KILLED

## Applied diff (per reviewer report)
```diff
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -1238,1 +1238,1 @@
-                    if not caller_id and agent_profile in ("supervisor", "code_supervisor", "chao_supervisor"):
+                    if False and not caller_id and agent_profile in ("supervisor", "code_supervisor", "chao_supervisor"):
```

## Command
```
uv run pytest test/services/test_f203_mutant_kills.py::TestR1SupervisorSelfNotify::test_supervisor_no_caller_gets_self_notify -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Excerpt
```
AssertionError: R1 KILL: supervisor terminal got action='refuse' instead of 'self_notify'.
D15 self-notify path is broken.
```

## Post-restore hash
dd50dccd
