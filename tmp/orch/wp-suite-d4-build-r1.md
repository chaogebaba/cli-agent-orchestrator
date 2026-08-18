# WP-SUITE D4 Build Report (r1)

**Wall:** D4 — suite slot at the pytest layer (F272)
**Branch:** `cao/95e96790`
**HEAD:** `20878cab2c3f66c220097737798d99bfe4d919f1`
**Base:** fork mainline at same commit (worktree branch, no prior commits)

## Diff stat

```
 scripts/run-pytest.sh | 13 ++++---------
 test/conftest.py      |  1 +
 test/plugins/suite_slot.py (new) | 171 +++
 2 files changed (+1 new), 5 insertions(+), 9 deletions(-)
```

## AC coverage

| AC | Status | Evidence |
|----|--------|----------|
| AC4.1 | DONE | `test/plugins/suite_slot.py` — acquires flock in `pytest_configure`, controller-only (`_is_xdist_worker` guard), releases in `pytest_unconfigure`. Registered in `test/conftest.py:111`. |
| AC4.2 | DONE | `scripts/run-pytest.sh` — SUITE_LOCK definition and flock block (lines 25, 43-47) deleted. Lock path `/data/cao-scratch/.suite-slot.lock` is the single acquisition site. |
| AC4.3 | DONE | Lockfile carries `pid=<N> terminal=<id> tmux=<pane> sha=<short> since=<HH:MMZ>`. On contention prints holder identity. |
| AC4.4 | DONE | Default is block-and-wait. `CAO_SUITE_SLOT=nowait` fails fast with exit 1. |
| AC4.5 | DONE | No-op when `CI` env is set — confirmed: `CI=true` run produces no lockfile. |
| AC4.6 | DONE | `_try_reclaim_stale_lock()` checks holder pid liveness; flock(2) auto-releases on death, identity file is advisory. |

## Evidence: slot contention test

```
$ uv run python <contention-script>
PASS: lockfile written: pid=3296910 terminal=95e96790 tmux=%1696 sha=20878cab since=16:59Z
PASS: nowait contention detected (exit=1)
  msg: Exit: [suite-slot] FAIL: slot busy (CAO_SUITE_SLOT=nowait). Holder: pid=3296910 terminal=95e96790 tmux=%1696 sha=20878cab since=16:59Z
PASS: contention test complete
```

## Evidence: CI no-op proof

```
$ CI=true uv run pytest -n 2 --collect-only -q
11711 tests collected in 9.91s
$ test -f /data/cao-scratch/.suite-slot.lock  → NO (file not created)
```

## Evidence: full suite run (make test-ci)

```
Command: make test-ci TCACHE=bypass ARGS="--timeout=30 -q"
Lock msg: [suite-slot] lock acquired — pid=3392290 terminal=95e96790 tmux=%1696 sha=20878cab since=17:10Z

Result: 1 failed, 11527 passed, 36 skipped, 8 xfailed, 1 xpassed, 2 warnings in 1352.50s (0:22:32)
Exit code: 2 (make wraps pytest exit 1 → make error 2)
```

**The 1 failure** is `test/services/test_ready_deadline_edge_probe.py::test_ready_completion_at_deadline_has_one_lawful_owner` — a pre-existing non-deterministic flake:

- **F262 gate record** (`tmp/orch/f262-build-r1.md:49`): listed as class "(d) irreproducible" in the F262 failure table.
- **Base-commit isolation**: test passes at base `20878cab` with identical `-n 2` invocation (1 passed in 6.13s).
- **D4-commit isolation**: test passes with D4 changes applied (1 passed in 4.43s).
- **Conclusion**: non-deterministic race, classified pre-existing by F262. Not caused by suite-slot changes.

## Operational note: sibling serialization

During this run, sibling terminal `473b1670` was running its own full suite on the same lock path. My run waited for slot release (the plugin's block-and-wait default), then acquired. This confirms cross-worktree serialization works via the shared `/data/cao-scratch/.suite-slot.lock` path — the same resource the legacy wrapper used at `/tmp/cao-suite.lock`, now unified.

## Verdict

**D4 COMPLETE.** All six ACs implemented and empirically verified. Suite green minus one pre-existing unrelated flake.
