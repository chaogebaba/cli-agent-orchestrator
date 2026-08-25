# F469 Build Report — park-clients-on-kill

## Summary

Resolves GH issue #324: when `kill_window` reaps a worker window, attached
tmux clients viewing that window are now parked on a stable surviving window
(supervisor seat at index 0, or any other survivor) BEFORE the kill, preventing
tmux's default focus-stealing fallback.

## Changes

### `src/cli_agent_orchestrator/backends/tmux_backend.py`
- `kill_window()` calls `_park_clients_off_window()` before delegating to
  the underlying `_client.kill_window()`.
- New helpers: `_park_clients_off_window`, `_resolve_window_index`,
  `_clients_on_window`, `_park_single_client`, `_any_surviving_window`.
- All parking logic is best-effort with exception swallowing — a parking
  failure never blocks the kill.

### `test/backends/test_f469_park_clients_on_kill.py` (new)
- Socket-isolated integration tests using `pty.fork()` to attach a real
  tmux client on a per-test isolated tmux server.
- 4 test cases: park-on-win0, not-moved-if-not-viewing, parking-failure-
  graceful, kill-win0-parks-on-survivor.
- xdist group marker ensures serial execution of pty-dependent tests.

### `test/conftest.py`
- Added `CAO_TMUX_SOCKET` to the `_hermetic_cao_env` autouse fixture's
  env-stripping list — prevents the enclosing CAO terminal's socket from
  leaking into tests that don't explicitly set one, which was the reported
  blocker (fixture ordering: autouse deletes first, explicit `tmux_env`
  fixture sets the isolated value second via the same monkeypatch scope).

## Blocker Resolution

The previous lane reported that the conftest autouse fixture
`_hermetic_cao_env` fought with the test's `tmux_env` fixture over
`CAO_TMUX_SOCKET` / `CAO_INSTANCE_ID` env ordering.

Root cause: `_hermetic_cao_env` stripped `CAO_INSTANCE_ID` but not
`CAO_TMUX_SOCKET`. If the test suite ran inside a real CAO terminal (which
has `CAO_TMUX_SOCKET` set), that socket could leak into test code that reads
env at call-time. The fix is simple: add `CAO_TMUX_SOCKET` to the fixture's
striplist. The explicit `tmux_env` fixture then sets BOTH vars after the
autouse cleanup (same monkeypatch scope, later write wins — guaranteed by
pytest's autouse-before-explicit ordering).

## Test Evidence

```
$ uv run pytest test/backends/test_f469_park_clients_on_kill.py -v
4 passed in 3.67s

$ uv run pytest test/backends/ --timeout=30 -q
2 failed (pre-existing, unrelated: allowed_blocked_values kwarg mismatch)
192 passed in 8.51s
```

## Residual Risk

- The 2 pre-existing failures in `test_tmux_backend.py` are unrelated (a
  recent `allowed_blocked_values` parameter addition not reflected in mock
  assertions) and should be fixed in a separate commit.
- Tests rely on `pty.fork()` which is Linux-only; CI already runs on Linux.


---

## R2 — Gate-fix rebuild (scope-only, no code changes)

**Base:** `origin/main` @ 92fe6295 (post-F467 merge)
**Previous branch:** `cao/146d9726` @ 2689ca76 — GATE-NO (2 BLOCKERs)

### What was dropped

The original branch was built on a pre-F467 base and carried stale copies of
5 non-F469 files that regressed F467 (re-introduced `tzlocal.get_localzone()`
in fleet_service, cleanup_service, inbox_service) and deleted the parametrized
4-TZ F467 proof test. None of these changes were F469 scope:

- `src/cli_agent_orchestrator/services/fleet_service.py`
- `src/cli_agent_orchestrator/services/cleanup_service.py`
- `src/cli_agent_orchestrator/services/inbox_service.py`
- `test/services/test_f72_fleet_lifecycle.py`
- `test/services/test_wpm1_delivery.py`
- `orchestrator/tmp/orch/f467-build-report.md` (spurious deletion)

Plus several other unrelated files (cc_session_registry, doorbell_service,
test_f459, test_f186, test_fx168) that were diffs against the stale base.

### What was kept (scope = F469 only)

Rebuilt `cao/f469-r2` from current `main` (92fe6295), applying ONLY the 4
F469-scoped file diffs via `git diff | git apply` and per-file checkout:

1. `src/cli_agent_orchestrator/backends/tmux_backend.py` — park-clients impl
2. `test/backends/test_f469_park_clients_on_kill.py` — integration tests
3. `test/conftest.py` — CAO_TMUX_SOCKET striplist addition
4. `orchestrator/tmp/orch/f469-build-report.md` — this report

### Verification

- `git diff main..cao/f469-r2 --stat` lists exactly those 4 paths.
- F467 parametrized 4-TZ test is identical to main's (0 diff).
- `uv run pytest test/backends/ -q --timeout=30`: 192 passed, 2 failed
  (known F471 `allowed_blocked_values` kwarg mismatch — pre-existing, separate fix).
- F469 tests: 4/4 passed.
