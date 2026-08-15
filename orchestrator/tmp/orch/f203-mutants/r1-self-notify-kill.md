# Mutant R1: Disable D15 self-notify — KILLED (r3 re-verified)

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -1239 +1239 @@
-                if not caller_id and agent_profile in ("supervisor", "code_supervisor", "chao_supervisor"):
+                if False and not caller_id and agent_profile in ("supervisor", "code_supervisor", "chao_supervisor"):
```

## Command
```
uv run pytest test/services/test_f203_mutant_kills.py::TestR1SupervisorSelfNotify::test_supervisor_no_caller_gets_self_notify -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Witness (r3 verbatim)
```
>       mock_self_notify.assert_called_once_with("sup_r1")
E       AssertionError: Expected 'mock' to be called once. Called 0 times.
------------------------------ Captured log call -------------------------------
WARNING  cli_agent_orchestrator.services.stalled_callback_watchdog:stalled_callback_watchdog.py:1257 waiting-inbox watchdog: refusing invalid caller for terminal sup_r1
FAILED test/services/test_f203_mutant_kills.py::TestR1SupervisorSelfNotify::test_supervisor_no_caller_gets_self_notify
1 failed in 1.51s
```

## Targeting witness
V1-c: test invokes the REAL `tick_waiting_inbox` method (no inline reimplementation). With D15 self-notify condition disabled, the supervisor terminal (`caller_id=None`, `agent_profile="supervisor"`) falls through to the refusal path. Log: `refusing invalid caller for terminal sup_r1`. `_create_self_notify_obligation` never called → assertion fires.

## Post-restore hash
0676e7c0 (r3 head)
