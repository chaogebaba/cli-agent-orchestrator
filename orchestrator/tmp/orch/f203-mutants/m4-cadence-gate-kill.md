# Mutant M4: Remove D16 cadence gate — NOW KILLED

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -480,2 +480,0 @@
-        if now < self._next_tick_due:
-            return
```

## Command
```
uv run pytest test/services/test_f203_mutant_kills.py::TestAC13TickFrequency::test_tick_executes_at_most_once_per_tick_s -x --tb=short
```

## Result
Exit code: 1 (KILLED)

## Excerpt
```
AssertionError: M4 KILL: convergence_tick executed 60 times in one tick_s window — cadence gate broken
assert 60 == 1
```
Without cadence gate, every invocation fires convergence_tick.

## Post-restore hash
dd50dccd (fix round commit)
