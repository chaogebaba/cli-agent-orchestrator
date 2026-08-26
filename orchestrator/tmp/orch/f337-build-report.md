# F337 Build Report — Native Wake Activation Readiness

**Branch:** `cao/f337-native-wake`  
**Base:** `main@8c994302` (includes F461 coalescer + F487/F475)  
**Date:** 2026-08-26

## Changes

### 1. Auth Handshake (F337 #192)

**File:** `src/cli_agent_orchestrator/services/cc_session_registry.py`

- `read_peer_token(pid, sessions_dir)` — reads `<sessions_dir>/<pid>.<hex>.key` JSON files, extracts `peerToken` field
- `_build_auth_frame(token)` — formats `{"type":"auth","token":"<token>"}` JSON line (compact, no spaces)
- `write_to_socket()` updated — when `auth_token` is provided, sends the JSON auth frame as the first line before the message payload

**File:** `src/cli_agent_orchestrator/services/doorbell_service.py`

- `_attempt_native_ring()` now calls `read_peer_token(record.pid)` and passes the result as `auth_token` to `write_to_socket`
- Graceful degradation: if no key file exists, auth frame is skipped (backward compat with pre-UDS-gate CC versions)

### 2. Wake.native Split-Brain Gate (F457)

**File:** `src/cli_agent_orchestrator/services/terminal_service.py`

- `cc_team_inbox_path` derivation at pane creation now gated on `_native_wake_enabled = ConfigService.get("supervisor.wake.native", default=True)`
- Both if/elif branches for inbox path derivation require the flag

**Pre-existing gates (already in place on base, verified intact):**
- `inbox_service.py:2320` — deliver_pending teammate push
- `inbox_service.py:3509` — reconcile_pull_mode_notifications
- `delivery_service.py:403` — attempt_rung1 native ring

### Wire Format (Probed)

```
Line 1: {"type":"auth","token":"<peerToken>"}\n     ← from key file
Line 2: {"msgV":1,"msg_id":"...","type":"user",...}\n  ← message payload
         [half-close, no read]
```

**Evidence source:** Live probe against throwaway CC 2.1.243 session (pid 852122), socket at `/run/user/1000/cc-socks/852122.sock`. Both JSON auth frame and raw token accepted; JSON frame chosen as the canonical wire format per CC protocol docs.

**Key file format:** `~/.claude/sessions/<pid>.<64hex>.key` → `{"peerToken":"<32hex>","procStart":"<int>"}`

## Test Results

### New Tests (14 tests, all pass)

`test/services/test_f337_auth_handshake.py`:
- AC1: Auth frame sent as first line / not sent when token absent
- AC2: read_peer_token reads key files correctly (6 cases)
- AC3: _build_auth_frame format verification
- AC4: _attempt_native_ring end-to-end auth flow (with/without key file)
- AC5: wake.native gate in terminal_service (structural)

### Full Suite (box cursor-4)

```
13634 passed, 14 failed, 204 skipped, 15 xfailed
```

**14 failures = all pre-existing on base 8c994302** (verified by running same tests on detached HEAD at base).  
**0 new failures introduced by this branch.**

### Related Tests (150 tests, local)

```
test_fx170_native_doorbell.py: 60 passed
test_f216_null_socket_path.py: 17 passed  
test_f186_reconciler_doorbell_lock.py: (subset)
test_fx168_doorbell.py: (full)
test_f461_doorbell_coalesce.py: (full)
Total: 150 passed
```

## Default Values (unchanged)

| Config key | Code default | Production override |
|---|---|---|
| `supervisor.wake.native` | `True` | `false` (stays false) |
| `supervisor.teammate_push` | `False` | varies |
| `supervisor.doorbell` | `True` | `true` |

Flipping `supervisor.wake.native=true` is a separate live trial. This branch makes it safe to flip.
