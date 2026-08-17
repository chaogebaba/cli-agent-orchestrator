# DST Liveness Harness — Mutation Ledger

4 mutants, 1 per fault-class seam. All KILLED.

---

## M1: Clock delegation seam (D3)

**Seam:** `src/cli_agent_orchestrator/clients/database.py` — `_utcnow()` delegation to sim clock.

**Applied diff:**
```diff
 def _utcnow() -> datetime:
-    from cli_agent_orchestrator.sim.clock import active as _sim_clock_active
-
-    clock = _sim_clock_active()
-    if clock is not None:
-        return clock.utcnow()
     return datetime.now(timezone.utc)
```

**Exact command:**
```
uv run pytest test/simulation/test_sim_substrate.py::TestAC1ClockNoLeaks::test_utcnow_uses_sim_clock_when_installed -x --no-header -q
```

**Exit code:** 1

**Failing output excerpt:**
```
>           assert result == datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
E           assert datetime.datetime(2026, 8, 15, 1, 53, 12, 895347, tzinfo=datetime.timezone.utc) == datetime.datetime(2026, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)

test/simulation/test_sim_substrate.py:32: AssertionError
FAILED test/simulation/test_sim_substrate.py::TestAC1ClockNoLeaks::test_utcnow_uses_sim_clock_when_installed
1 failed in 3.02s
```

**Post-restore sha256:** `2ea76882a6d4a47750541e6740e7784dc2b8a2b65e3b26d32c281796e4d45cf7`

**Targeting witness:** `TestAC1ClockNoLeaks::test_utcnow_uses_sim_clock_when_installed` — asserts `_utcnow()` returns the sim clock's virtual time, not real wall-clock.

---

## M2: RNG sub-stream isolation (D9)

**Seam:** `src/cli_agent_orchestrator/sim/rng.py` — `SimRNG.stream()` named sub-stream independence.

**Applied diff:**
```diff
-                self._streams[name] = random.Random(sub_seed)
+                self._streams[name] = self._master
```

**Exact command:**
```
uv run pytest test/simulation/test_sim_substrate.py::TestAC3ByteIdenticalReplay::test_adding_stream_does_not_shift_others -x --no-header -q
```

**Exit code:** 1

**Failing output excerpt:**
```
E         At index 1 diff: 750 != 10
E         Use -v to get more diff

test/simulation/test_sim_substrate.py:167: AssertionError
FAILED test/simulation/test_sim_substrate.py::TestAC3ByteIdenticalReplay::test_adding_stream_does_not_shift_others
1 failed in 2.89s
```

**Post-restore sha256:** `beaffee26bf6ae903e499e96ea9e4a2a14d490f5b257764ac98320a5b9dead0f`

**Targeting witness:** `TestAC3ByteIdenticalReplay::test_adding_stream_does_not_shift_others` — asserts that interleaving draws from a new "workload" stream does not shift the "faults" stream sequence.

---

## M3: Livelock detection (D16) — previously surviving, NOW KILLED by S1

**Seam:** `src/cli_agent_orchestrator/sim/world.py` — `check_liveness()` no_progress_count → LIVELOCK path.

**Applied diff:**
```diff
             made_progress = self.step()
-            if not made_progress:
-                no_progress_count += 1
-                if no_progress_count > 3:
-                    undelivered_ids = [o["inbox_row_id"] for o in self.undelivered()]
-                    return LivenessVerdict(
-                        LivenessVerdict.LIVELOCK,
-                        details=f"no_deadline_progress undelivered={undelivered_ids}",
-                        seed=self._seed,
-                    )
-            else:
-                no_progress_count = 0
+            if not made_progress:
+                # M3 mutant: just add a deadline and continue (no LIVELOCK)
+                if self._driver is not None:
+                    self._driver.add_deadline(self._clock.monotonic() + 5.0)
```

**Exact command:**
```
uv run pytest test/simulation/test_liveness_harness.py::TestPureLivelock::test_pure_livelock_verdict -x --no-header -q
```

**Exit code:** 1

**Failing output excerpt:**
```
E               AssertionError: Expected LIVELOCK but got LIVENESS_TIMEOUT: iteration_cap undelivered=[9999]. M3 mutant (removing LIVELOCK detection) would cause this to be LIVENESS_TIMEOUT.
E               assert 'LIVENESS_TIMEOUT' == 'LIVELOCK'

test/simulation/test_liveness_harness.py:278: AssertionError
FAILED test/simulation/test_liveness_harness.py::TestPureLivelock::test_pure_livelock_verdict
1 failed in 3.16s
```

**Post-restore sha256:** `a6f63ff877bc1e7350379ac6fb7ae6c1d5c269ccac910c9aec9fe5f7ab15868f`

**Targeting witness:** `TestPureLivelock::test_pure_livelock_verdict` — asserts `verdict == LivenessVerdict.LIVELOCK` specifically (not "either timeout or livelock"), for the shape where step()→False with no deadlines and undelivered obligations.

---

## M4: Phase guard (D15 / Do-NOT #6)

**Seam:** `src/cli_agent_orchestrator/sim/faults.py` — `FaultSet.inject()` phase check.

**Applied diff:**
```diff
-        if self._phase != "CHAOS":
+        if False:  # M4 mutant: phase guard removed
             raise RuntimeError(
                 f"Cannot inject fault during {self._phase} phase (Do-NOT #6)"
             )
```

**Exact command:**
```
uv run pytest test/simulation/test_liveness_harness.py::TestAC5FaultClassesInjectAndHeal::test_no_injection_during_require_progress -x --no-header -q
```

**Exit code:** 1

**Failing output excerpt:**
```
>       with pytest.raises(RuntimeError, match="Cannot inject fault"):
E       Failed: DID NOT RAISE <class 'RuntimeError'>

test/simulation/test_liveness_harness.py:77: Failed
FAILED test/simulation/test_liveness_harness.py::TestAC5FaultClassesInjectAndHeal::test_no_injection_during_require_progress
1 failed in 2.93s
```

**Post-restore sha256:** `dd3b6caf6f585432490e4ded44dea29712558251c68d8ae4416b13c287accbb8`

**Targeting witness:** `TestAC5FaultClassesInjectAndHeal::test_no_injection_during_require_progress` — asserts `pytest.raises(RuntimeError)` fires when injecting during REQUIRE_PROGRESS phase.

---

## Summary

| Mutant | Seam | Targeting Witness | Result |
|---|---|---|---|
| M1 | Clock delegation (D3) | `AC1::test_utcnow_uses_sim_clock_when_installed` | **KILLED** |
| M2 | RNG sub-stream isolation (D9) | `AC3::test_adding_stream_does_not_shift_others` | **KILLED** |
| M3 | Livelock detection (D16) | `TestPureLivelock::test_pure_livelock_verdict` (S1) | **KILLED** |
| M4 | Phase guard (D15) | `AC5::test_no_injection_during_require_progress` | **KILLED** |

All 4 mutants killed. M3 was the previously surviving mutant identified by the diff-gate reviewer; it is now killed by the dedicated pure-livelock test (S1 fix).
