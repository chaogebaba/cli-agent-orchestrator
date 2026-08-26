# F514 (#369, P1) — build report

**Fix:** the supervisor pane-nudge wake fallback now survives a cao-server
restart, independent of the `supervisor.wake.native` flip state.

- Branch: `cao/aac674ab`
- HEAD: `c7b3c27fe0fa30d2469663bbf0e5f8fff876317f`
- Worktree: `.cao/worktrees/aac674ab` (fork, isolated)

---

## Root cause (confirmed against the diagnosis)

Per `orchestrator/tmp/orch/f213-quirk-report.md` §3/§4/§6, message 1081 (and
every message that day) hit `f170_doorbell ... decision=skipped_disabled
reason=not_registered_fallback`. The chain:

1. Native ring is off by config (`supervisor.wake.native=false`) → falls
   through to the fx168 pane-nudge fallback.
2. The fallback gate `teammate_push_service._should_teammate_push()` checked
   the **raw** `metadata['cc_team_inbox_path']` key and returned `False` when
   it was absent.
3. The 11:16:29 cao-server restart recreated terminal `6a68b6ee` from scratch
   ("Terminal metadata not found"), so `cc_team_inbox_path` was gone and
   **nothing repopulated it** → the gate returned `False` on every message →
   the supervisor wake safety net was silently dead until full
   re-registration.

Critically, `_resolve_inbox_path()` **already** has an F152 lazy self-heal
that re-derives the path from the persisted `working_directory` + provider and
persists it back — but `_should_teammate_push()` short-circuited on the raw
key before that self-heal ever ran.

## The fix

`src/cli_agent_orchestrator/services/teammate_push_service.py` —
`_should_teammate_push()` now routes through `_resolve_inbox_path()`:

```python
def _should_teammate_push(terminal_id: str) -> bool:
    if not ConfigService.get("supervisor.teammate_push"):
        return False
    return _resolve_inbox_path(terminal_id) is not None
```

Properties:

- **Durable / self-healing:** `_resolve_inbox_path` re-derives the lost path
  from the persisted `working_directory` (a real terminals-table column) and
  `provider == "claude_code"`, then persists it back via
  `update_terminal_metadata` for future lookups. Survives a restart because it
  is rebuilt from durable row state, not the ephemeral metadata key.
- **Independent of `wake.native`:** neither `_should_teammate_push` nor
  `_resolve_inbox_path` consults `supervisor.wake.native`. The fallback is the
  net and must arm regardless of the native flip — exactly the F514 goal.
- **F337 gate untouched:** the blueprint-frozen create-time derivation gate
  `_maybe_derive_cc_team_inbox_path` (terminal_service.py:1466) still requires
  `teammate_push AND wake.native` and is **not modified**. F514 fixes only the
  fallback/recovery path, not creation-time behavior — so no conflict with the
  frozen F337 semantics, and no stop-and-ask was needed.
- **Behavior-preserving for the old contract:** still `False` when the flag is
  off, metadata is missing, provider is non-claude, or no `working_directory`
  is available (nothing to derive from).

### Diff (`git diff --stat`)

```
 src/cli_agent_orchestrator/services/teammate_push_service.py | 26 +++++++---
 test/services/test_f514_fallback_survives_restart.py         | (new)
 2 files changed, 270 insertions(+), 6 deletions(-)
```

Production change is 6 lines of logic (+ docstring); the rest is the new test
file.

## Regression tests (restart-simulation shape)

New: `test/services/test_f514_fallback_survives_restart.py` — models the
restart shape (claude_code provider + persisted `working_directory` but **no**
`cc_team_inbox_path`):

- `test_gate_true_after_restart_rederives_path` — gate returns True after the
  key is lost, and the re-derived path is **persisted back** durably.
- `test_gate_independent_of_wake_native` — gate True even with
  `supervisor.wake.native=False`.
- `test_gate_false_when_flag_off_even_if_derivable` — flag off short-circuits,
  no derivation attempted.
- `test_gate_false_when_not_derivable_non_claude` — non-claude terminal stays
  False, self-heal must not over-fire.
- `test_gate_false_when_no_working_directory` — nothing to derive from → False.
- `test_fallback_rings_after_restart` — end-to-end at
  `ring_supervisor_doorbell`: with native off and the restarted-terminal
  metadata, the doorbell now reaches the fx168 fallback and returns `"rang"`
  (was `"skipped_disabled"` / `not_registered_fallback` before the fix).

### Test evidence (local targeted runs — not the full suite; M8 respected)

```
test/services/test_f514_fallback_survives_restart.py ......  6 passed in 3.02s

# no-regression sweep of adjacent suites:
test_teammate_push_bridge.py + test_fx168_doorbell.py +
test_fx170_native_doorbell.py + test_f337_auth_handshake.py
  168 passed, 1 xfailed in 8.82s   (xfail pre-existing, unrelated)

test_fx158_pull_reconciler.py + test_f186_reconciler_doorbell_lock.py
  20 passed in 2.16s
```

Import/compile check of the changed module: OK.

## Notes / scope

- Repo-only search, as instructed. The `fx168`/`fx170`/`f337` design docs
  referenced in the diagnosis live in the orchestrator tree, not this source
  repo; the F337 frozen contract as expressed in code (the create-time helper)
  was located, read, and left untouched.
- No full suite run was performed locally (M8). If a full-suite gate is
  wanted, it should go to an offload box via `scripts/box-run.sh`
  (cursor-1/3/4/5).
- The diagnosis also lists other independent gaps (native off by config, the
  WS-monitor arming reminder wired only into a Kiro hook, the f213 10s-TTL
  watcher gap, and a D-item S2 subagent-marker bug). Those are **out of scope**
  for F514, which is specifically the lost-metadata fallback death.
