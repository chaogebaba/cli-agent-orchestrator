# Codex Worker Init Pipeline — Debug Report

**Date:** 2026-08-24  
**Branch:** `cao/6e515e5c`  
**Tip SHA:** `d5640c95`  
**Status:** Fixed — OUR bug, not codex-version-behavior

---

## Root Cause

**Persona CODEX_HOME dropped by tmux blocked-prefix filter.**

The init pipeline breaks at the `validate_session_artifact` step because the validator and the codex binary disagree on where session files live:

1. `compose_persona_plan` creates an isolated `codex-home/` dir for the worker (under `/run/user/1000/cao-personas/<terminal_id>/current/gen-1/codex-home/`)
2. `sandbox_guard.py` correctly injects `CODEX_HOME=<persona codex-home>` into the pane environment
3. **BUG:** `tmux.py:_BLOCKED_ENV_PREFIXES` includes `"CODEX_"` — and `_merge_extra_env` drops `CODEX_HOME` in production mode (non-sandbox), logging: `"Dropping forwarded env var with blocked prefix: CODEX_HOME"`
4. Without `CODEX_HOME`, the codex binary defaults to `~/.codex` and writes sessions there
5. `validate_session_artifact` calls `_resolved_codex_home(terminal_id)` → finds the persona plan → resolves to `<persona codex-home>` (which has NO `sessions/` directory)
6. Glob finds zero matches → `RetryableArtifactValidation("session_artifact_missing")` → retries until deadline → watchdog fires → worker unwound

**Confirmed in all 5 failing terminals:** Every spawn had the log line:
```
cli_agent_orchestrator.clients.tmux - WARNING - Dropping forwarded env var with blocked prefix: CODEX_HOME
```

**Secondary issue:** Even if CODEX_HOME passed through, `codex resume <uuid>` still wouldn't find the seed session because:
- `seed_resume_identity` runs as a subprocess (no persona env) → rollout lands in `~/.codex/sessions/`
- If the pane's codex gets `CODEX_HOME=<persona home>`, it looks in `<persona home>/sessions/` → not found

---

## Fix (2 changes)

### 1. tmux.py — Allow CODEX_HOME through blocked-prefix filter

Added `"CODEX_HOME"` to `_BLOCKED_PREFIX_ALLOWLIST`. Rationale: CODEX_HOME is a config/session directory path — it cannot cause "nested session" errors (the original reason for blocking `CODEX_*`). Other `CODEX_*` vars (CODEX_TOKEN, CODEX_SESSION_ID, etc.) remain blocked.

### 2. persona_context.py — Symlink sessions/ into persona codex-home

After creating the persona's `codex-home/` staging dir and symlinking `auth.json`, we now also symlink `sessions → <production home>/sessions`. This ensures:
- `codex resume <uuid>` (running with CODEX_HOME pointing to persona home) can find the seed's rollout via the symlink
- The validator's glob through persona home resolves to the same physical files

---

## Evidence

### Log analysis (all 5 terminals: d85e84f0, 6364da8b, c404ed1e, 91a11c1b, 34990a75)

| Terminal | CODEX_HOME dropped? | Result |
|----------|---------------------|--------|
| d85e84f0 | YES (05:34:25) | watchdog_deadline + session_artifact_missing |
| 6364da8b | YES (05:38:23) | session_artifact_missing at task delivery |
| c404ed1e | YES (05:40:17) | watchdog_deadline |
| 91a11c1b | YES (05:43:26) | deferred_init cancelled + watchdog |
| 34990a75 | YES (05:44:37) | startup prompt timeout + watchdog |

### Test results

```
test/clients/test_codex_init_persona_home.py — 6 passed
test/clients/test_tmux_merge_extra_env.py    — 9 passed (existing tests unaffected)
test/utils/test_persona_context.py           — 27 passed
test/clients/test_persona_retention.py       — 12 passed
test/services/test_f110_deferred_init_watchdog.py — 7 passed
```

---

## Confounders (not the root cause)

- **codex v0.149.1 resume-cwd dialog:** Appeared on early spawns (d85e84f0, 6364da8b) before `resume_cwd="current"` was persisted at 05:43. This added latency but is NOT the root cause — the dialog is handled by `_handle_trust_prompt`, and even after the dialog was eliminated (91a11c1b, 34990a75), the session_artifact_missing still blocked delivery.

- **cc-switch at 05:43:** Rewrote `~/.codex/config.toml` to add `resume_cwd = "current"`. This fixed the dialog but didn't affect the persona home mismatch.

---

## Files Changed

- `src/cli_agent_orchestrator/clients/tmux.py` — CODEX_HOME added to allowlist
- `src/cli_agent_orchestrator/utils/persona_context.py` — sessions symlink in compose
- `test/clients/test_codex_init_persona_home.py` — regression test (6 cases)

---

## Upstream Behavior Hardening (codex 0.142.5+ / 0.148+)

### a) Lazy rollout creation (openai/codex#31158, since 0.142.5)

Rollout files are created at FIRST TURN, not thread start. The fix's sessions
symlink does NOT mask this into a hard failure:
- Empty glob through symlink → `RetryableArtifactValidation("session_artifact_missing")` — same retryable error as without symlink
- Retry window: 60s (`artifact_validate_deadline_s`), poll_interval=0.4s default
  (deferred path overrides to POLL_INTERVAL=2.0s) → 30–150 retries depending on path
- The symlink is to a directory (not a file); it doesn't change the glob's semantics for absent files

No adjustment needed — existing retry window tolerates first-turn latency.

### b) PTY input discarded during TUI startup (openai/codex#38641, since 0.148)

Our spawn path is safe: the task message is delivered ONLY after
`provider_instance.initialize()` returns (which waits for the idle composer
prompt via `_handle_trust_prompt`). The send_input call is sequenced AFTER:
  1. `initialize()` (idle prompt visible)
  2. `_confirm_launch_health()` (process alive)
  3. `_validate_deferred_artifact()` (rollout exists)

The shell command that launches codex (`codex resume <uuid>`) is consumed by
the shell, not the codex TUI, so it's unaffected by codex's input buffering.

**Residual risk (separate issue, out of scope):** If `_handle_trust_prompt`
times out (the 20s startup_prompt_handler_timeout fires), the code logs an
error and proceeds — in theory this could deliver a message before the TUI is
fully ready. However, this is a pre-existing behavior unrelated to this fix
and already handled by `_confirm_worker_started_or_resubmit`.

### c) Cross-persona session symlink — collision risk analysis

All persona codex-homes symlink sessions/ to the same `~/.codex/sessions/`.
This is acceptable:

1. **UUID uniqueness:** Each seed creates a fresh UUID via `codex exec`; the
   fork_context binds that UUID exclusively to one worker
2. **Glob specificity:** `validate_session_artifact` globs `*{session_uuid}*`
   — UUID substring collision probability is negligible (36-char hex)
3. **Resume isolation:** `codex resume <uuid>` is session-locked; it cannot
   interfere with another session's files
4. **Pre-existing sharing:** All processes on this machine already share
   `~/.codex/sessions/` at the OS level. Persona isolation targets config and
   auth (preventing credential leakage between profiles), not session-file
   visibility. The shared sessions dir is the SAME isolation boundary as
   before this fix — we just made the persona's codex binary aware of it

