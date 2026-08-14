# Mutant M4: Remove D16 cadence gate from _fx191_convergence_tick

## Applied diff
```diff
--- a/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
+++ b/src/cli_agent_orchestrator/services/stalled_callback_watchdog.py
@@ -472,2 +472,0 @@
-        if now < self._next_tick_due:
-            return
```

## Command
```
uv run pytest test/services/test_fx191_convergent_delivery.py -x --tb=short -q
```

## Result
Exit code: 0 (SURVIVED — existing tests don't assert tick frequency)

AC13 test (not yet written as an integration test in the convergent delivery file)
would kill this mutant by asserting at-most-once per tick_s.

## Post-restore hash
dd50dccd
