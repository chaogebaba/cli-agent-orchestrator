# Mutant M4: Remove D16 cadence gate — KILLED (r3 re-verified)

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -481,2 +481,0 @@
-        if now < self._next_tick_due:
-            return
```

## Command
```
uv run pytest test/services/test_f203_mutant_kills.py::TestAC13TickFrequency::test_tick_executes_at_most_once_per_tick_s -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Witness (r3 verbatim)
```
E       AssertionError: M4 KILL: convergence_tick executed 60 times in one tick_s window — cadence gate broken
E       assert 60 == 1
E        +  where 60 = <MagicMock name='convergence_tick' id='140327492002416'>.call_count
test/services/test_f203_mutant_kills.py:59: AssertionError
FAILED test/services/test_f203_mutant_kills.py::TestAC13TickFrequency::test_tick_executes_at_most_once_per_tick_s
1 failed in 1.54s
```

## Targeting witness
Without the cadence gate at lines 481-482, every invocation of `_fx191_convergence_tick` fires `convergence_tick()`. 60 rapid calls → 60 executions. Gate present → 1 execution.

## Post-restore hash
0676e7c0 (r3 head)
