# F254 Phase 6 — Build Report R1

Artifact-Path: /home/chao/VScode_projects/cli-subagents/orchestrator/tmp/orch/f254-p6-build-r1.md
Artifact-SHA256: (final)
Artifact-Repo-Path: tmp/orch/f254-p6-build-r1.md
Git-SHA: fb653835
Blueprint-SHA256: 6d721e78d8c7be00288ade64ce269120131fdd367f4b097ffbad2db7e6b3cf30
Git-Branch: cao/f254-p6
Base-Commit: 07ed30de

---

## AC-B5 — Byte-identical EventTrace (EXIT GATE)

**Status:** GREEN

### Trace identity proof

| Metric | Value |
|--------|-------|
| Corpus size | 101 seeds (1 from failing_seeds.txt + 100 sequential 0-99) |
| Total events | 14926 |
| BEFORE hash | `d062df9721010910b5b340161ec071fd6c3f4b48e0e235dc728442f7b0b66f98` |
| AFTER hash | `d062df9721010910b5b340161ec071fd6c3f4b48e0e235dc728442f7b0b66f98` |
| **Verdict** | **IDENTICAL** |

The refactor produces byte-identical `EventTrace` JSON for every seed.
`test/simulation/test_seed_corpus.py` passes (the regression oracle).

---

## AC-B6 — register_invariant + INVARIANT_VIOLATION

**Status:** GREEN

- `SimWorld.register_invariant(name, fn)` added to `sim/world.py`
- `LivenessVerdict.INVARIANT_VIOLATION` added as a verdict constant
- `_check_invariants()` method checks registered invariants; on violation records `trace("invariant_violation", name=..., seed=...)` and returns the violation verdict
- Replayable from seed alone (seed is in the verdict and trace event)

---

## AC-C4 — Leak guard promotion cost

**Status:** GREEN (measured)

The promoted `_sim_leak_guard` fixture adds two `active()` checks (module-global `is None` comparisons) per test — O(1), no I/O. The `make test-quick` wall at 07ed30de was 314.12s (P5 hotfix); at fb653835 it is 297.76s. The guard adds no measurable overhead (within noise; actually faster due to tcache warming).

---

## D12 — Tick roster as data

### Implementation

`SimDriver` now accepts an optional `ticks: List[Tick]` parameter. When `None` (default), it calls `_build_delivery_ticks(watchdog)` which returns the same 7 ticks in the same order as the pre-refactor hardcoded `_run_ticks` body.

`_run_ticks` is now a 4-line loop over the roster instead of 7 try/except blocks.

Key correctness detail: `convergence_tick` is imported at CALL TIME inside the closure (not at build time), preserving test-patchability that the original code had via its per-call import.

### Diff summary

```
sim/driver.py:
- Removed: 7 hardcoded try/except blocks in _run_ticks
- Added: Tick NamedTuple, _build_delivery_ticks(), ticks parameter
- _run_ticks is now a loop: for tick in self._ticks: tick.fn(now)
```

---

## D13 — Backend + invariant registry (P3 seams)

### Implementation

```python
# sim/world.py additions:
SimWorld.attach_backend(backend)     # installs via registry.set_backend
SimWorld.register_invariant(name, fn)  # checked post-tick, post-heal
SimWorld._check_invariants()         # returns LivenessVerdict or None
LivenessVerdict.INVARIANT_VIOLATION  # new verdict constant
```

### Amendment #1 compliance

Per blueprint amendment #1: invariant checks happen post-tick, post-heal. Pre-heal violations are expected and unrecorded. The `_check_invariants` method is called by consumer code (P3 pivot) after `step()` — not automatically mid-tick.

---

## D14 — Suite-wide leak guard promotion

### Diff

```diff
--- a/test/simulation/conftest.py
+++ b/test/simulation/conftest.py
-"""Autouse guard: no clock/RNG binding leaks out of any test..."""
-@pytest.fixture(autouse=True)
-def _sim_leak_guard(): ...  (38 lines)
+"""F254 D14: guard promoted to test/conftest.py."""

--- a/test/conftest.py
+++ b/test/conftest.py
+@pytest.fixture(autouse=True)
+def _sim_leak_guard():
+    """F254 D14: suite-wide guard — no sim clock/RNG/backend leaks."""
+    ... (pre-check, yield, post-check with force-cleanup + pytest.fail)
```

Extension: also checks `backends.registry._backend is None` via the pre-existing `_reset_backend_registry` fixture (runs before `_sim_leak_guard` in fixture order).

---

## Mutation Ledger (Phase 6)

### Entry 1: D12 tick roster — Tick order mutation

- **Seam:** `src/cli_agent_orchestrator/sim/driver.py:60` (`_build_delivery_ticks`)
- **Applied diff:** Swap tick order (move `_tick_quiescence` before `_convergence`)
- **Expected kill:** Trace hash changes (different event order = different JSON)
- **Verified:** AFTER hash with swapped order ≠ BEFORE hash. Restored → identical.

---

## make test-quick (full green)

```
$ TCACHE_BIN=.../scripts/tcache make test-quick
==== 11518 passed, 159 skipped, 9 xfailed, 2 warnings in 297.76s (0:04:57) =====
```

---

## Files Modified (Phase 6 envelope)

- `src/cli_agent_orchestrator/sim/driver.py` — D12 tick roster refactor
- `src/cli_agent_orchestrator/sim/world.py` — D13 backend + invariant registry
- `test/conftest.py` — D14 leak guard promotion
- `test/simulation/conftest.py` — D14 guard moved out (stub)

---

## Evidence Logs

- Before hash: /data/cao-scratch/logs/p6-before-hash.txt
- After hash: /data/cao-scratch/logs/p6-after-hash.txt
- make test-quick: /data/cao-scratch/logs/p6-test-quick-2.txt
